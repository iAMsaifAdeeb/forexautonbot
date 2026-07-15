"""
Stop-ladder strategy (V21 — dual grid):

Place MANY Sell Stops BELOW price AND MANY Buy Stops ABOVE price at the
same time. Whichever way gold breaks, that side starts banking 10-pip TPs.

Safety (so chop doesn't fight itself):
  - when the FIRST side fills, cancel every pending on the OPPOSITE side
  - keep only 1 open position at a time
  - still stop before previous swing (+ margin) / EMA touch on the active side
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
    Falls back to the recent extremes when no fractal is confirmed yet."""
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
    """Sell ladder ends when price tags the lower EMA from above.
    Buy ladder ends when price tags the upper EMA from below."""
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


def _levels_ok(direction: str, entry: float, df: pd.DataFrame,
               config: dict) -> str | None:
    """Return a refusal reason, or None if this stop level is allowed."""
    pip = pip_size(config)
    margin = float(config.get("ladder_prev_margin_pips", 25)) * pip
    last = df.iloc[-1]
    if direction == "SELL":
        prev_low = previous_swing(df, config, "L")
        if prev_low is not None and entry <= prev_low + margin:
            return (f"Sell Stop {entry:.2f} too close to previous low "
                    f"{prev_low:.2f}")
        ma = lower_ma(last)
        if ma is not None and float(last["close"]) > ma and entry <= ma:
            return f"Sell Stop {entry:.2f} would hit lower MA {ma:.2f}"
        return None

    prev_high = previous_swing(df, config, "H")
    if prev_high is not None and entry >= prev_high - margin:
        return (f"Buy Stop {entry:.2f} too close to previous high "
                f"{prev_high:.2f}")
    ma = upper_ma(last)
    if ma is not None and float(last["close"]) < ma and entry >= ma:
        return f"Buy Stop {entry:.2f} would hit upper MA {ma:.2f}"
    return None


def build_side_plans(direction: str, market_price: float, df: pd.DataFrame,
                     config: dict, legs: int | None = None) -> list[LadderPlan]:
    """Build up to `legs` pending stops on ONE side of the market."""
    pip = pip_size(config)
    offset = float(config.get("ladder_entry_offset_pips", 10)) * pip
    # Distance between consecutive stop entries (TP + gap, matching 4110→4108).
    step = (float(config.get("ladder_tp_pips", 10))
            + float(config.get("ladder_gap_pips", 10))) * pip
    tp_dist = float(config.get("ladder_tp_pips", 10)) * pip
    sl_dist = float(config.get("ladder_sl_pips", 20)) * pip
    n = int(legs if legs is not None else config.get("ladder_legs", 5))
    n = max(1, n)

    plans: list[LadderPlan] = []
    for i in range(n):
        if direction == "SELL":
            entry = market_price - offset - i * step
            if entry >= market_price - pip * 0.5:
                continue
            tp = entry - tp_dist
            sl = entry + sl_dist
        else:
            entry = market_price + offset + i * step
            if entry <= market_price + pip * 0.5:
                continue
            tp = entry + tp_dist
            sl = entry - sl_dist

        refuse = _levels_ok(direction, entry, df, config)
        if refuse:
            break  # further legs are deeper into the terminal zone
        plans.append(LadderPlan(
            direction, entry, sl, tp,
            f"{direction} STOP grid[{i + 1}/{n}] @ {entry:.2f} TP {tp:.2f}",
        ))
    return plans


def plan_dual_grid(df: pd.DataFrame, config: dict, *,
                   bid: float, ask: float) -> tuple[list[LadderPlan], str]:
    """Both sides at once: Sell Stops below + Buy Stops above."""
    sells = build_side_plans("SELL", bid, df, config)
    buys = build_side_plans("BUY", ask, df, config)
    plans = sells + buys
    if not plans:
        return [], "dual grid empty — too close to swing/MA terminals"
    return plans, f"{len(buys)} Buy Stops + {len(sells)} Sell Stops armed"


def next_entry_from_ladder(direction: str, last_tp: float | None,
                           market_price: float, config: dict) -> float:
    """Next single stop after a completed TP (active-side continuation)."""
    pip = pip_size(config)
    gap = float(config.get("ladder_gap_pips", 10)) * pip
    offset = float(config.get("ladder_entry_offset_pips", 10)) * pip

    if direction == "SELL":
        entry = (last_tp - gap) if last_tp is not None else (market_price - offset)
        if entry >= market_price - pip * 0.5:
            entry = market_price - offset
        return entry

    entry = (last_tp + gap) if last_tp is not None else (market_price + offset)
    if entry <= market_price + pip * 0.5:
        entry = market_price + offset
    return entry


def plan_next(df: pd.DataFrame, config: dict, *,
              market_price: float,
              last_tp: float | None = None,
              last_direction: str | None = None,
              force_direction: str | None = None) -> tuple[LadderPlan | None, str]:
    """Decide the next single pending stop (used after a side has activated)."""
    direction = force_direction or short_direction(df, config)
    if direction is None:
        return None, "candles flat — waiting for clear short-term direction"

    if last_direction and last_direction != direction and not force_direction:
        last_tp = None

    if ma_touched(df, direction):
        which = "lower" if direction == "SELL" else "upper"
        return None, f"{which} MA already touched — ladder finished for this move"

    entry = next_entry_from_ladder(direction, last_tp, market_price, config)
    pip = pip_size(config)
    tp_dist = float(config.get("ladder_tp_pips", 10)) * pip
    sl_dist = float(config.get("ladder_sl_pips", 20)) * pip

    if direction == "SELL":
        tp, sl = entry - tp_dist, entry + sl_dist
    else:
        tp, sl = entry + tp_dist, entry - sl_dist

    refuse = _levels_ok(direction, entry, df, config)
    if refuse:
        return None, refuse + " — stopping"

    note = ""
    if direction == "SELL":
        prev = previous_swing(df, config, "L")
        note = f"prev_low={prev:.2f}" if prev else ""
    else:
        prev = previous_swing(df, config, "H")
        note = f"prev_high={prev:.2f}" if prev else ""

    return LadderPlan(
        direction, entry, sl, tp,
        f"{direction} STOP ladder @ {entry:.2f} TP {tp:.2f}", note,
    ), "ok"
