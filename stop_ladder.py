"""
Stop-ladder strategy (V20):

When the short M5 candles are falling -> place ONE Sell Stop below price,
with a fixed 10-pip take-profit. When that TP hits, place the next Sell Stop
one step further down. Mirror for Buy Stop when candles are rising.

Continue stacking until:
  - price touches the lower of the two EMAs (sell side) / upper EMA (buy), OR
  - the next entry would land within a safety margin of the previous swing
    low (sells) / high (buys).

Always 1 trade at a time (no pending pile-up, no basket).
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

import market_structure as ms

PIP_DEFAULT = 0.10


@dataclass
class LadderPlan:
    direction: str          # "BUY" or "SELL"
    entry: float            # pending stop trigger price
    stop_loss: float
    take_profit: float
    reason: str
    terminal_note: str = ""


def pip_size(config: dict) -> float:
    return float(config.get("pip_size", PIP_DEFAULT))


def short_direction(df: pd.DataFrame, config: dict) -> str | None:
    """Tiny-timeframe bias from the last N closed candles.
    Net fall -> SELL ladder; net rise -> BUY ladder. Flat -> wait."""
    n = int(config.get("ladder_bias_bars", 3))
    if len(df) < n:
        return None
    window = df.iloc[-n:]
    net = float(window["close"].iloc[-1] - window["open"].iloc[0])
    min_move = pip_size(config) * float(config.get("ladder_min_bias_pips", 3))
    if net <= -min_move:
        return "SELL"
    if net >= min_move:
        return "BUY"
    # Tie-break: last candle body alone.
    last = df.iloc[-1]
    body = float(last["close"] - last["open"])
    if body <= -min_move * 0.5:
        return "SELL"
    if body >= min_move * 0.5:
        return "BUY"
    return None


def lower_ma(last: pd.Series) -> float | None:
    vals = []
    for key in ("ema_fast", "ema_slow"):
        if key in last and pd.notna(last[key]):
            vals.append(float(last[key]))
    return min(vals) if vals else None


def upper_ma(last: pd.Series) -> float | None:
    vals = []
    for key in ("ema_fast", "ema_slow"):
        if key in last and pd.notna(last[key]):
            vals.append(float(last[key]))
    return max(vals) if vals else None


def previous_swing(df: pd.DataFrame, config: dict, kind: str) -> float | None:
    """Most recent confirmed swing low ('L') or high ('H').
    Falls back to the recent extremes when no fractal is confirmed yet —
    that keeps the safety margin working on young moves."""
    lookback = int(config.get("swing_lookback", 3))
    state = ms.analyze(df, lookback)
    if kind == "L" and state.last_swing_low is not None:
        return state.last_swing_low.price
    if kind == "H" and state.last_swing_high is not None:
        return state.last_swing_high.price

    skip = int(config.get("ladder_prev_skip_bars", 6))
    window = int(config.get("ladder_prev_lookback_bars", 96))
    if len(df) <= skip + 5:
        return None
    hist = df.iloc[max(0, len(df) - window - skip): len(df) - skip]
    if hist.empty:
        return None
    if kind == "L":
        return float(hist["low"].min())
    return float(hist["high"].max())


def ma_touched(df: pd.DataFrame, direction: str) -> bool:
    """True when price has JUST tagged the terminal MA from the active side.

    Sell ladder ends when a falling market tags the lower EMA from above.
    Buy ladder ends when a rising market tags the upper EMA from below.
    Already being deep through the MA does NOT freeze a fresh cascade —
    only the touch/claim event does.
    """
    if len(df) < 2:
        return False
    last = df.iloc[-1]
    prev = df.iloc[-2]
    if direction == "SELL":
        ma = lower_ma(last)
        if ma is None:
            return False
        return float(prev["close"]) > ma and float(last["low"]) <= ma
    ma = upper_ma(last)
    if ma is None:
        return False
    return float(prev["close"]) < ma and float(last["high"]) >= ma


def next_entry_from_ladder(direction: str, last_tp: float | None,
                           market_price: float, config: dict) -> float:
    """Compute the next Sell/Buy Stop price.

    After a completed trade (last_tp set): step one gap beyond that TP.
    Otherwise: start a fresh ladder one offset beyond live price.
    """
    pip = pip_size(config)
    tp_pips = float(config.get("ladder_tp_pips", 10))
    gap_pips = float(config.get("ladder_gap_pips", 10))
    offset_pips = float(config.get("ladder_entry_offset_pips", 10))
    gap = gap_pips * pip
    offset = offset_pips * pip

    if direction == "SELL":
        if last_tp is not None:
            entry = last_tp - gap
        else:
            entry = market_price - offset
        # Must sit BELOW the live bid for a Sell Stop.
        if entry >= market_price - pip * 0.5:
            entry = market_price - offset
        return entry

    if last_tp is not None:
        entry = last_tp + gap
    else:
        entry = market_price + offset
    if entry <= market_price + pip * 0.5:
        entry = market_price + offset
    return entry


def plan_next(df: pd.DataFrame, config: dict, *,
              market_price: float,
              last_tp: float | None = None,
              last_direction: str | None = None) -> tuple[LadderPlan | None, str]:
    """Decide the next single pending stop, or explain why we wait."""
    direction = short_direction(df, config)
    if direction is None:
        return None, "candles flat — waiting for clear short-term direction"

    # Fresh direction resets the ladder chain.
    if last_direction and last_direction != direction:
        last_tp = None

    if ma_touched(df, direction):
        which = "lower" if direction == "SELL" else "upper"
        return None, f"{which} MA already touched — ladder finished for this move"

    entry = next_entry_from_ladder(direction, last_tp, market_price, config)
    pip = pip_size(config)
    tp_dist = float(config.get("ladder_tp_pips", 10)) * pip
    sl_dist = float(config.get("ladder_sl_pips", 20)) * pip
    margin = float(config.get("ladder_prev_margin_pips", 25)) * pip

    if direction == "SELL":
        tp = entry - tp_dist
        sl = entry + sl_dist
        # Terminal: never sell into previous low (+ safety margin).
        prev_low = previous_swing(df, config, "L")
        if prev_low is not None and entry <= prev_low + margin:
            return None, (f"next Sell Stop {entry:.2f} too close to previous "
                          f"low {prev_low:.2f} (margin {margin:.2f}) — stopping")
        # Refuse a Sell Stop that would fire into the lower MA while price is
        # still above it (that's the planned end of the cascade).
        last = df.iloc[-1]
        ma = lower_ma(last)
        if (ma is not None and float(last["close"]) > ma and entry <= ma):
            return None, (f"next Sell Stop {entry:.2f} would hit lower MA "
                          f"{ma:.2f} — stopping")
        reason = f"SELL STOP ladder @ {entry:.2f} TP {tp:.2f}"
        note = f"prev_low={prev_low:.2f}" if prev_low else ""
        return LadderPlan("SELL", entry, sl, tp, reason, note), "ok"

    tp = entry + tp_dist
    sl = entry - sl_dist
    prev_high = previous_swing(df, config, "H")
    if prev_high is not None and entry >= prev_high - margin:
        return None, (f"next Buy Stop {entry:.2f} too close to previous "
                      f"high {prev_high:.2f} (margin {margin:.2f}) — stopping")
    last = df.iloc[-1]
    ma = upper_ma(last)
    if (ma is not None and float(last["close"]) < ma and entry >= ma):
        return None, (f"next Buy Stop {entry:.2f} would hit upper MA "
                      f"{ma:.2f} — stopping")
    reason = f"BUY STOP ladder @ {entry:.2f} TP {tp:.2f}"
    note = f"prev_high={prev_high:.2f}" if prev_high else ""
    return LadderPlan("BUY", entry, sl, tp, reason, note), "ok"
