"""
Top-down multi-timeframe analysis — the professional pre-trade routine.

Exactly the flow a pro trader follows before touching the buy/sell button:

 1. D1  — look at the PREVIOUS completed daily candle. Which side won the day?
 2. H4  — are the 4-hour candles trending? (close vs EMA20, EMA20 slope)
 3. H1  — is the hourly aligned? (close vs EMA20 vs EMA50)

Each timeframe casts one vote: +1 (bullish), -1 (bearish) or 0 (unclear).
Only when the votes clearly agree (net score >= 2 of 3) does the bot get a
directional bias — then the M5 chart is only used to time the ENTRY in that
direction. Mixed votes = no bias = no trade (never fight the big picture).
"""

import logging

import pandas as pd

from indicators import ema
import market_structure as ms

log = logging.getLogger("bot.topdown")


def _vote_d1(d1: pd.DataFrame) -> tuple[int, str]:
    """Previous completed daily candle: which side owned the day?"""
    if d1 is None or len(d1) < 2:
        return 0, "D1:n/a"
    prev = d1.iloc[-2]  # last row is the still-forming day
    if prev["close"] > prev["open"]:
        return 1, "D1:bull"
    if prev["close"] < prev["open"]:
        return -1, "D1:bear"
    return 0, "D1:flat"


def _vote_h4(h4: pd.DataFrame) -> tuple[int, str]:
    """H4 trend: close vs EMA20 and the EMA20 slope."""
    if h4 is None or len(h4) < 25:
        return 0, "H4:n/a"
    closed = h4.iloc[:-1]
    e20 = ema(closed["close"], 20)
    close = float(closed["close"].iloc[-1])
    rising = e20.iloc[-1] > e20.iloc[-4]
    falling = e20.iloc[-1] < e20.iloc[-4]
    if close > e20.iloc[-1] and rising:
        return 1, "H4:bull"
    if close < e20.iloc[-1] and falling:
        return -1, "H4:bear"
    return 0, "H4:flat"


def _vote_h1(h1: pd.DataFrame) -> tuple[int, str]:
    """H1 trend: close vs EMA20 vs EMA50 stack."""
    if h1 is None or len(h1) < 60:
        return 0, "H1:n/a"
    closed = h1.iloc[:-1]
    fast = ema(closed["close"], 20).iloc[-1]
    slow = ema(closed["close"], 50).iloc[-1]
    close = float(closed["close"].iloc[-1])
    if close > fast > slow:
        return 1, "H1:bull"
    if close < fast < slow:
        return -1, "H1:bear"
    return 0, "H1:flat"


def bias_from_frames(d1: pd.DataFrame | None, h4: pd.DataFrame | None,
                     h1: pd.DataFrame | None) -> tuple[str | None, str]:
    """Pure vote-counting logic (testable offline).
    Returns (UPTREND / DOWNTREND / None, human-readable detail)."""
    votes = [_vote_d1(d1), _vote_h4(h4), _vote_h1(h1)]
    score = sum(v for v, _ in votes)
    detail = " ".join(label for _, label in votes)

    if score >= 2:
        return ms.UPTREND, f"{detail} => BUY bias"
    if score <= -2:
        return ms.DOWNTREND, f"{detail} => SELL bias"
    return None, f"{detail} => mixed, no bias"


def htf_bias(client, config: dict) -> tuple[str | None, str]:
    """Fetch real D1 / H4 / H1 candles from MT5 and compute the bias."""
    if not config.get("topdown_enabled", True):
        return None, "top-down disabled"
    try:
        d1 = client.get_rates_tf(1440, 12)
        h4 = client.get_rates_tf(240, 80)
        h1 = client.get_rates_tf(60, 160)
    except Exception as exc:
        log.warning("Top-down data fetch failed: %s", exc)
        return None, "top-down data unavailable"
    return bias_from_frames(d1, h4, h1)
