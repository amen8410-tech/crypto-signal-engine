#!/usr/bin/env python3
"""
backtest.py

One-time historical test of the exact RSI/MACD entry-stop-target logic used
in crypto_signal_engine.py and the Signal Board app. Pulls up to a year of
DAILY candles per token via ccxt (not limited to 50 candles like the live
MCP connector), simulates every trade the rule would have taken, and
reports win rate, average return, and max drawdown -- per token and
combined.

This does NOT need to run continuously. Run it once, read the results, done.

Usage:
    pip install ccxt pandas --break-system-packages
    python backtest.py
    python backtest.py --days 180          # shorter window
    python backtest.py --timeframe 4h       # finer granularity (needs fewer days of ccxt history per candle, more candles)
"""

import argparse
import json
from pathlib import Path

import ccxt
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config" / "tokens_config.json"

# ccxt spot symbols differ slightly from the Crypto.com perp-style names
# used in the live signal board (e.g. HBARUSD vs HBAR/USDT). This maps
# your token list to standard ccxt spot pairs on a liquid exchange.
DEFAULT_EXCHANGES = ["binance", "kucoin", "okx", "gateio", "mexc"]


def load_config():
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def to_ccxt_symbol(token_symbol):
    """Config stores e.g. 'HBAR/USDT' already -- pass through as-is."""
    return token_symbol


def fetch_history(symbol, timeframe, days, exchanges):
    limit = None
    if timeframe == "1d":
        limit = days
    elif timeframe == "4h":
        limit = days * 6
    elif timeframe == "1h":
        limit = days * 24
    else:
        limit = days

    last_err = None
    for exchange_id in exchanges:
        try:
            ex = getattr(ccxt, exchange_id)({"enableRateLimit": True})
            ex.load_markets()
            if symbol not in ex.markets:
                continue
            raw = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=min(limit, 1000))
            if not raw or len(raw) < 60:
                continue
            df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
            return df, exchange_id
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
    if last_err:
        print(f"  (last error: {last_err})")
    return None, None


def compute_rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-12)
    return 100 - (100 / (1 + rs))


def compute_macd_hist(close, fast=12, slow=26, signal=9):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line - signal_line


def simulate(df, swing_lookback=20, rr=2.0, rsi_oversold=30, rsi_overbought=70):
    """Walk forward bar by bar, exactly mirroring the live signal logic,
    and simulate entering/exiting trades. Returns a list of closed trades."""

    close = df["close"]
    high = df["high"]
    low = df["low"]
    rsi = compute_rsi(close)
    hist = compute_macd_hist(close)

    trades = []
    open_trade = None  # {"side": "long"/"short", "entry", "stop", "target", "entry_idx"}

    warmup = 35  # ema/macd/rsi settle time
    for i in range(warmup, len(df)):
        price = close.iloc[i]
        rsi_now = rsi.iloc[i]
        hist_now, hist_prev = hist.iloc[i], hist.iloc[i - 1]
        bull_cross = hist_prev <= 0 < hist_now
        bear_cross = hist_prev >= 0 > hist_now
        bullish_bias = bull_cross or (hist_now > 0 and rsi_now < 60)
        bearish_bias = bear_cross or (hist_now < 0 and rsi_now > 40)

        lookback_high = high.iloc[max(0, i - swing_lookback):i].max()
        lookback_low = low.iloc[max(0, i - swing_lookback):i].min()

        # -- manage an open trade first: check stop/target hit on this bar --
        if open_trade:
            side = open_trade["side"]
            if side == "long":
                if low.iloc[i] <= open_trade["stop"]:
                    open_trade["exit"] = open_trade["stop"]
                    open_trade["outcome"] = "stop"
                    trades.append(open_trade)
                    open_trade = None
                elif high.iloc[i] >= open_trade["target"]:
                    open_trade["exit"] = open_trade["target"]
                    open_trade["outcome"] = "target"
                    trades.append(open_trade)
                    open_trade = None
            else:  # short
                if high.iloc[i] >= open_trade["stop"]:
                    open_trade["exit"] = open_trade["stop"]
                    open_trade["outcome"] = "stop"
                    trades.append(open_trade)
                    open_trade = None
                elif low.iloc[i] <= open_trade["target"]:
                    open_trade["exit"] = open_trade["target"]
                    open_trade["outcome"] = "target"
                    trades.append(open_trade)
                    open_trade = None

        # -- open a new trade only if flat --
        if not open_trade:
            if bullish_bias and rsi_now < rsi_overbought:
                stop = lookback_low
                risk = price - stop
                if risk > 0:
                    target = price + risk * rr
                    open_trade = {
                        "side": "long", "entry": price, "stop": stop,
                        "target": target, "entry_idx": i,
                    }
            elif bearish_bias and rsi_now > rsi_oversold:
                stop = lookback_high
                risk = stop - price
                if risk > 0:
                    target = price - risk * rr
                    open_trade = {
                        "side": "short", "entry": price, "stop": stop,
                        "target": target, "entry_idx": i,
                    }

    # close any still-open trade at the last available price (mark-to-market)
    if open_trade:
        open_trade["exit"] = close.iloc[-1]
        open_trade["outcome"] = "open_at_end"
        trades.append(open_trade)

    return trades


def summarize(trades, symbol):
    if not trades:
        return {
            "symbol": symbol, "count": 0, "win_rate": None,
            "avg_return_pct": None, "total_return_pct": None, "max_drawdown_pct": None,
        }

    returns = []
    for t in trades:
        if t["side"] == "long":
            r = (t["exit"] - t["entry"]) / t["entry"]
        else:
            r = (t["entry"] - t["exit"]) / t["entry"]
        returns.append(r)

    wins = sum(1 for r in returns if r > 0)
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for r in returns:
        equity *= (1 + r)
        peak = max(peak, equity)
        dd = (equity - peak) / peak
        max_dd = min(max_dd, dd)

    return {
        "symbol": symbol,
        "count": len(trades),
        "win_rate": round(100 * wins / len(trades), 1),
        "avg_return_pct": round(100 * sum(returns) / len(returns), 2),
        "total_return_pct": round(100 * (equity - 1), 2),
        "max_drawdown_pct": round(100 * max_dd, 2),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=365, help="How many days of history to pull (default 365)")
    parser.add_argument("--timeframe", default="1d", choices=["1d", "4h", "1h"])
    parser.add_argument("--rr", type=float, default=2.0, help="Risk:reward ratio (default 2.0)")
    args = parser.parse_args()

    cfg = load_config()
    all_results = []
    all_trades = []

    print(f"Backtesting {len(cfg['tokens'])} tokens over ~{args.days} days ({args.timeframe} candles)...\n")

    for token in cfg["tokens"]:
        symbol = to_ccxt_symbol(token)
        df, exchange_id = fetch_history(symbol, args.timeframe, args.days, DEFAULT_EXCHANGES)
        if df is None:
            print(f"{token:12s} SKIPPED (not found / insufficient history on {DEFAULT_EXCHANGES})")
            continue

        trades = simulate(df, rr=args.rr)
        result = summarize(trades, token)
        all_results.append(result)
        all_trades.extend(trades)

        if result["count"] == 0:
            print(f"{token:12s} 0 trades in this window (source: {exchange_id})")
        else:
            print(
                f"{token:12s} {result['count']:3d} trades | "
                f"win rate {result['win_rate']:5.1f}% | "
                f"avg return {result['avg_return_pct']:+6.2f}% | "
                f"total {result['total_return_pct']:+7.2f}% | "
                f"max drawdown {result['max_drawdown_pct']:6.2f}%  "
                f"(source: {exchange_id})"
            )

    # combined stats across every token/trade
    combined = summarize(all_trades, "ALL TOKENS COMBINED")
    print("\n" + "=" * 78)
    if combined["count"] == 0:
        print("No trades were generated across any token in this window.")
    else:
        print(
            f"{'COMBINED':12s} {combined['count']:3d} trades | "
            f"win rate {combined['win_rate']:5.1f}% | "
            f"avg return {combined['avg_return_pct']:+6.2f}% | "
            f"total {combined['total_return_pct']:+7.2f}% | "
            f"max drawdown {combined['max_drawdown_pct']:6.2f}%"
        )
    print("=" * 78)
    print(
        "\nReminder: past performance on historical data does not guarantee\n"
        "future results. Small trade counts per token mean single trades can\n"
        "swing the win rate a lot -- weight the COMBINED row more than any\n"
        "single token's row unless a token has 20+ trades on its own."
    )


if __name__ == "__main__":
    main()
