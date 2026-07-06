"""
Offline self-test — no MT5 terminal needed.
Validates indicators, market structure detection, signal generation,
and risk-manager state transitions with synthetic data.
Run: python test_logic.py
"""

import os
import numpy as np
import pandas as pd

from config import CONFIG
from indicators import add_indicators
import market_structure as ms
import strategy
from risk_manager import (
    RiskManager, MODE_NORMAL, MODE_TARGET_DONE, MODE_OBSERVE, MODE_RECOVERY,
)

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def make_trend_df(n=400, direction="up", seed=42):
    """Synthetic stair-stepping trend with realistic candles:
    open = previous close, wicks proportional to the move, and volume that
    rises on impulse bars (as in real markets)."""
    rng = np.random.default_rng(seed)
    sign = 1 if direction == "up" else -1
    price = 2400.0
    opens, highs, lows, closes, volumes = [], [], [], [], []
    for i in range(n):
        cycle = i % 10
        push = cycle < 6                       # 6 bars push, 4 bars pull back
        step = 3.0 if push else -1.2
        o = price
        c = price + sign * step + rng.normal(0, 0.3)
        wick = abs(rng.normal(0.4, 0.15))
        opens.append(o)
        closes.append(c)
        highs.append(max(o, c) + wick)
        lows.append(min(o, c) - wick)
        volumes.append(int(rng.normal(850 if push else 300, 60)))
        price = c
    return pd.DataFrame({
        "time": pd.date_range("2026-07-06 08:00", periods=n, freq="15min"),
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "tick_volume": volumes,
    })


print("--- market structure ---")
up_df = make_trend_df(direction="up")
st = ms.analyze(up_df, CONFIG["swing_lookback"])
check("uptrend classified", st.trend == ms.UPTREND, f"got {st.trend}")
check("swings detected", len(st.swings) > 10, f"got {len(st.swings)}")

down_df = make_trend_df(direction="down")
st2 = ms.analyze(down_df, CONFIG["swing_lookback"])
check("downtrend classified", st2.trend == ms.DOWNTREND, f"got {st2.trend}")

print("--- indicators ---")
ind = add_indicators(up_df, CONFIG)
check("ema_fast rising", ind["ema_fast"].iloc[-1] > ind["ema_fast"].iloc[-50])
check("atr positive", ind["atr"].iloc[-1] > 0)
check("adx in range", 0 <= ind["adx"].iloc[-1] <= 100)

print("--- strategy signals ---")
# Scan the synthetic uptrend bar by bar; a BOS entry should fire at least once
buy_signals = sell_signals = 0
for end in range(250, len(up_df)):
    window = add_indicators(up_df.iloc[:end + 1].reset_index(drop=True), CONFIG)
    sig, _ = strategy.evaluate(window, CONFIG)
    if sig:
        if sig.direction == "BUY":
            buy_signals += 1
        else:
            sell_signals += 1
check("buy signals fire in uptrend", buy_signals > 0, f"got {buy_signals}")
check("NO sell signals in uptrend (rule 14)", sell_signals == 0, f"got {sell_signals}")

down_ind = None
buy2 = sell2 = 0
for end in range(250, len(down_df)):
    window = add_indicators(down_df.iloc[:end + 1].reset_index(drop=True), CONFIG)
    sig, _ = strategy.evaluate(window, CONFIG)
    if sig:
        if sig.direction == "SELL":
            sell2 += 1
        else:
            buy2 += 1
check("sell signals fire in downtrend", sell2 > 0, f"got {sell2}")
check("NO buy signals in downtrend (rule 15)", buy2 == 0, f"got {buy2}")

# signal geometry
window = add_indicators(up_df, CONFIG)
sig = None
for end in range(250, len(up_df)):
    w = add_indicators(up_df.iloc[:end + 1].reset_index(drop=True), CONFIG)
    sig, _ = strategy.evaluate(w, CONFIG)
    if sig:
        break
if sig:
    check("buy SL below entry", sig.stop_loss < sig.entry_hint)
    check("buy TP above entry", sig.take_profit > sig.entry_hint)
    rr = (sig.take_profit - sig.entry_hint) / (sig.entry_hint - sig.stop_loss)
    check("reward:risk >= 2", rr >= CONFIG["min_reward_risk"] - 0.01, f"rr={rr:.2f}")

print("--- sideways market lockout ---")

def make_sideways_df(n=400, seed=7):
    """Price oscillating in a band — the bot must never trade this."""
    rng = np.random.default_rng(seed)
    center = 2400.0
    opens, highs, lows, closes, volumes = [], [], [], [], []
    price = center
    for i in range(n):
        target = center + 6.0 * np.sin(2 * np.pi * i / 24)   # 6$-wide slow wave
        o = price
        c = target + rng.normal(0, 1.2)
        wick = abs(rng.normal(0.5, 0.2))
        opens.append(o)
        closes.append(c)
        highs.append(max(o, c) + wick)
        lows.append(min(o, c) - wick)
        volumes.append(int(rng.normal(500, 100)))
        price = c
    return pd.DataFrame({
        "time": pd.date_range("2026-07-06 08:00", periods=n, freq="15min"),
        "open": opens, "high": highs, "low": lows, "close": closes,
        "tick_volume": volumes,
    })

side_df = make_sideways_df()
side_signals = 0
sideways_rejections = 0
for end in range(250, len(side_df)):
    w = add_indicators(side_df.iloc[:end + 1].reset_index(drop=True), CONFIG)
    s, why = strategy.evaluate(w, CONFIG)
    if s:
        side_signals += 1
    elif "SIDEWAYS" in why or "not trending" in why or "no clear trend" in why:
        sideways_rejections += 1
check("ZERO trades in sideways market", side_signals == 0, f"got {side_signals}")
check("range explicitly detected", sideways_rejections > 50, f"got {sideways_rejections}")

chop_side = add_indicators(side_df, CONFIG)["chop"].iloc[-1]
chop_trend = add_indicators(make_trend_df(direction="up"), CONFIG)["chop"].iloc[-1]
check("choppiness higher in range than in trend", chop_side > chop_trend,
      f"range {chop_side:.0f} vs trend {chop_trend:.0f}")

print("--- SL/TP hard guarantee ---")
from trade_manager import TradeManager

class DummyClient:
    def get_tick(self):
        raise AssertionError("order should be refused before touching the market")
    def positions(self):
        return []

tm = TradeManager(CONFIG, DummyClient())
check("order without SL refused", tm.open_trade("BUY", 0.1, 0, 2450.0, "t") is False)
check("order without TP refused", tm.open_trade("BUY", 0.1, 2380.0, 0, "t") is False)
check("BUY with SL above TP refused", tm.open_trade("BUY", 0.1, 2460.0, 2450.0, "t") is False)
check("SELL with SL below TP refused", tm.open_trade("SELL", 0.1, 2380.0, 2390.0, "t") is False)

print("--- fakeout & spike protection ---")

def first_signal_window(df):
    """Return (window_end_index, analyzed_window) of the first bar that fires."""
    for end in range(250, len(df)):
        w = add_indicators(df.iloc[:end + 1].reset_index(drop=True), CONFIG)
        s, _ = strategy.evaluate(w, CONFIG)
        if s:
            return end, df.iloc[:end + 1].reset_index(drop=True)
    return None, None

sig_end, raw_win = first_signal_window(up_df)
check("baseline signal exists for filter tests", sig_end is not None)

if sig_end is not None:
    # (a) same setup but the breakout candle is a wick spike with a tiny body
    fake = raw_win.copy()
    i = len(fake) - 1
    fake.loc[i, "high"] = fake.loc[i, "close"] + 3.0     # long wick above
    fake.loc[i, "open"] = fake.loc[i, "close"] - 0.2     # almost no body
    fake.loc[i, "low"] = fake.loc[i, "open"] - 0.3
    s, why = strategy.evaluate(add_indicators(fake, CONFIG), CONFIG)
    check("wick-spike breakout rejected", s is None, why)
    check("  reason mentions fakeout", s is None and "fakeout" in why.lower(), why)

    # (b) same setup but breakout volume is dead
    fake = raw_win.copy()
    fake.loc[len(fake) - 1, "tick_volume"] = 50
    s, why = strategy.evaluate(add_indicators(fake, CONFIG), CONFIG)
    check("low-volume breakout rejected", s is None, why)

    # (c) a giant news candle a few bars before the signal -> spike cooldown
    fake = raw_win.copy()
    j = len(fake) - 3
    fake.loc[j, "high"] = fake.loc[j, "close"] + 25.0
    fake.loc[j, "low"] = fake.loc[j, "open"] - 25.0
    s, why = strategy.evaluate(add_indicators(fake, CONFIG), CONFIG)
    check("post-spike cooldown blocks entry", s is None, why)
    check("  reason mentions spike", s is None and "spike" in why.lower(), why)

    # (d) same bar but timestamped inside a news blackout window
    fake = raw_win.copy()
    base_day = fake["time"].iloc[-1].normalize()
    fake.loc[len(fake) - 1, "time"] = base_day + pd.Timedelta(hours=15, minutes=30)
    s, why = strategy.evaluate(add_indicators(fake, CONFIG), CONFIG)
    check("news blackout window blocks entry", s is None, why)
    check("  reason mentions blackout", s is None and "blackout" in why.lower(), why)

# (e) burned level detector: break above 100, fall back under, break again
closes = [98, 99, 101, 99, 98, 99, 101.5]
burn_df = pd.DataFrame({"close": closes})
check("burned level detected",
      strategy.level_burned(burn_df, 100.0, 0, "BULL", 30))
clean_df = pd.DataFrame({"close": [97, 98, 99, 98, 99, 99.5, 101.5]})
check("clean level not flagged",
      not strategy.level_burned(clean_df, 100.0, 0, "BULL", 30))

print("--- risk manager ---")
if os.path.exists("test_state.json"):
    os.remove("test_state.json")
cfg = dict(CONFIG, state_file="test_state.json")
rm = RiskManager(cfg, 10000.0)

check("starts NORMAL", rm.update(10000.0, False) == MODE_NORMAL)
check("risk pct normal", rm.current_risk_pct() == cfg["risk_per_trade_pct"])

check("+5%% -> TARGET_DONE", rm.update(10500.0, False) == MODE_TARGET_DONE)
ok, reason = rm.can_open_trade(0)
check("no trading after target", not ok, reason)

# reset for drawdown path
if os.path.exists("test_state.json"):
    os.remove("test_state.json")
rm = RiskManager(cfg, 10000.0)
rm.update(10000.0, False)
check("-10%% -> OBSERVE", rm.update(9000.0, False) == MODE_OBSERVE)
ok, _ = rm.can_open_trade(0)
check("no trading while observing", not ok)
for _ in range(cfg["observe_bars"]):
    rm.on_new_bar()
check("observe -> RECOVERY", rm.update(9000.0, False) == MODE_RECOVERY)
check("reduced risk in recovery", rm.current_risk_pct() == cfg["recovery_risk_pct"])
check("recovered -> NORMAL", rm.update(10000.0, False) == MODE_NORMAL)

# trade count limit
for _ in range(cfg["max_trades_per_day"]):
    rm.on_trade_opened()
ok, reason = rm.can_open_trade(0)
check("daily trade cap enforced", not ok, reason)

# lot sizing with a fake symbol info
class FakeSymbol:
    trade_tick_size = 0.01
    trade_tick_value = 0.01   # $0.01 per tick per 0.01-lot-normalized unit... broker-style
    volume_min = 0.01
    volume_max = 100.0
    volume_step = 0.01

if os.path.exists("test_state.json"):
    os.remove("test_state.json")
rm = RiskManager(cfg, 10000.0)
# risk 1% of 10000 = $100; SL distance $5.00 -> 500 ticks * $0.01 = $5/lot -> 20 lots
lots = rm.lot_size(10000.0, 5.0, FakeSymbol())
check("lot size formula", abs(lots - 20.0) < 1e-9, f"got {lots}")
lots2 = rm.lot_size(10000.0, 0.0, FakeSymbol())
check("zero SL -> zero lots", lots2 == 0.0)

if os.path.exists("test_state.json"):
    os.remove("test_state.json")
print(f"\n{PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
