# XAUUSD M15 Auto-Trading Bot (MetaTrader 5)

A fully automated gold trading bot for MetaTrader 5. It analyzes market
structure on the 15-minute chart, trades only with the trend, and manages the
account with a daily profit target and a hard drawdown guard.

## How your rules map to the bot

| # | Rule | Implementation |
|---|------|----------------|
| 1 | 15-min timeframe | All analysis on closed M15 candles |
| 2 | XAUUSD | `config.py` → `symbol` |
| 3 | 5% daily target | Day-start equity recorded; at +5% all trades close and the bot stops until the next day |
| 4 | Full risk care | Fixed % risk per trade, SL on every order, breakeven + trailing stop, drawdown guard |
| 5 | Auto market analysis | EMA 50/200, ADX trend strength, ATR volatility, swing detection — every candle |
| 6 | Follow the trend | Trades only when market structure AND EMAs agree on direction |
| 7 | Few trades | Max 3 trades/day, max 1 open position (configurable) |
| 8 | Stop after target | `TARGET_DONE` mode blocks all new entries until the next day |
| 9 | High-quality strategy | Smart-Money-Concept structure + trend + momentum confluence |
| 10 | Lot size from equity | Volume computed so the stop-loss risks exactly the configured % of live equity |
| 11 | 10% loss → observe → recover | `OBSERVE` mode (waits N bars), then `RECOVERY` mode at half risk until equity is back, then normal |
| 12 | Market structure | Swing highs/lows, HH/HL vs LL/LH classification |
| 13 | Break of structure | Entry trigger = candle close beyond the last confirmed swing |
| 14 | Market up → BUY only | Counter-trend signals are rejected |
| 15 | Market down → SELL only | Counter-trend signals are rejected |

## Sideways-market lockout

The bot does **no work at all** in a ranging market. Three independent
detectors each have veto power — if any one says "range", there is no trade:

1. **Choppiness Index** above 55 → sideways, locked out.
2. **EMA compression** — EMA50 and EMA200 tangled within 0.3 ATR of each
   other → flat market, locked out.
3. **Price box** — the last 9 hours compressed into less than 5 ATR of total
   range → consolidation box, locked out.

On top of that, ADX must be above 25 and the M15 structure, the EMAs and the
H1 trend must all agree on a direction before any entry is considered.

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
  least 2x the risk. At +1R the stop moves to breakeven, then trails 2 ATR
  behind price.

## Fakeout & news-spike protection

Every breakout must pass ALL of these gates before a trade opens:

1. **Strong body** — the breakout candle's body must be at least 40% of its
   range. A long wick with a tiny body is a classic fakeout and is rejected.
2. **Real close-through** — the close must clear the broken level by at least
   0.1 ATR. Paper-thin breaks are rejected.
3. **Volume confirmation** — breakout volume must be above its 20-bar average.
   A breakout nobody participated in is a trap.
4. **Burned-level memory** — if the same level was already broken and reclaimed
   in the last 30 bars (a proven fakeout), the bot refuses to trade it again.
5. **No chasing** — if price already ran more than 1 ATR past the level, the
   entry is skipped; the good price is gone.
6. **Spike detector** — any candle wider than 2.5 ATR (news shock, flash move)
   freezes new entries for 6 bars (1.5 hours) until the market settles.
7. **News blackout windows** — no entries during configured server-time windows
   (defaults cover the usual 15:30 / 17:00 US high-impact releases on UTC+3
   brokers; adjust `blackout_windows` in `config.py` to your broker's timezone).
8. **H1 agreement** — the hourly timeframe trend (EMA 20/50) must point the
   same way as the M15 signal. No fighting the bigger picture.
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
