"""
Market structure analysis (Smart-Money-Concept style):

- Detects swing highs / swing lows using a fractal window.
- Classifies the trend from the sequence of swings:
    Higher Highs + Higher Lows  -> UPTREND
    Lower Highs  + Lower Lows   -> DOWNTREND
    otherwise                   -> RANGE
- Detects Break of Structure (BOS): a candle CLOSE beyond the most recent
  confirmed swing high (bullish BOS) or swing low (bearish BOS).
"""

from dataclasses import dataclass, field

import pandas as pd

UPTREND = "UP"
DOWNTREND = "DOWN"
RANGE = "RANGE"


@dataclass
class Swing:
    index: int
    price: float
    kind: str  # "H" or "L"


@dataclass
class StructureState:
    trend: str = RANGE
    swings: list = field(default_factory=list)
    last_swing_high: Swing | None = None
    last_swing_low: Swing | None = None
    bos: str | None = None       # "BULL", "BEAR" or None — on the latest closed bar
    bos_level: float | None = None


def find_swings(df: pd.DataFrame, lookback: int) -> list[Swing]:
    """A swing high/low is a bar whose high/low is the extreme of the
    `lookback` bars on BOTH sides (classic fractal)."""
    highs = df["high"].values
    lows = df["low"].values
    swings: list[Swing] = []

    for i in range(lookback, len(df) - lookback):
        window_h = highs[i - lookback: i + lookback + 1]
        window_l = lows[i - lookback: i + lookback + 1]
        if highs[i] == window_h.max() and (window_h == highs[i]).sum() == 1:
            swings.append(Swing(i, float(highs[i]), "H"))
        if lows[i] == window_l.min() and (window_l == lows[i]).sum() == 1:
            swings.append(Swing(i, float(lows[i]), "L"))

    swings.sort(key=lambda s: s.index)
    return swings


def classify_trend(swings: list[Swing]) -> str:
    """Compare the last two swing highs and last two swing lows."""
    highs = [s for s in swings if s.kind == "H"][-2:]
    lows = [s for s in swings if s.kind == "L"][-2:]
    if len(highs) < 2 or len(lows) < 2:
        return RANGE

    higher_highs = highs[1].price > highs[0].price
    higher_lows = lows[1].price > lows[0].price
    lower_highs = highs[1].price < highs[0].price
    lower_lows = lows[1].price < lows[0].price

    if higher_highs and higher_lows:
        return UPTREND
    if lower_highs and lower_lows:
        return DOWNTREND
    return RANGE


def analyze(df: pd.DataFrame, lookback: int) -> StructureState:
    """Analyze structure on a dataframe of CLOSED candles.
    BOS is evaluated on the last closed candle only."""
    state = StructureState()
    swings = find_swings(df, lookback)
    state.swings = swings
    if not swings:
        return state

    state.trend = classify_trend(swings)

    last_bar = len(df) - 1
    last_close = float(df["close"].iloc[-1])

    # Most recent swings that were confirmed BEFORE the last bar
    # (a fractal needs `lookback` future bars, so anything in the list qualifies
    #  as long as it isn't the last bar itself).
    prior_highs = [s for s in swings if s.kind == "H" and s.index < last_bar]
    prior_lows = [s for s in swings if s.kind == "L" and s.index < last_bar]
    state.last_swing_high = prior_highs[-1] if prior_highs else None
    state.last_swing_low = prior_lows[-1] if prior_lows else None

    # Break of structure: close beyond the last confirmed swing point,
    # where the previous bar had NOT yet closed beyond it (fresh break).
    prev_close = float(df["close"].iloc[-2]) if len(df) >= 2 else last_close

    if state.last_swing_high and last_close > state.last_swing_high.price >= prev_close:
        state.bos = "BULL"
        state.bos_level = state.last_swing_high.price
    elif state.last_swing_low and last_close < state.last_swing_low.price <= prev_close:
        state.bos = "BEAR"
        state.bos_level = state.last_swing_low.price

    return state
