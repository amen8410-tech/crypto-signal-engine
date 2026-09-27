# Crypto Signal Engine

Watches your token list using **public market data only** (no exchange API
keys, no trading permissions needed) and emails you when:

1. RSI/MACD flips into a new **spot buy / long / short** setup, or
2. Price crosses a level you set.

Each alert includes an entry, stop-loss, and take-profit — not just "signal
fired."

## Setup

```bash
pip install ccxt pandas python-dotenv --break-system-packages
cp .env.example .env
# edit .env with your SMTP details (same pattern as your BloFin/Bitrue bots)
```

First, confirm every token resolves on one of your configured exchanges:

```bash
python crypto_signal_engine.py --check-symbols
```

`NIGHT` (Midnight Network) and `HYPE` (Hyperliquid) are newer/smaller-cap
tickers — if either comes back "not found," check the exact symbol on the
exchange's site and fix it in `config/tokens_config.json`.

Then run continuously on your always-on laptop:

```bash
python crypto_signal_engine.py
```

Or run one check and exit (useful for testing, or wiring into cron/Task
Scheduler instead of an infinite loop):

```bash
python crypto_signal_engine.py --once
```

## Editing your token list

Open `config/tokens_config.json` and add/remove lines in `"tokens"`. No code
changes needed. Format is `"SYMBOL/USDT"`.

## Adding a price alert

In the same file, under `"price_alerts"`, set `"above"` and/or `"below"` for
any token:

```json
"UNI/USDT": { "above": 12.00, "below": 6.50 }
```

Each level fires once when crossed, then re-arms if price crosses back the
other way.

## How the plan is built

- **RSI (14)** and **MACD (12/26/9)** on 1h candles (configurable)
- **Long / spot buy** bias: MACD bullish crossover, or histogram positive
  with RSI not yet overbought
- **Short** bias: MACD bearish crossover, or histogram negative with RSI not
  yet oversold
- **Stop-loss**: recent swing low (longs) / swing high (shorts) over the
  last 20 candles (configurable)
- **Take-profit**: entry ± (risk × risk_reward_ratio), default 2:1

This is the same RSI/MACD logic as your `crypto-signals.jsx` dashboard,
extended with stop/target math and spot-vs-long-vs-short separation.

## Running it 24/7 without a laptop (GitHub Actions)

This repo already includes `.github/workflows/signal-engine.yml`, which makes
GitHub's own servers run the check on a schedule -- your computer can be off,
asleep, or a thousand miles away.

### 1. Create the repo

1. Go to github.com and sign in (or create a free account).
2. Click **+** (top right) -> **New repository**.
3. Name it anything (e.g. `crypto-signal-engine`).
4. **Public vs Private:**
   - **Public** -> GitHub Actions minutes are unlimited/free. Your token list
     and code are visible to anyone, but nothing sensitive (no keys, no
     credentials) is in the code -- those live in step 3 below.
   - **Private** -> Free plans get ~2000 Actions minutes/month. At the
     included 30-minute schedule this comfortably fits. If you switch the
     cron to every 15 minutes on a private repo, keep an eye on usage under
     Settings -> Billing.
5. Click **Create repository**.

### 2. Upload these files

Easiest way (no git experience needed): on your new repo's page, click
**"Add file" -> "Upload files"**, drag in this entire unzipped folder
(keeping the `.github/workflows/` and `config/` subfolders intact), and
commit.

(If you're comfortable with git instead:)
```bash
cd crypto_signal_engine
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```

### 3. Add your email credentials as GitHub Secrets

These replace the `.env` file for the cloud version -- GitHub encrypts them,
your code never sees the raw values checked into the repo.

Go to your repo -> **Settings** -> **Secrets and variables** -> **Actions**
-> **New repository secret**, and add each of these one at a time:

| Name | Value |
|---|---|
| `SMTP_SERVER` | e.g. `smtp.gmail.com` |
| `SMTP_PORT` | e.g. `587` |
| `SMTP_USER` | your email |
| `SMTP_PASSWORD` | your Gmail app password |
| `ALERT_EMAIL_TO` | where alerts should go |

### 4. Test it

Go to the **Actions** tab -> click **Crypto Signal Engine** on the left ->
**Run workflow** button -> **Run workflow**. Watch it go from yellow (running)
to green (success). Click into it to see the same log output you'd see
locally. If a signal or price alert fired, check your email.

### 5. Let it run

That's it -- it now runs automatically every 30 minutes, forever, with no
device of yours involved. To change how often, edit the `cron:` line in
`.github/workflows/signal-engine.yml` (times are UTC).

### If something goes wrong

- Red X instead of green check -> click into the run -> expand the failing
  step to see the actual Python error.
- No emails ever arriving -> double check the 5 secrets are spelled exactly
  as above (typos are the #1 cause) and that you used a Gmail **app
  password**, not your normal password.
- Workflow doesn't appear under Actions at all -> make sure the file is at
  exactly `.github/workflows/signal-engine.yml` (GitHub only looks in that
  folder).

## Backtesting before you trust it (recommended first step)

Before relying on any of this for real position sizing, run `backtest.py`.
It's a **one-time** script -- it doesn't need to run continuously, so your
laptop being off most of the time is not a problem. Run it whenever you
happen to be on the laptop.

```bash
python backtest.py                    # ~1 year of daily candles, all 13 tokens
python backtest.py --days 180         # shorter window
python backtest.py --timeframe 4h     # finer granularity
```

It pulls real historical price data via `ccxt` (not limited to the 50-candle
cap the live Signal Board app has), replays the exact same RSI/MACD rule bar
by bar, and reports for each token:

- **Trade count** -- how many signals actually fired
- **Win rate** -- % that hit take-profit before stop-loss
- **Average return per trade**
- **Total return** (compounded, 1 unit position size)
- **Max drawdown**

Read the **COMBINED** row at the bottom as the more reliable number --
any single token with under ~20 trades can have its win rate swung hard by
one or two lucky/unlucky trades.

**What to actually do with the result:**
- Win rate meaningfully above ~50% with a positive average return across
  the combined row -> the rule has some edge, worth using as one input
- Win rate near 50/50 or negative average return -> the rule alone isn't
  making money here; treat live signals as informational only, not a
  reason to trade
- Either way, this only tells you what would have happened historically --
  it's not a promise about the future

## Notes

- **Spot** mode only ever suggests buy zones — no shorting, since you can't
  short on spot.
- **Long/short** are meant for your futures/perp accounts (BloFin etc.) —
  this script does NOT place trades, it only tells you the plan. You still
  pull the trigger.
- Data source priority is `kucoin → gateio → mexc → okx → binance` (all
  public endpoints, no login). Reorder in the config if you'd rather it try
  a specific exchange first.
- State (so you don't get the same alert every 15 minutes) lives in
  `state/last_state.json`. Delete it to reset.
