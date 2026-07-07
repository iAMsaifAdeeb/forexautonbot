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
    if CONFIG.get("entry_mode") == "hybrid":
        pip = CONFIG.get("pip_size", 0.10)
        tp_dist = abs(sig.take_profit - sig.entry_hint)
        sl_dist = abs(sig.entry_hint - sig.stop_loss)
        check("hybrid TP at fixed pips",
              abs(tp_dist - CONFIG["hybrid_tp_pips"] * pip) < 0.05,
              f"tp_dist={tp_dist:.2f}")
        check("hybrid SL within pip budget",
              sl_dist <= CONFIG["hybrid_sl_pips"] * pip + 0.05,
              f"sl_dist={sl_dist:.2f}")
    else:
        rr = (sig.take_profit - sig.entry_hint) / (sig.entry_hint - sig.stop_loss)
        check("reward:risk >= 2", rr >= CONFIG["min_reward_risk"] - 0.01, f"rr={rr:.2f}")
    check("signal carries confidence 0-100",
          0 <= sig.confidence <= 100, f"conf={sig.confidence}")
    check("confidence above minimum gate",
          sig.confidence >= CONFIG["min_confidence"], f"conf={sig.confidence}")

# impossible confidence bar -> the same data produces zero signals
strict_cfg = dict(CONFIG, min_confidence=101, hybrid_min_confidence=101)
strict_signals = 0
for end in range(250, len(up_df)):
    w = add_indicators(up_df.iloc[:end + 1].reset_index(drop=True), strict_cfg)
    s, _ = strategy.evaluate(w, strict_cfg)
    if s:
        strict_signals += 1
check("min-confidence gate blocks all when raised", strict_signals == 0,
      f"got {strict_signals}")

# Pullback continuation trigger (structure mode only)
struct_cfg = dict(CONFIG, entry_mode="structure")
pb_cfg = dict(struct_cfg, pullback_enabled=True)
nopb_cfg = dict(struct_cfg, pullback_enabled=False)
pb_signals = nopb_signals = 0
for end in range(250, len(up_df)):
    w = add_indicators(up_df.iloc[:end + 1].reset_index(drop=True), CONFIG)
    s1, r1 = strategy.evaluate(w, pb_cfg)
    s2, r2 = strategy.evaluate(w, nopb_cfg)
    if s1 and "retest" in r1.lower():
        pb_signals += 1
    if s2 and "retest" in r2.lower():
        nopb_signals += 1
check("retest entries fire in uptrend (structure mode)", pb_signals > 0, f"got {pb_signals}")
check("pullback_enabled=False disables them", nopb_signals == 0,
      f"got {nopb_signals}")

print("--- hybrid Option B (structure + candles + fixed pips) ---")
# Explicit 3-candle alignment check (synthetic end-of-series may include pullbacks)
aligned_up = pd.DataFrame({
    "open": [2400.0, 2401.0, 2402.0],
    "close": [2401.0, 2402.0, 2403.0],
})
aligned_down = pd.DataFrame({
    "open": [2403.0, 2402.0, 2401.0],
    "close": [2402.0, 2401.0, 2400.0],
})
check("3 green candles aligned in uptrend",
      strategy.candles_aligned(aligned_up, ms.UPTREND, 3))
check("3 red candles aligned in downtrend",
      strategy.candles_aligned(aligned_down, ms.DOWNTREND, 3))
# One tiny doji pullback inside the run is fine (2 of 3 + last + net move)
doji_up = pd.DataFrame({
    "open": [2400.0, 2402.0, 2401.5],
    "close": [2402.0, 2401.5, 2404.0],
})
check("small pullback candle allowed in run",
      strategy.candles_aligned(doji_up, ms.UPTREND, 3))
check("last candle against trend not aligned",
      not strategy.candles_aligned(
          pd.DataFrame({"open": [1.0, 2.0, 4.0], "close": [2.0, 4.0, 3.0]}),
          ms.UPTREND, 3))
check("majority against trend not aligned",
      not strategy.candles_aligned(
          pd.DataFrame({"open": [3.0, 2.5, 1.5], "close": [2.5, 1.5, 2.0]}),
          ms.UPTREND, 3))
hybrid_signals = 0
for end in range(250, len(up_df)):
    w = add_indicators(up_df.iloc[:end + 1].reset_index(drop=True), CONFIG)
    s, r = strategy.evaluate(w, CONFIG)
    if s and "hybrid" in s.reason.lower():
        hybrid_signals += 1
check("hybrid buy signals in uptrend", hybrid_signals > 0, f"got {hybrid_signals}")
hybrid_sell = hybrid_buy = 0
for end in range(250, len(down_df)):
    w = add_indicators(down_df.iloc[:end + 1].reset_index(drop=True), CONFIG)
    s, r = strategy.evaluate(w, CONFIG)
    if s:
        if s.direction == "SELL":
            hybrid_sell += 1
        else:
            hybrid_buy += 1
check("hybrid sell signals in downtrend", hybrid_sell > 0, f"got {hybrid_sell}")
check("hybrid NO buy in downtrend", hybrid_buy == 0, f"got {hybrid_buy}")

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

def first_signal_window(df, eval_cfg=None):
    """Return (window_end_index, analyzed_window) of the first BOS bar that
    fires (the fakeout gates below specifically test breakout candles)."""
    cfg = eval_cfg or dict(CONFIG, entry_mode="structure")
    for end in range(250, len(df)):
        w = add_indicators(df.iloc[:end + 1].reset_index(drop=True), cfg)
        s, r = strategy.evaluate(w, cfg)
        if s and "bos" in r.lower():
            return end, df.iloc[:end + 1].reset_index(drop=True)
    return None, None

sig_end, raw_win = first_signal_window(up_df)
check("baseline signal exists for filter tests", sig_end is not None)

if sig_end is not None:
    struct_cfg = dict(CONFIG, entry_mode="structure")
    # (a) same setup but the breakout candle is a wick spike with a tiny body
    fake = raw_win.copy()
    i = len(fake) - 1
    fake.loc[i, "high"] = fake.loc[i, "close"] + 3.0     # long wick above
    fake.loc[i, "open"] = fake.loc[i, "close"] - 0.2     # almost no body
    fake.loc[i, "low"] = fake.loc[i, "open"] - 0.3
    s, why = strategy.evaluate(add_indicators(fake, struct_cfg), struct_cfg)
    check("wick-spike breakout rejected", s is None, why)
    check("  reason mentions fakeout", s is None and "fakeout" in why.lower(), why)

    # (b) same setup but breakout volume is dead
    fake = raw_win.copy()
    fake.loc[len(fake) - 1, "tick_volume"] = 50
    s, why = strategy.evaluate(add_indicators(fake, struct_cfg), struct_cfg)
    check("low-volume breakout rejected", s is None, why)

    # (c) a giant COUNTER-trend candle (red crash in the uptrend) a few bars
    # before the signal -> full spike cooldown blocks the trade
    fake = raw_win.copy()
    j = len(fake) - 3
    o, c = fake.loc[j, "open"], fake.loc[j, "close"]
    fake.loc[j, "open"] = max(o, c) + 10.0            # force a huge RED candle
    fake.loc[j, "close"] = min(o, c) - 15.0
    fake.loc[j, "high"] = fake.loc[j, "open"] + 2.0
    fake.loc[j, "low"] = fake.loc[j, "close"] - 2.0
    s, why = strategy.evaluate(add_indicators(fake, struct_cfg), struct_cfg)
    check("counter-trend spike blocks entry", s is None, why)
    check("  reason mentions spike", s is None and "spike" in why.lower(), why)

    # (c2) a giant WITH-trend candle on the LAST bar -> short calm-down only
    fake = raw_win.copy()
    i = len(fake) - 1
    fake.loc[i, "close"] = fake.loc[i, "open"] + 30.0  # huge green in uptrend
    fake.loc[i, "high"] = fake.loc[i, "close"] + 1.0
    s, why = strategy.evaluate(add_indicators(fake, struct_cfg), struct_cfg)
    check("with-trend spike on last bar asks only a short wait",
          s is None and ("waiting" in why.lower() or "spike" in why.lower()), why)

    # (c3) direction-aware gate directly: with-trend spike older than
    # spike_calm_bars must NOT block; the old behaviour blocked 18 bars.
    fake = raw_win.copy()
    j = len(fake) - 6                                   # 5 bars ago (> calm 2)
    fake.loc[j, "close"] = fake.loc[j, "open"] + 30.0   # huge green (with-trend)
    fake.loc[j, "high"] = fake.loc[j, "close"] + 1.0
    w_ind = add_indicators(fake, struct_cfg)
    check("with-trend spike 5 bars ago does not block",
          strategy.spike_gate(w_ind, ms.UPTREND, struct_cfg) is None)
    check("same spike still blocks SELL side (counter-trend)",
          strategy.spike_gate(w_ind, ms.DOWNTREND, struct_cfg) is not None)

    # (d) same bar but timestamped inside a news blackout window
    fake = raw_win.copy()
    base_day = fake["time"].iloc[-1].normalize()
    fake.loc[len(fake) - 1, "time"] = base_day + pd.Timedelta(hours=15, minutes=30)
    s, why = strategy.evaluate(add_indicators(fake, struct_cfg), struct_cfg)
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

# trade count: unlimited by default (cap = 0), enforced when a cap is set
for _ in range(25):
    rm.on_trade_opened()
ok, _r = rm.can_open_trade(0)
check("unlimited trades until target (cap=0)", ok)

if os.path.exists("test_state.json"):
    os.remove("test_state.json")
capped_cfg = dict(cfg, max_trades_per_day=2)
rm_capped = RiskManager(capped_cfg, 10000.0)
rm_capped.on_trade_opened()
rm_capped.on_trade_opened()
ok, reason = rm_capped.can_open_trade(0)
check("daily trade cap enforced when set", not ok, reason)

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

print("--- loss guards ---")
from risk_manager import MODE_OBSERVE, MODE_RECOVERY

def fresh_rm():
    if os.path.exists("test_state.json"):
        os.remove("test_state.json")
    r = RiskManager(cfg, 10000.0)
    r.update(10000.0, False)
    return r

# Daily loss guard: -3% does NOT end the day — the bot observes the market,
# then returns in RECOVERY mode to win the loss back.
rm = fresh_rm()
check("-3%% day -> OBSERVE (watch market)", rm.update(9690.0, False) == MODE_OBSERVE)
ok, reason = rm.can_open_trade(0)
check("no trading while observing", not ok, reason)
for _ in range(cfg["observe_bars"]):
    rm.on_new_bar()
check("after observing -> RECOVERY", rm.update(9690.0, False) == MODE_RECOVERY)
ok, _ = rm.can_open_trade(0)
check("trading allowed again in recovery", ok)
check("recovery risk is reduced",
      rm.current_risk_pct() == cfg["recovery_risk_pct"])
check("loss recovered -> back to NORMAL", rm.update(10000.0, False) == MODE_NORMAL)

# profit lock: peaked +2.5%, gave half back -> cooldown pause (NOT a day stop)
rm = fresh_rm()
check("day peaking +2.5%% stays NORMAL", rm.update(10250.0, False) == MODE_NORMAL)
check("giving back half -> still NORMAL mode", rm.update(10100.0, False) == MODE_NORMAL)
ok, reason = rm.can_open_trade(0)
check("profit lock pauses trading", not ok, reason)
for _ in range(cfg["loss_pause_bars"]):
    rm.on_new_bar()
ok, _ = rm.can_open_trade(0)
check("after pause, trading continues toward target", ok)
# It must NOT re-trigger without a NEW day peak
rm.update(10090.0, False)
ok, _ = rm.can_open_trade(0)
check("profit lock does not re-trigger without new peak", ok)

# small pullback does NOT trigger the lock
rm = fresh_rm()
rm.update(10250.0, False)
rm.update(10200.0, False)
ok, _ = rm.can_open_trade(0)
check("small pullback keeps trading", ok)

# consecutive-loss cooldown
rm = fresh_rm()
rm.update(9950.0, False, day_profits=[-10.0, -20.0, -15.0])
ok, reason = rm.can_open_trade(0)
check("3 losses in a row -> cooldown", not ok, reason)
for _ in range(cfg["loss_pause_bars"]):
    rm.on_new_bar()
ok, _r = rm.can_open_trade(0)
check("cooldown expires after pause bars", ok)

# a win resets the streak
rm = fresh_rm()
rm.update(9990.0, False, day_profits=[-10.0, -20.0, 30.0])
ok, _r = rm.can_open_trade(0)
check("win breaks the loss streak", ok)

# confidence-tiered risk
rm = fresh_rm()
check("normal risk for normal setups", rm.current_risk_pct(65.0) == cfg["risk_per_trade_pct"])
check("boosted risk for exceptional setups",
      rm.current_risk_pct(90.0) == cfg["high_confidence_risk_pct"])

# spread guard
class WideSpreadTick:
    ask = 2401.20
    bid = 2400.00

class WideSpreadInfo:
    point = 0.01

class WideSpreadClient:
    def get_tick(self):
        return WideSpreadTick()
    def symbol_info(self):
        return WideSpreadInfo()
    def positions(self):
        return []

tm_spread = TradeManager(CONFIG, WideSpreadClient())
check("blown-out spread refused",
      tm_spread.open_trade("BUY", 0.1, 2380.0, 2450.0, "t") is False)

print("--- basket entries (TP ladder + runner) ---")
from trade_manager import basket_take_profits, split_basket_volumes

# volume splitting
check("0.25 lots -> 5 x 0.05", split_basket_volumes(0.25, 5, 0.01, 0.01) ==
      [0.05, 0.05, 0.05, 0.05, 0.05],
      str(split_basket_volumes(0.25, 5, 0.01, 0.01)))
check("0.03 lots -> only 3 legs", split_basket_volumes(0.03, 5, 0.01, 0.01) ==
      [0.01, 0.01, 0.01], str(split_basket_volumes(0.03, 5, 0.01, 0.01)))
check("below minimum -> no basket", split_basket_volumes(0.005, 5, 0.01, 0.01) == [])
vols = split_basket_volumes(0.23, 5, 0.01, 0.01)
check("remainder goes to the runner leg",
      abs(sum(vols) - 0.23) < 1e-9 and vols[-1] >= vols[0], str(vols))

# TP ladder: entry 2400, SL 2395 (R = 5)
tps = basket_take_profits(2400.0, 2395.0, True, 5, CONFIG)
check("TP1 at +1R", abs(tps[0] - 2405.0) < 1e-9, str(tps))
check("TP2 at +1.5R", abs(tps[1] - 2407.5) < 1e-9)
check("TP4 at +3R", abs(tps[3] - 2415.0) < 1e-9)
check("runner TP far away (+10R)", abs(tps[4] - 2450.0) < 1e-9)
tps_sell = basket_take_profits(2400.0, 2405.0, False, 5, CONFIG)
check("SELL ladder mirrored", abs(tps_sell[0] - 2395.0) < 1e-9
      and tps_sell[4] < tps_sell[0], str(tps_sell))

# risk-free detection (basket may add only when every stop is at BE+)
from trade_manager import TradeManager as _TM
import MetaTrader5 as _mt5

class _Pos:
    def __init__(self, type_, entry, sl):
        self.type = type_
        self.price_open = entry
        self.sl = sl

check("risky BUY position detected",
      not _TM.positions_risk_free([_Pos(_mt5.POSITION_TYPE_BUY, 2400.0, 2395.0)]))
check("breakeven BUY is risk-free",
      _TM.positions_risk_free([_Pos(_mt5.POSITION_TYPE_BUY, 2400.0, 2400.5)]))
check("breakeven SELL is risk-free",
      _TM.positions_risk_free([_Pos(_mt5.POSITION_TYPE_SELL, 2400.0, 2399.5)]))
check("missing SL is never risk-free",
      not _TM.positions_risk_free([_Pos(_mt5.POSITION_TYPE_BUY, 2400.0, 0.0)]))

# runner ladder uses the STORED risk unit, not its far TP:
# entry 2400, SL 2395 (R=5), runner TP 2450. At +1R price=2405 the runner
# must still go to breakeven (would not without the stored risk_unit).
from trade_manager import compute_protective_sl as _cps
runner_be = _cps(True, 2400.0, 2395.0, 2450.0, 2405.0, 3.0, None, CONFIG,
                 risk_unit=5.0)
check("runner reaches breakeven at +1R with stored R",
      runner_be is not None and runner_be >= 2400.0, f"got {runner_be}")

print("--- protection ladder (breakeven / trailing) ---")
from trade_manager import compute_protective_sl, profit_in_r
from market_structure import Swing, StructureState

# BUY: entry 2400, SL 2395 (risk unit 5), TP 2410 (2R)
ENTRY, SL0, TP = 2400.0, 2395.0, 2410.0
ATR = 3.0

def ladder(price, sl=SL0, structure=None):
    return compute_protective_sl(True, ENTRY, sl, TP, price, ATR, structure, CONFIG)

check("no move at +0.2R", ladder(2401.0) is None)

sl_half = ladder(2402.5)   # +0.5R
check("stage 1: half-risk at +0.5R", sl_half is not None and abs(sl_half - 2397.5) < 1e-6,
      f"got {sl_half}")

sl_be = ladder(2405.0)     # +1R
check("stage 2: breakeven(+) at +1R", sl_be is not None and sl_be >= ENTRY,
      f"got {sl_be}")

sl_lock = ladder(2407.5)   # +1.5R
check("stage 3: locks +0.5R at +1.5R", sl_lock is not None and sl_lock >= ENTRY + 2.5,
      f"got {sl_lock}")

# stage 4: deep profit -> trailing follows price
sl_deep = ladder(2420.0)
check("stage 4: trails in deep profit", sl_deep is not None and sl_deep > ENTRY + 2.5,
      f"got {sl_deep}")
check("trail keeps min gap from price", sl_deep <= 2420.0 - CONFIG["min_trail_gap_atr"] * ATR)

# structure trailing: a swing low above the ATR trail tightens the stop
st_struct = StructureState()
st_struct.last_swing_low = Swing(0, 2416.0, "L")
sl_struct = ladder(2420.0, structure=st_struct)
expected_struct = 2416.0 - CONFIG["trail_struct_buffer_atr"] * ATR
check("structure trail tightens beyond ATR trail",
      sl_struct is not None and abs(sl_struct - expected_struct) < 1e-6,
      f"got {sl_struct}, expected {expected_struct}")

# never loosen: same price but SL already tighter -> no change
check("stop never moves backwards", ladder(2402.5, sl=2399.0) is None)

# SELL mirror: entry 2400, SL 2405, TP 2390
sell_sl = compute_protective_sl(False, 2400.0, 2405.0, 2390.0, 2395.0, ATR, None, CONFIG)
check("SELL breakeven mirrored", sell_sl is not None and sell_sl <= 2400.0,
      f"got {sell_sl}")

# time-stop math
r_now = profit_in_r(True, ENTRY, TP, 2401.0, CONFIG["min_reward_risk"])
check("profit_in_r math", abs(r_now - 0.2) < 1e-9, f"got {r_now}")

print("--- control panel settings ---")
import importlib
import json
import config as config_module
from control_panel import ControlPanel

had_settings = os.path.exists("settings.json")
backup = None
if had_settings:
    with open("settings.json", "r", encoding="utf-8") as f:
        backup = f.read()

try:
    # WITHOUT user_tuned: only account keys apply — stale strategy overrides
    # from an old version must NOT freeze the new defaults.
    with open("settings.json", "w", encoding="utf-8") as f:
        json.dump({"risk_per_trade_pct": 0.75, "symbol": "GOLD"}, f)
    importlib.reload(config_module)
    check("stale strategy override ignored",
          config_module.CONFIG["risk_per_trade_pct"] == 1.0)
    check("account key (symbol) still applies", config_module.CONFIG["symbol"] == "GOLD")
    check("untouched keys keep defaults", config_module.CONFIG["daily_target_pct"] == 5.0)

    # WITH user_tuned (saved from the ⚙ settings window): everything applies.
    with open("settings.json", "w", encoding="utf-8") as f:
        json.dump({"risk_per_trade_pct": 0.75, "symbol": "GOLD",
                   "user_tuned": True}, f)
    importlib.reload(config_module)
    check("user-tuned strategy override applies",
          config_module.CONFIG["risk_per_trade_pct"] == 0.75)
    check("user_tuned flag not leaked into CONFIG",
          "user_tuned" not in config_module.CONFIG)
finally:
    if had_settings:
        with open("settings.json", "w", encoding="utf-8") as f:
            f.write(backup)
    elif os.path.exists("settings.json"):
        os.remove("settings.json")
    importlib.reload(config_module)

panel = ControlPanel.__new__(ControlPanel)   # parse helpers don't need a window
check("hours parsed", panel._parse_value("trading_hours", "hours", "7-21") == [7, 21])
check("windows parsed",
      panel._parse_value("blackout_windows", "windows", "15:15-15:50, 16:55-17:20")
      == ["15:15-15:50", "16:55-17:20"])
try:
    panel._parse_value("trading_hours", "hours", "25-3")
    check("bad hours rejected", False)
except ValueError:
    check("bad hours rejected", True)
try:
    panel._parse_value("blackout_windows", "windows", "99:99-12:00")
    check("bad window rejected", False)
except ValueError:
    check("bad window rejected", True)
check("empty optional login -> None",
      panel._parse_value("mt5_login", "opt_int", "") is None)

print("--- top-down D1/H4/H1 bias ---")
import topdown

def make_tf_df(n, step, start=2400.0, noise_seed=1):
    """Simple synthetic candles drifting by `step` per bar."""
    rng = np.random.default_rng(noise_seed)
    closes = start + np.cumsum(np.full(n, step) + rng.normal(0, abs(step) * 0.1, n))
    opens = np.concatenate([[start], closes[:-1]])
    highs = np.maximum(opens, closes) + 0.5
    lows = np.minimum(opens, closes) - 0.5
    return pd.DataFrame({
        "time": pd.date_range("2026-06-01", periods=n, freq="h"),
        "open": opens, "high": highs, "low": lows, "close": closes,
    })

d1_up = make_tf_df(12, 8.0)
h4_up = make_tf_df(80, 2.0)
h1_up = make_tf_df(160, 0.8)
bias, detail = topdown.bias_from_frames(d1_up, h4_up, h1_up)
check("all frames up -> BUY bias", bias == ms.UPTREND, detail)

d1_dn = make_tf_df(12, -8.0)
h4_dn = make_tf_df(80, -2.0)
h1_dn = make_tf_df(160, -0.8)
bias, detail = topdown.bias_from_frames(d1_dn, h4_dn, h1_dn)
check("all frames down -> SELL bias", bias == ms.DOWNTREND, detail)

bias, detail = topdown.bias_from_frames(d1_up, h4_dn, h1_up)
check("mixed frames -> no bias (no trade)", bias is None, detail)

bias, detail = topdown.bias_from_frames(None, None, None)
check("missing data -> no bias", bias is None, detail)

# The bias plugs into evaluate() as the HTF gate: with a SELL bias, the
# uptrend M5 data must never produce a BUY.
w = add_indicators(up_df.iloc[:300].reset_index(drop=True), CONFIG)
s, r = strategy.evaluate(w, CONFIG, htf_bias=ms.DOWNTREND)
check("SELL bias blocks buys on bullish M5", s is None or s.direction != "BUY", r)

# With a BUY bias, signals still fire on the bullish M5 run.
bias_signals = 0
for end in range(250, len(up_df)):
    w = add_indicators(up_df.iloc[:end + 1].reset_index(drop=True), CONFIG)
    s, r = strategy.evaluate(w, CONFIG, htf_bias=ms.UPTREND)
    if s:
        bias_signals += 1
        if s.direction != "BUY":
            check("bias trades only in bias direction", False, s.direction)
            break
check("BUY bias still produces buy entries", bias_signals > 0, f"got {bias_signals}")

print(f"\n{PASS} passed, {FAIL} failed")
print("--- data heartbeat ---")
from data_heartbeat import write_heartbeat, heartbeat_fresh
import tempfile
hb_cfg = dict(CONFIG, heartbeat_file=os.path.join(tempfile.gettempdir(),
                                                   "gg_test_heartbeat.json"))
write_heartbeat(hb_cfg, equity=10000.0, last_bar="2026-07-07 12:00:00")
check("heartbeat fresh after write", heartbeat_fresh(hb_cfg, within_seconds=30))
write_heartbeat(hb_cfg, rates_ok=False)
check("heartbeat not fresh when rates_ok=False",
      not heartbeat_fresh(hb_cfg, within_seconds=30))
try:
    os.remove(hb_cfg["heartbeat_file"])
except OSError:
    pass
raise SystemExit(1 if FAIL else 0)
