#!/usr/bin/env python3
"""
crypto_signal_engine.py

Reads public market data (no exchange API keys needed) for a configurable
list of tokens, computes RSI + MACD, and builds an entry/stop/target plan
for three trade styles:

    - spot   -> buy zone only, no leverage, no shorting
    - long   -> futures/perp long: entry, stop-loss, take-profit
    - short  -> futures/perp short: entry, stop-loss, take-profit

Alerts fire on:
    1. A signal STATE CHANGE (e.g. neutral -> long, long -> short)
    2. A price crossing a level you set in config/tokens_config.json

All alerts are emailed. State is persisted to state/last_state.json so you
don't get the same alert every loop -- only when something actually changes.

Usage:
    pip install ccxt pandas python-dotenv --break-system-packages
    cp .env.example .env        # fill in SMTP details
    python crypto_signal_engine.py            # run continuously
    python crypto_signal_engine.py --once      # run a single pass and exit
    python crypto_signal_engine.py --check-symbols   # just verify every
                                                       # symbol resolves
"""

import argparse
import json
import os
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

import ccxt
import pandas as pd
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config" / "tokens_config.json"
STATE_DIR = BASE_DIR / "state"
STATE_PATH = STATE_DIR / "last_state.json"
LOG_PATH = BASE_DIR / "signal_engine.log"

load_dotenv(BASE_DIR / ".env")


# --------------------------------------------------------------------------
# Config + state I/O
# --------------------------------------------------------------------------

def load_config():
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def load_state():
    STATE_DIR.mkdir(exist_ok=True)
    if STATE_PATH.exists():
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def log(msg):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{stamp}] {msg}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


# --------------------------------------------------------------------------
# Exchange access (public data only -- no API keys required)
# --------------------------------------------------------------------------

_exchange_cache = {}


def get_exchange(exchange_id):
    if exchange_id not in _exchange_cache:
        klass = getattr(ccxt, exchange_id)
        _exchange_cache[exchange_id] = klass({"enableRateLimit": True})
    return _exchange_cache[exchange_id]


def fetch_ohlcv_with_fallback(symbol, timeframe, limit, exchanges_priority):
    """Try each exchange in order until one has this symbol. Returns
    (dataframe, exchange_id_used) or (None, None) if nobody has it."""
    last_err = None
    for exchange_id in exchanges_priority:
        try:
            ex = get_exchange(exchange_id)
            if not ex.has.get("fetchOHLCV", False):
                continue
            ex.load_markets()
            if symbol not in ex.markets:
                continue
            raw = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            if not raw or len(raw) < 30:
                continue
            df = pd.DataFrame(
                raw, columns=["ts", "open", "high", "low", "close", "volume"]
            )
            return df, exchange_id
        except Exception as e:  # noqa: BLE001 - keep trying other exchanges
            last_err = e
            continue
    if last_err:
        log(f"  (last error for {symbol}: {last_err})")
    return None, None


# --------------------------------------------------------------------------
# Indicators
# --------------------------------------------------------------------------

def compute_rsi(close, period):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-12)
    return 100 - (100 / (1 + rs))


def compute_macd(close, fast, slow, signal):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


# --------------------------------------------------------------------------
# Signal + trade-plan logic
# --------------------------------------------------------------------------

def build_plan(df, cfg):
    """Given an OHLCV dataframe, return a dict describing current state
    and, where applicable, spot/long/short entry-stop-target plans."""

    close = df["close"]
    rsi = compute_rsi(close, cfg["rsi_period"])
    macd_line, signal_line, hist = compute_macd(
        close, cfg["macd_fast"], cfg["macd_slow"], cfg["macd_signal"]
    )

    last_close = close.iloc[-1]
    last_rsi = rsi.iloc[-1]
    hist_now, hist_prev = hist.iloc[-1], hist.iloc[-2]
    macd_bull_cross = hist_prev <= 0 < hist_now
    macd_bear_cross = hist_prev >= 0 > hist_now

    lookback = cfg["swing_lookback_candles"]
    swing_high = df["high"].iloc[-lookback:].max()
    swing_low = df["low"].iloc[-lookback:].min()
    rr = cfg["risk_reward_ratio"]

    state = "neutral"
    reasons = []

    bullish_bias = macd_bull_cross or (hist_now > 0 and last_rsi < 60)
    bearish_bias = macd_bear_cross or (hist_now < 0 and last_rsi > 40)

    if macd_bull_cross:
        reasons.append("MACD bullish crossover")
    if macd_bear_cross:
        reasons.append("MACD bearish crossover")
    if last_rsi <= cfg["rsi_oversold"]:
        reasons.append(f"RSI oversold ({last_rsi:.1f})")
    if last_rsi >= cfg["rsi_overbought"]:
        reasons.append(f"RSI overbought ({last_rsi:.1f})")

    plan = {
        "price": round(float(last_close), 6),
        "rsi": round(float(last_rsi), 2),
        "macd_hist": round(float(hist_now), 6),
        "reasons": reasons,
        "spot": None,
        "long": None,
        "short": None,
    }

    if bullish_bias and last_rsi < cfg["rsi_overbought"]:
        state = "long"
        stop = swing_low
        risk = last_close - stop
        target = last_close + risk * rr if risk > 0 else last_close * 1.03
        plan_entry = {
            "entry": round(float(last_close), 6),
            "stop_loss": round(float(stop), 6),
            "take_profit": round(float(target), 6),
            "risk_reward": rr,
        }
        if cfg["modes"]["spot"]:
            plan["spot"] = {**plan_entry, "note": "Buy zone (no leverage)"}
        if cfg["modes"]["long"]:
            plan["long"] = plan_entry

    elif bearish_bias and last_rsi > cfg["rsi_oversold"]:
        state = "short"
        stop = swing_high
        risk = stop - last_close
        target = last_close - risk * rr if risk > 0 else last_close * 0.97
        if cfg["modes"]["short"]:
            plan["short"] = {
                "entry": round(float(last_close), 6),
                "stop_loss": round(float(stop), 6),
                "take_profit": round(float(target), 6),
                "risk_reward": rr,
            }

    plan["state"] = state
    return plan


def check_price_alerts(symbol, price, alert_cfg, fired_state):
    """Returns a list of alert message strings for one-time level crosses."""
    msgs = []
    levels = alert_cfg.get(symbol, {})
    above = levels.get("above")
    below = levels.get("below")
    fired = fired_state.setdefault(symbol, {"above": False, "below": False})

    if above is not None:
        if price >= above and not fired["above"]:
            msgs.append(f"{symbol} crossed ABOVE {above} (now {price})")
            fired["above"] = True
        elif price < above:
            fired["above"] = False

    if below is not None:
        if price <= below and not fired["below"]:
            msgs.append(f"{symbol} crossed BELOW {below} (now {price})")
            fired["below"] = True
        elif price > below:
            fired["below"] = False

    return msgs


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------

def send_email(subject, body):
    host = os.getenv("SMTP_SERVER")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASSWORD")
    to_addr = os.getenv("ALERT_EMAIL_TO", user)

    if not all([host, user, password, to_addr]):
        log("Email not sent -- SMTP settings missing in .env")
        return

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr

    try:
        with smtplib.SMTP(host, port, timeout=20) as server:
            server.starttls()
            server.login(user, password)
            server.send_message(msg)
        log(f"Email sent: {subject}")
    except Exception as e:  # noqa: BLE001
        log(f"Email FAILED: {e}")


def format_plan_body(symbol, exchange_id, plan):
    lines = [
        f"{symbol}  (data: {exchange_id})",
        f"Price: {plan['price']}   RSI: {plan['rsi']}   MACD hist: {plan['macd_hist']}",
        f"Reasons: {', '.join(plan['reasons']) if plan['reasons'] else 'trend continuation'}",
        "",
    ]
    for label in ("spot", "long", "short"):
        p = plan.get(label)
        if p:
            lines.append(f"[{label.upper()}]")
            lines.append(f"  Entry:       {p['entry']}")
            lines.append(f"  Stop-loss:   {p['stop_loss']}")
            lines.append(f"  Take-profit: {p['take_profit']}  (R:R {p['risk_reward']})")
            if "note" in p:
                lines.append(f"  Note: {p['note']}")
            lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def run_pass(cfg, state):
    last_signal_state = state.setdefault("signal_state", {})
    price_alert_fired = state.setdefault("price_alert_fired", {})

    changed_msgs = []
    level_msgs = []

    for symbol in cfg["tokens"]:
        df, exchange_id = fetch_ohlcv_with_fallback(
            symbol,
            cfg["timeframe"],
            limit=max(100, cfg["swing_lookback_candles"] + cfg["macd_slow"] + 10),
            exchanges_priority=cfg["exchanges_priority"],
        )
        if df is None:
            log(f"SKIP {symbol}: not found on any configured exchange")
            continue

        plan = build_plan(df, cfg)
        log(f"{symbol}: state={plan['state']} price={plan['price']} rsi={plan['rsi']}")

        prev_state = last_signal_state.get(symbol)
        if plan["state"] != "neutral" and plan["state"] != prev_state:
            changed_msgs.append(format_plan_body(symbol, exchange_id, plan))
        last_signal_state[symbol] = plan["state"]

        for m in check_price_alerts(
            symbol, plan["price"], cfg["price_alerts"], price_alert_fired
        ):
            level_msgs.append(m)

    if changed_msgs:
        send_email(
            subject=f"Signal change: {len(changed_msgs)} token(s)",
            body="\n\n".join(changed_msgs),
        )
    if level_msgs:
        send_email(
            subject=f"Price alert: {len(level_msgs)} level(s) crossed",
            body="\n".join(level_msgs),
        )

    save_state(state)


def check_symbols(cfg):
    ok, missing = [], []
    for symbol in cfg["tokens"]:
        df, exchange_id = fetch_ohlcv_with_fallback(
            symbol, cfg["timeframe"], limit=30,
            exchanges_priority=cfg["exchanges_priority"],
        )
        if df is None:
            missing.append(symbol)
        else:
            ok.append(f"{symbol} -> {exchange_id}")
    print("\nResolved:")
    for line in ok:
        print(" ", line)
    if missing:
        print("\nNOT FOUND on any configured exchange:")
        for s in missing:
            print(" ", s)
        print(
            "\nFor these, check the exact ticker on the exchange's website "
            "and update config/tokens_config.json."
        )
    else:
        print("\nAll symbols resolved.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run a single pass and exit")
    parser.add_argument(
        "--check-symbols", action="store_true",
        help="Just verify every configured symbol resolves on an exchange, then exit",
    )
    args = parser.parse_args()

    cfg = load_config()

    if args.check_symbols:
        check_symbols(cfg)
        return

    state = load_state()

    if args.once:
        run_pass(cfg, state)
        return

    interval = cfg["check_interval_minutes"] * 60
    log(f"Starting signal engine. Checking every {cfg['check_interval_minutes']} min. Ctrl+C to stop.")
    while True:
        try:
            run_pass(cfg, state)
        except Exception as e:  # noqa: BLE001 - never let one bad pass kill the loop
            log(f"Pass failed: {e}")
        time.sleep(interval)


if __name__ == "__main__":
    main()
