"""
Signal engine — combines every market rule into one decision per closed candle.

A trade signal must survive ALL of these gates, in order:

 1. SESSION      — inside configured trading hours.
 2. NEWS         — not inside a blackout window (scheduled high-impact news).
 3. SPIKE        — no abnormal candle (range > 2.5 * ATR) in the recent past;
                   news/flash spikes pause trading for a cooldown period.
 4. STRENGTH     — ADX above threshold AND the sideways lockout passes:
                   Choppiness Index, EMA compression and price-box detectors
                   must ALL agree the market is trending. Sideways = no work.
 5. HTF TREND    — the M30 timeframe agrees with the trade direction.
 6. M5 TREND     — market structure (HH/HL or LL/LH) AND EMA 50/200 agree.
 7. TRIGGER      — one of two classic entries:
                   a) Break of Structure: candle CLOSE beyond the last
                      confirmed swing high (buy) / swing low (sell), or
                   b) Pullback continuation: price pulled back against the
                      trend, then a candle resumed it by closing beyond the
                      previous bar's extreme (classic "buy the dip" entry).
 8. FAKEOUT      — a BOS candle must be genuine:
                     - strong body (>= 35% of its range), right direction,
                     - close clears the level by a margin (no paper-thin breaks),
                     - volume above average,
                     - the level has NOT already faked out recently,
                     - price hasn't already run away (no chasing).
 9. EXHAUSTION   — RSI not overbought (buys) / oversold (sells).

Stops go beyond the opposite swing plus an ATR buffer (volatility stop as a
fallback); targets are a fixed reward:risk multiple.
"""

import logging
from dataclasses import dataclass
from datetime import time as dtime

import pandas as pd

import market_structure as ms
from indicators import ema

log = logging.getLogger("bot.strategy")


@dataclass
class Signal:
    direction: str          # "BUY" or "SELL"
    entry_hint: float       # last close (actual fill is at market)
    stop_loss: float
    take_profit: float
    reason: str
    confidence: float = 0.0  # 0-100 quality score of this setup


def confidence_score(last: pd.Series, direction: str, config: dict) -> float:
    """Score the setup 0-100 across five independent quality dimensions.
    Everything here already passed the hard gates — this measures HOW good
    the setup is beyond the minimum requirements."""
    score = 0.0

    # 1. Trend strength: ADX from the minimum up to 45 -> 0..30 points
    adx_span = 45 - config["adx_min"]
    score += max(0.0, min(30.0, (float(last["adx"]) - config["adx_min"]) / adx_span * 30))

    # 2. Trend cleanliness: choppiness from the max down to 30 -> 0..20 points
    chop_span = config["chop_max"] - 30
    score += max(0.0, min(20.0, (config["chop_max"] - float(last["chop"])) / chop_span * 20))

    # 3. Breakout conviction: candle body ratio 0.4..0.9 -> 0..20 points
    rng = float(last["high"] - last["low"])
    body_ratio = abs(float(last["close"] - last["open"])) / rng if rng > 0 else 0
    body_span = 0.9 - config["min_body_ratio"]
    score += max(0.0, min(20.0, (body_ratio - config["min_body_ratio"]) / body_span * 20))

    # 4. Participation: volume 1x..2x its average -> 0..20 points
    vol_sma = float(last["vol_sma"]) if pd.notna(last["vol_sma"]) else 0.0
    if vol_sma > 0:
        vol_ratio = float(last["tick_volume"]) / vol_sma
        score += max(0.0, min(20.0, (vol_ratio - 1.0) * 20))

    # 5. Room to run: RSI distance from the exhaustion zone -> 0..10 points
    if direction == "BUY":
        headroom = config["rsi_overbought"] - float(last["rsi"])
        span = config["rsi_overbought"] - 50
    else:
        headroom = float(last["rsi"]) - config["rsi_oversold"]
        span = 50 - config["rsi_oversold"]
    score += max(0.0, min(10.0, headroom / span * 10))

    return round(min(100.0, score), 1)


# --------------------------------------------------------------------------
# Protection filters
# --------------------------------------------------------------------------

def _parse_window(window: str) -> tuple[dtime, dtime]:
    start, end = window.split("-")
    h1, m1 = map(int, start.split(":"))
    h2, m2 = map(int, end.split(":"))
    return dtime(h1, m1), dtime(h2, m2)


def in_blackout(bar_time, config: dict) -> bool:
    t = bar_time.time()
    for window in config["blackout_windows"]:
        start, end = _parse_window(window)
        if start <= t <= end:
            return True
    return False


def recent_spike(df: pd.DataFrame, config: dict) -> bool:
    """True if any of the last `spike_pause_bars` candles had an abnormal
    range compared to the ATR *before* that candle (so the spike itself
    doesn't hide inside an inflated ATR)."""
    n = config["spike_pause_bars"]
    recent = df.iloc[-n:]
    prior_atr = df["atr"].shift(1).iloc[-n:]
    ranges = recent["high"] - recent["low"]
    return bool((ranges > config["spike_atr_mult"] * prior_atr).any())


def sideways_reason(df: pd.DataFrame, config: dict) -> str | None:
    """Three independent sideways detectors. If ANY of them says 'range',
    the bot does not work at all (user rule: no trading in sideways markets).

    1. Choppiness Index — the textbook range detector.
    2. EMA compression — EMA50 and EMA200 tangled together = flat market.
    3. Price box — the whole recent session compressed into a tiny band.
    """
    last = df.iloc[-1]
    atr_value = float(last["atr"])

    if float(last["chop"]) > config["chop_max"]:
        return (f"SIDEWAYS (choppiness {last['chop']:.0f} > "
                f"{config['chop_max']:.0f}) — not working")

    if atr_value > 0:
        separation = abs(float(last["ema_fast"] - last["ema_slow"]))
        if separation < config["ema_separation_atr"] * atr_value:
            return "SIDEWAYS (EMAs flat and tangled) — not working"

        n = config["range_box_bars"]
        box = df.iloc[-n:]
        span = float(box["high"].max() - box["low"].min())
        if span < config["range_box_atr"] * atr_value:
            return (f"SIDEWAYS (last {n} bars boxed in {span:.1f}, "
                    f"< {config['range_box_atr']:.0f} ATR) — not working")

    return None


def pullback_entry(df: pd.DataFrame, ema_trend: str, config: dict) -> bool:
    """Classic trend-continuation trigger ("buy the dip / sell the rally"):
    price pulled back against the trend within the last few bars, and the
    latest candle resumed the trend by closing beyond the previous bar's
    extreme, on the right side of the EMA50."""
    if not config.get("pullback_enabled", True):
        return False
    look = config["pullback_lookback"]
    last = df.iloc[-1]
    prev = df.iloc[-2]
    recent = df.iloc[-(look + 1):-1]     # bars before the current candle

    # The resume candle must have real conviction — no wick-spike bodies.
    rng = float(last["high"] - last["low"])
    body = abs(float(last["close"] - last["open"]))
    if rng <= 0 or body / rng < config["min_body_ratio"]:
        return False

    if ema_trend == ms.UPTREND:
        pulled = bool((recent["close"] < recent["open"]).any())
        resumed = (last["close"] > last["open"]
                   and last["close"] > prev["high"]
                   and last["close"] > last["ema_fast"])
        return pulled and resumed
    if ema_trend == ms.DOWNTREND:
        pulled = bool((recent["close"] > recent["open"]).any())
        resumed = (last["close"] < last["open"]
                   and last["close"] < prev["low"]
                   and last["close"] < last["ema_fast"])
        return pulled and resumed
    return False


def htf_trend(df: pd.DataFrame, config: dict) -> str | None:
    """Resample the base candles to the higher timeframe and read its EMA trend."""
    htf = (
        df.set_index("time")
        .resample(f"{config['htf_minutes']}min")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
        .dropna()
    )
    if len(htf) < config["htf_ema_slow"]:
        return None
    fast = ema(htf["close"], config["htf_ema_fast"]).iloc[-1]
    slow = ema(htf["close"], config["htf_ema_slow"]).iloc[-1]
    close = htf["close"].iloc[-1]
    if close > fast > slow:
        return ms.UPTREND
    if close < fast < slow:
        return ms.DOWNTREND
    return None


def level_burned(df: pd.DataFrame, level: float, start_idx: int, direction: str,
                 memory_bars: int) -> bool:
    """A level that was already broken and reclaimed recently is a proven
    fakeout magnet — skip it."""
    begin = max(start_idx, len(df) - 1 - memory_bars)
    closes = df["close"].values
    beyond = False
    for i in range(begin, len(df) - 1):   # exclude the current breakout bar
        if direction == "BULL":
            if closes[i] > level:
                beyond = True
            elif beyond and closes[i] <= level:
                return True
        else:
            if closes[i] < level:
                beyond = True
            elif beyond and closes[i] >= level:
                return True
    return False


def breakout_quality(last: pd.Series, level: float, direction: str,
                     atr_value: float, config: dict) -> str | None:
    """Returns a rejection reason if the breakout candle looks like a fakeout,
    otherwise None."""
    rng = float(last["high"] - last["low"])
    body = abs(float(last["close"] - last["open"]))
    close = float(last["close"])

    if rng <= 0 or body / rng < config["min_body_ratio"]:
        return f"weak breakout body ({body / rng:.0%} of range) — fakeout risk"

    if direction == "BULL" and last["close"] <= last["open"]:
        return "bullish break on a bearish candle — fakeout risk"
    if direction == "BEAR" and last["close"] >= last["open"]:
        return "bearish break on a bullish candle — fakeout risk"

    margin = config["bos_margin_atr"] * atr_value
    cleared = (close - level) if direction == "BULL" else (level - close)
    if cleared < margin:
        return f"close cleared level by only {cleared:.2f} (< {margin:.2f}) — fakeout risk"
    if cleared > config["max_chase_atr"] * atr_value:
        return f"price already {cleared:.2f} past the level — too late, not chasing"

    vol_sma = float(last["vol_sma"]) if pd.notna(last["vol_sma"]) else 0.0
    if vol_sma > 0 and float(last["tick_volume"]) < config["volume_confirm_mult"] * vol_sma:
        return "breakout volume below average — no participation, fakeout risk"

    return None


# --------------------------------------------------------------------------
# Main evaluation
# --------------------------------------------------------------------------

def evaluate(df: pd.DataFrame, config: dict,
             htf_bias: str | None = None) -> tuple[Signal | None, str]:
    """`df` must contain only CLOSED candles with indicators attached.
    `htf_bias` is the top-down D1/H4/H1 direction from topdown.py; when given
    it REPLACES the internal M30 resample as the big-picture gate.
    Returns (signal, explanation)."""
    last = df.iloc[-1]

    # 1. Session
    bar_hour = last["time"].hour
    start_h, end_h = config["trading_hours"]
    if not (start_h <= bar_hour < end_h):
        return None, f"outside trading hours ({bar_hour}:00)"

    # 2. Scheduled news blackout
    if in_blackout(last["time"], config):
        return None, "inside news blackout window — not trading"

    # 3. Spike / flash-move cooldown
    if recent_spike(df, config):
        return None, "abnormal spike detected recently — waiting for market to settle"

    # 4. Trend strength + sideways lockout
    if last["adx"] < config["adx_min"]:
        return None, f"market not trending (ADX {last['adx']:.1f} < {config['adx_min']})"
    range_reason = sideways_reason(df, config)
    if range_reason:
        return None, range_reason

    # 5. Higher-timeframe agreement: top-down D1/H4/H1 bias when available,
    #    otherwise the internal M30 resample (tests / fallback).
    h_trend = htf_bias if htf_bias in (ms.UPTREND, ms.DOWNTREND) else htf_trend(df, config)
    if h_trend is None:
        return None, "no clear higher-timeframe trend (D1/H4/H1 mixed) — waiting"

    # 6. Base-timeframe agreement with the bias. The higher timeframes set
    #    the DIRECTION; the M5 chart only needs to confirm it — either the
    #    market structure or the EMA stack must agree, and the EMAs must
    #    never point the opposite way.
    structure = ms.analyze(df, config["swing_lookback"])
    ema_trend = None
    if last["close"] > last["ema_fast"] > last["ema_slow"]:
        ema_trend = ms.UPTREND
    elif last["close"] < last["ema_fast"] < last["ema_slow"]:
        ema_trend = ms.DOWNTREND

    if ema_trend is not None and ema_trend != h_trend:
        return None, (f"M5 EMAs ({ema_trend}) fight the higher-timeframe "
                      f"trend ({h_trend}) — waiting for alignment")
    if structure.trend != h_trend and ema_trend != h_trend:
        return None, (f"HTF says {h_trend} but M5 structure/EMAs don't "
                      "confirm yet — waiting")

    trade_trend = h_trend
    atr_value = float(last["atr"])
    close = float(last["close"])
    rr = config["min_reward_risk"]
    buffer = config["sl_atr_buffer"] * atr_value
    max_sl = config["max_sl_atr"] * atr_value
    fallback = config["fallback_sl_atr"] * atr_value

    # 7. Trigger a) fresh break of structure in the trend direction
    trigger = None       # ("BOS"|"PULLBACK", human reason)
    if trade_trend == ms.UPTREND and structure.bos == "BULL":
        level = structure.bos_level
        reject = breakout_quality(last, level, "BULL", atr_value, config)
        if reject:
            return None, reject
        if level_burned(df, level, structure.last_swing_high.index, "BULL",
                        config["fakeout_memory_bars"]):
            return None, f"level {level:.2f} already faked out recently — skipping"
        trigger = ("BOS", f"bullish BOS above {level:.2f}")
    elif trade_trend == ms.DOWNTREND and structure.bos == "BEAR":
        level = structure.bos_level
        reject = breakout_quality(last, level, "BEAR", atr_value, config)
        if reject:
            return None, reject
        if level_burned(df, level, structure.last_swing_low.index, "BEAR",
                        config["fakeout_memory_bars"]):
            return None, f"level {level:.2f} already faked out recently — skipping"
        trigger = ("BOS", f"bearish BOS below {level:.2f}")

    # 7. Trigger b) pullback continuation (classic EMA bounce with the trend)
    if trigger is None:
        if pullback_entry(df, trade_trend, config):
            trigger = ("PULLBACK", "pullback to EMA50 resumed with the trend")
        elif structure.bos is not None and structure.bos != (
                "BULL" if trade_trend == ms.UPTREND else "BEAR"):
            return None, "BOS against the trend — ignored (counter-trend trades forbidden)"
        else:
            return None, (f"trend {trade_trend} confirmed but no entry trigger "
                          "(no BOS, no pullback resume)")

    direction = "BUY" if trade_trend == ms.UPTREND else "SELL"

    # 8. Exhaustion
    if direction == "BUY" and last["rsi"] > config["rsi_overbought"]:
        return None, f"RSI {last['rsi']:.0f} overbought — not buying the top"
    if direction == "SELL" and last["rsi"] < config["rsi_oversold"]:
        return None, f"RSI {last['rsi']:.0f} oversold — not selling the bottom"

    # 9. Confidence gate
    conf = confidence_score(last, direction, config)
    if conf < config["min_confidence"]:
        return None, (f"setup confidence {conf:.0f} < {config['min_confidence']:.0f}"
                      " — watching, not trading")

    # Stop beyond the protective swing; ATR fallback when the swing is too far.
    if direction == "BUY":
        if structure.last_swing_low is None:
            return None, "no swing low available for the stop"
        sl = structure.last_swing_low.price - buffer
        risk = close - sl
        if risk <= 0 or risk > max_sl:
            risk = fallback
            sl = close - risk
        tp = close + rr * risk
    else:
        if structure.last_swing_high is None:
            return None, "no swing high available for the stop"
        sl = structure.last_swing_high.price + buffer
        risk = sl - close
        if risk <= 0 or risk > max_sl:
            risk = fallback
            sl = close + risk
        tp = close - rr * risk

    kind, detail = trigger
    frame_note = "D1/H4/H1 top-down" if htf_bias else "M5+M30"
    return (
        Signal(direction, close, sl, tp,
               f"{trade_trend} trend ({frame_note}) + {detail}",
               confidence=conf),
        f"{direction.lower()} signal ({kind})",
    )
