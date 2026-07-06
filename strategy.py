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
 5. HTF TREND    — the H1 timeframe agrees with the trade direction.
 6. M15 TREND    — market structure (HH/HL or LL/LH) AND EMA 50/200 agree.
 7. TRIGGER      — fresh Break of Structure: candle CLOSE beyond the last
                   confirmed swing high (buy) / swing low (sell).
 8. FAKEOUT      — the breakout candle must be genuine:
                     - strong body (>= 40% of its range), right direction,
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


def htf_trend(df: pd.DataFrame, config: dict) -> str | None:
    """Resample M15 candles to the higher timeframe and read its EMA trend."""
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

def evaluate(df: pd.DataFrame, config: dict) -> tuple[Signal | None, str]:
    """`df` must contain only CLOSED candles with indicators attached.
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

    # 5. Higher-timeframe agreement
    h_trend = htf_trend(df, config)
    if h_trend is None:
        return None, "H1 timeframe has no clear trend"

    # 6. M15 trend: structure + EMAs
    structure = ms.analyze(df, config["swing_lookback"])
    ema_trend = None
    if last["close"] > last["ema_fast"] > last["ema_slow"]:
        ema_trend = ms.UPTREND
    elif last["close"] < last["ema_fast"] < last["ema_slow"]:
        ema_trend = ms.DOWNTREND

    if ema_trend is None:
        return None, "price between EMAs — no clear trend"
    if structure.trend != ema_trend:
        return None, f"structure ({structure.trend}) and EMAs ({ema_trend}) disagree"
    if h_trend != ema_trend:
        return None, f"H1 trend ({h_trend}) disagrees with M15 ({ema_trend})"

    # 7. Trigger: fresh break of structure
    if structure.bos is None:
        return None, f"trend {ema_trend} confirmed but no fresh break of structure"

    atr_value = float(last["atr"])
    close = float(last["close"])
    rr = config["min_reward_risk"]
    buffer = config["sl_atr_buffer"] * atr_value
    max_sl = config["max_sl_atr"] * atr_value
    fallback = config["fallback_sl_atr"] * atr_value

    if ema_trend == ms.UPTREND and structure.bos == "BULL":
        level = structure.bos_level
        swing_idx = structure.last_swing_high.index

        # 8. Fakeout gates
        reject = breakout_quality(last, level, "BULL", atr_value, config)
        if reject:
            return None, reject
        if level_burned(df, level, swing_idx, "BULL", config["fakeout_memory_bars"]):
            return None, f"level {level:.2f} already faked out recently — skipping"

        # 9. Exhaustion
        if last["rsi"] > config["rsi_overbought"]:
            return None, f"RSI {last['rsi']:.0f} overbought — not buying the top"

        if structure.last_swing_low is None:
            return None, "bullish BOS but no swing low for the stop"
        sl = structure.last_swing_low.price - buffer
        risk = close - sl
        if risk <= 0 or risk > max_sl:
            # Swing too far away in a strong trend — use a volatility stop instead.
            risk = fallback
            sl = close - risk
        tp = close + rr * risk
        return (
            Signal("BUY", close, sl, tp,
                   f"UP trend (M15+H1) + confirmed bullish BOS above {level:.2f}"),
            "buy signal",
        )

    if ema_trend == ms.DOWNTREND and structure.bos == "BEAR":
        level = structure.bos_level
        swing_idx = structure.last_swing_low.index

        reject = breakout_quality(last, level, "BEAR", atr_value, config)
        if reject:
            return None, reject
        if level_burned(df, level, swing_idx, "BEAR", config["fakeout_memory_bars"]):
            return None, f"level {level:.2f} already faked out recently — skipping"

        if last["rsi"] < config["rsi_oversold"]:
            return None, f"RSI {last['rsi']:.0f} oversold — not selling the bottom"

        if structure.last_swing_high is None:
            return None, "bearish BOS but no swing high for the stop"
        sl = structure.last_swing_high.price + buffer
        risk = sl - close
        if risk <= 0 or risk > max_sl:
            risk = fallback
            sl = close + risk
        tp = close - rr * risk
        return (
            Signal("SELL", close, sl, tp,
                   f"DOWN trend (M15+H1) + confirmed bearish BOS below {level:.2f}"),
            "sell signal",
        )

    return None, "BOS against the trend — ignored (counter-trend trades forbidden)"
