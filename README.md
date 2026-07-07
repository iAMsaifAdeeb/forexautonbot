# Gold Genious — XAUUSD M5 Auto-Trading Bot (MetaTrader 5)

A fully automated gold trading bot for MetaTrader 5. It analyzes market
structure on the 5-minute chart, trades only with the trend, and manages the
account with a daily profit target and a hard drawdown guard.

## How your rules map to the bot

| # | Rule | Implementation |
|---|------|----------------|
| 1 | 5-min timeframe | All analysis on closed M5 candles, confirmed on M30 |
| 2 | XAUUSD | `config.py` → `symbol` |
| 3 | 5% daily target | Day-start equity recorded; at +5% all trades close and the bot stops until the next day |
| 4 | Full risk care | Fixed % risk per trade, SL on every order, breakeven + trailing stop, drawdown guard |
| 5 | Auto market analysis | EMA 50/200, ADX trend strength, ATR volatility, swing detection — every candle |
| 6 | Follow the trend | Trades only when market structure AND EMAs agree on direction |
| 7 | Only trades with reason | Unlimited trades until the daily target, but every one needs full confluence — no reason, no trade. One position at a time. (A hard cap is available in the Control Panel if wanted.) |
| 8 | Stop after target | `TARGET_DONE` mode blocks all new entries until the next day |
| 9 | High-quality strategy | Smart-Money-Concept structure + trend + momentum confluence |
| 10 | Lot size from equity | Volume computed so the stop-loss risks exactly the configured % of live equity |
| 11 | 10% loss → observe → recover | `OBSERVE` mode (waits N bars), then `RECOVERY` mode at half risk until equity is back, then normal |
| 12 | Market structure | Swing highs/lows, HH/HL vs LL/LH classification |
| 13 | Break of structure | Entry trigger A = candle close beyond the last confirmed swing; trigger B = pullback continuation (buy the dip / sell the rally with the trend) |
| 14 | Market up → BUY only | Counter-trend signals are rejected |
| 15 | Market down → SELL only | Counter-trend signals are rejected |

## Loss guards — the full protection stack

| Guard | What it does |
|-------|--------------|
| Daily loss guard | Day P/L hits **-3%** → the bot does NOT stop for the day: it watches the market for 2 hours, then returns in **recovery mode** (half risk) to win the loss back and keep pushing for the target |
| Profit lock | Day peaked at **+2% or more** and gave half back → 2-hour cooldown to protect the gains, then trading continues toward the target (re-arms only after a new peak) |
| Loss-streak cooldown | **3 losses in a row** → 2-hour pause; the market clearly isn't cooperating |
| 10% drawdown guard | Observe, then half-risk recovery mode until fully recovered |
| Spread guard | Entry refused when the spread is blown out (news, rollover) |
| Margin guard | Entry refused if it would strain free margin |
| Weekend protection | All positions closed Friday evening — never hold gold over the weekend gap |
| SL/TP guarantee | No order exists without both, verified broker-side after every fill |

## AI confidence engine

Every setup that passes the hard gates is then **scored 0–100** across five
quality dimensions: trend strength (ADX), trend cleanliness (choppiness),
breakout conviction (candle body), participation (volume vs average), and
room to run (RSI headroom).

- Score below **50** → the bot logs "watching, not trading" and skips it.
- Score **50–79** → normal trade at the standard risk (1%).
- Score **80+** → an exceptional setup earns a larger position (1.5% risk).
  This is the disciplined version of "size up when very sure" — no
  martingale, no doubling, always a fixed cap and always with SL/TP.

## Sideways-market lockout

The bot does **no work at all** in a ranging market. Three independent
detectors each have veto power — if any one says "range", there is no trade:

1. **Choppiness Index** above 58 → sideways, locked out.
2. **EMA compression** — EMA50 and EMA200 tangled within 0.3 ATR of each
   other → flat market, locked out.
3. **Price box** — the last 5 hours compressed into less than 5 ATR of total
   range → consolidation box, locked out.

On top of that, ADX must be above 20 and the market must confirm a direction
before any entry is considered.

## Top-down analysis — the pro pre-trade routine

Before every entry decision the bot runs the exact flow a professional
trader follows:

1. **D1** — the previous completed daily candle: which side owned the day?
2. **H4** — are the 4-hour candles trending? (close vs EMA20 + slope)
3. **H1** — is the hourly aligned? (close vs EMA20 vs EMA50 stack)

Each timeframe votes bullish / bearish / unclear, giving a directional bias
when at least 2 of 3 agree. This bias works as a **VETO, not a requirement**:
the M5 market structure decides the trade (HH/HL = buy the retest, LL/LH =
sell the retest), and the top-down bias only blocks the trade when the
higher timeframes clearly point the OPPOSITE way. Mixed higher timeframes
never stop a clean M5 structure trade — the bot never fights the daily
picture, but it also never sleeps through an M5 trend.

## Two entry triggers (why the bot now trades more)

Once the trend is confirmed on both timeframes, EITHER trigger opens a trade:

- **A — Break of Structure:** a candle closes beyond the last confirmed swing
  high/low (with all fakeout gates below).
- **B — Retest entry:** price pulled back against the trend (a dip / rally
  or a touch of the EMA50 zone) within the last few bars, then a conviction
  candle resumed the trend by closing beyond the previous bar's extreme.
  This is the core rule: **buy the retest in an uptrend (HH/HL), sell the
  retest in a downtrend (LL/LH)** — never sell a rising market from the top,
  never buy a falling market from the bottom.

## Every trade is bracketed — no exceptions

- An order without a stop-loss or take-profit is **refused before it ever
  reaches the broker** (hard guard in the execution layer).
- SL/TP geometry is validated per direction (BUY: SL below TP; SELL: SL above).
- After every fill the bot **verifies on the broker side** that the position
  actually carries its SL and TP. If the broker stripped them, it re-attaches
  them immediately — and if that fails, it closes the position rather than
  hold an unprotected trade.
- SL placement: beyond the last confirmed swing (+0.5 ATR buffer), falling
  back to a 2-ATR volatility stop if the swing is too far. TP is always at
  least 2x the risk.

### The protection ladder (how winners stay winners)

| Stage | Trigger | Stop moves to |
|-------|---------|---------------|
| 1 | +0.5R | Halfway to entry — remaining risk cut in half |
| 2 | +1.0R | Breakeven + small buffer (spread can't turn it red) |
| 3 | +1.5R | +0.5R locked — the trade is now guaranteed profitable |
| 4 | beyond | Trails behind the last swing low/high (market structure) AND a 2-ATR line, whichever is tighter — but never closer than 0.5 ATR to price |
| — | 20 bars below +0.5R | Time stop: dead trades are closed before they drift into the stop-loss |

## Fakeout & news-spike protection

Every breakout must pass ALL of these gates before a trade opens:

1. **Strong body** — the breakout candle's body must be at least 35% of its
   range. A long wick with a tiny body is a classic fakeout and is rejected.
2. **Real close-through** — the close must clear the broken level by at least
   0.1 ATR. Paper-thin breaks are rejected.
3. **Volume confirmation** — breakout volume must be above its 20-bar average.
   A breakout nobody participated in is a trap.
4. **Burned-level memory** — if the same level was already broken and reclaimed
   in the last 60 bars (a proven fakeout), the bot refuses to trade it again.
5. **No chasing** — if price already ran more than 1 ATR past the level, the
   entry is skipped; the good price is gone.
6. **Spike detector** — any candle wider than 2.5 ATR (news shock, flash move)
   freezes new entries for 18 bars (1.5 hours) until the market settles.
7. **News blackout windows** — no entries during configured server-time windows
   (defaults cover the usual 15:30 / 17:00 US high-impact releases on UTC+3
   brokers; adjust `blackout_windows` in `config.py` to your broker's timezone).
8. **M30 agreement** — the 30-minute timeframe trend (EMA 20/50) must point the
   same way as the M5 signal. No fighting the bigger picture.
9. **Exhaustion filter** — RSI above 80 blocks buys, below 20 blocks sells,
   so the bot never buys a parabolic top or sells a capitulation bottom.

## Requirements

- Windows with **MetaTrader 5 terminal** installed and logged in to your broker
  (a broker that offers XAUUSD, e.g. any major MT5 broker)
- **Python 3.10+** (64-bit, matching your MT5 terminal)

## Setup

```bash
pip install -r requirements.txt
```

1. Open MetaTrader 5, log in to your account, and make sure **XAUUSD** is
   visible in Market Watch.
2. Enable algorithmic trading: *Tools → Options → Expert Advisors → Allow
   algorithmic trading*.
3. (Optional) Put your login/server in `config.py` if you want the bot to log
   in by itself; otherwise it uses the account already open in the terminal.

## Run

**Easiest way — the Control Panel app.** Double-click
`Gold Genious.exe` (or run `python control_panel.py`). It gives you
a window where you can:

- edit every important setting (risk %, daily target, symbol, trading hours,
  news blackouts, MT5 login) and hit **Save Settings** — values are written to
  `settings.json` and applied the next time the bot starts;
- **Start / Stop** the bot with one click;
- watch the **live log** and current state (mode, trades today) in real time.

The panel also has an update button (**⟳**): one click downloads the complete
repository from GitHub and replaces **every** file A–Z (not just changed
ones), then installs any new dependencies — so grand changes, new modules and
renames always arrive intact. It works on any fresh VPS. Your
`settings.json`, `bot_state.json` and logs are never touched by an update.

To rebuild the exe after code changes, run `build_exe.bat`.

### Deploying on a VPS (one click)

1. Install **Python 3.12** (recommended) or 3.13 from python.org — tick **Add to PATH**.
2. Install **MetaTrader 5** from your broker; log in and enable Algo Trading.
3. Copy the project folder to the VPS (e.g. `C:\forexautobot`) — or download once
   with the ZIP method below.
4. Double-click **`SETUP.bat`** — it will:
   - download the **latest version of every file** from GitHub,
   - install Python packages (pre-built wheels — no compiler needed),
   - create a **Gold Genious** icon on your Desktop.
5. Double-click the Desktop icon to open the panel, enter account details, **START BOT**.

To update later: run **`SETUP.bat`** again — same steps, always fresh code.

**First-time download (if the folder is empty):**
```powershell
New-Item -ItemType Directory -Force -Path "C:\forexautobot" | Out-Null
Set-Location "C:\forexautobot"
Invoke-WebRequest -Uri "https://github.com/iAMsaifAdeeb/forexautonbot/archive/refs/heads/main.zip" -OutFile "repo.zip"
Expand-Archive "repo.zip" -DestinationPath "." -Force
Copy-Item "forexautonbot-main\*" . -Recurse -Force
Remove-Item "forexautonbot-main","repo.zip" -Recurse -Force
.\SETUP.bat
```

**Note:** use `SETUP.bat` / Desktop icon on VPS — not the `.exe`. The exe bundles its
own Python and often breaks on servers; the bat/vbs launcher uses your system Python
and is reliable.

**Or run the bot directly:**

```bash
python main.py
```

The bot logs every decision to the console and to `bot.log`, and stores its
daily state (target reached, drawdown mode, trades taken) in `bot_state.json`
so it survives restarts.

## Tuning

Everything lives in `config.py`:

- `risk_per_trade_pct` — % of equity risked per trade (default 1%)
- `daily_target_pct` / `max_drawdown_pct` — the 5% / 10% rules
- `max_trades_per_day`, `adx_min`, `trading_hours`, etc.

## IMPORTANT — read before going live

- **Test on a demo account first.** Run it for at least 2–4 weeks on demo
  before risking real money.
- **No bot can guarantee 5% per day.** 5% daily compounds to ~250,000% per
  year — nothing on earth returns that. Here, 5% is a *stop-trading target*
  that protects profits, not a promise. Some days the bot will not trade at
  all (no valid setup = no trade, which is correct behavior).
- Trading leveraged gold is high risk. Only trade money you can afford to lose.
