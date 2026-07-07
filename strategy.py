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
 5. M5 STRUCTURE — market structure is KING: HH/HL = uptrend (BUY only),
                   LL/LH = downtrend (SELL only). EMAs must never point
                   the opposite way; when structure is between swings the
                   EMA stack carries the trend.
 6. HTF VETO     — D1/H4/H1 top-down bias only BLOCKS a trade when it
                   clearly points the opposite way. Mixed = no block.
 7. TRIGGER      — depends on entry_mode:
                   HYBRID (Option B): last N candles align with M5 structure
                   (3 greens in uptrend / 3 reds in downtrend) -> fixed pip TP.
                   STRUCTURE: Break of Structure OR retest resume.
 8. FAKEOUT      — structure mode only: BOS candle quality gates.
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


def spike_gate(df: pd.DataFrame, trade_trend: str, config: dict) -> str | None:
    """Direction-aware spike protection.

    A big trend day IS a chain of large candles — freezing on every one of
    them means missing the whole move (the bug that kept the bot asleep on
    the 7-Jul dump). New rule:

    - spike AGAINST the intended trade direction  -> full pause
      (a violent counter-move / news whipsaw: stay out for spike_pause_bars),
    - spike IN the trade direction -> only wait `spike_calm_bars` bars for
      the dust to settle, then trading the continuation is allowed."""
    n = config["spike_pause_bars"]
    calm = config.get("spike_calm_bars", 2)
    recent = df.iloc[-n:]
    prior_atr = df["atr"].shift(1).iloc[-n:]
    ranges = (recent["high"] - recent["low"]).to_numpy()
    limits = (config["spike_atr_mult"] * prior_atr).to_numpy()
    opens = recent["open"].to_numpy()
    closes = recent["close"].to_numpy()

    m = len(recent)
    for pos in range(m):
        if not (limits[pos] > 0) or ranges[pos] <= limits[pos]:
            continue
        bars_ago = m - 1 - pos
        with_trend = ((trade_trend == ms.UPTREND and closes[pos] >= opens[pos])
                      or (trade_trend == ms.DOWNTREND and closes[pos] <= opens[pos]))
        if not with_trend:
            return ("abnormal COUNTER-trend spike detected — waiting for the "
                    "market to settle")
        if bars_ago < calm:
            return (f"spike in trend direction just happened ({bars_ago + 1} "
                    f"bar(s) ago) — waiting {calm} bars before continuation")
    return None


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


def retest_entry(df: pd.DataFrame, trend: str, config: dict) -> bool:
    """THE core rule: in an uptrend (HH/HL) buy the RETEST; in a downtrend
    (LL/LH) sell the RETEST. A retest = price pulled back against the trend
    (a counter-trend candle OR a touch of the EMA50 zone) and the latest
    candle resumed the trend with conviction, closing beyond the previous
    bar's extreme on the right side of the EMA50."""
    if not config.get("pullback_enabled", True):
        return False
    look = config["pullback_lookback"]
    last = df.iloc[-1]
    prev = df.iloc[-2]
    recent = df.iloc[-(look + 1):-1]     # bars before the current candle
    zone = config.get("retest_zone_atr", 0.3) * float(last["atr"])

    # The resume candle must have real conviction — no wick-spike bodies.
    rng = float(last["high"] - last["low"])
    body = abs(float(last["close"] - last["open"]))
    if rng <= 0 or body / rng < config["min_body_ratio"]:
        return False

    if trend == ms.UPTREND:
        pulled = (bool((recent["close"] < recent["open"]).any())
                  or bool((recent["low"] <= recent["ema_fast"] + zone).any()))
        resumed = (last["close"] > last["open"]
                   and last["close"] > prev["high"]
                   and last["close"] > last["ema_fast"])
        return pulled and resumed
    if trend == ms.DOWNTREND:
        pulled = (bool((recent["close"] > recent["open"]).any())
                  or bool((recent["high"] >= recent["ema_fast"] - zone).any()))
        resumed = (last["close"] < last["open"]
                   and last["close"] < prev["low"]
                   and last["close"] < last["ema_fast"])
        return pulled and resumed
    return False


def candles_aligned(df: pd.DataFrame, trend: str, n: int) -> bool:
    """Option B trigger: momentum must match the structure direction.

    Realistic rule (a strict 'N candles all one color' misses real moves
    because of tiny doji pullbacks): the LAST candle must be in the trend
    direction, at least n-1 of the last n candles must agree, and the net
    move across the window must also point with the trend."""
    if n < 1 or len(df) < n:
        return False
    recent = df.iloc[-n:]
    greens = (recent["close"] > recent["open"])
    net = float(recent["close"].iloc[-1] - recent["open"].iloc[0])
    if trend == ms.UPTREND:
        return bool(greens.iloc[-1]) and int(greens.sum()) >= n - 1 and net > 0
    if trend == ms.DOWNTREND:
        reds = ~greens
        return bool(reds.iloc[-1]) and int(reds.sum()) >= n - 1 and net < 0
    return False


def hybrid_stops(direction: str, close: float, structure, atr_value: float,
                 config: dict) -> tuple[float, float] | None:
    """Fixed pip TP/SL for hybrid scalps. SL may tighten to the last swing
    when that is closer than the fixed pip stop (never wider than hybrid_sl)."""
    pip = config.get("pip_size", 0.10)
    sl_dist = config["hybrid_sl_pips"] * pip
    tp_dist = config["hybrid_tp_pips"] * pip
    buffer = config["sl_atr_buffer"] * atr_value
    max_sl = config["max_sl_atr"] * atr_value

    if direction == "BUY":
        sl = close - sl_dist
        if structure.last_swing_low is not None:
            struct_sl = structure.last_swing_low.price - buffer
            struct_risk = close - struct_sl
            if 0 < struct_risk < sl_dist and struct_risk <= max_sl:
                sl = struct_sl
        risk = close - sl
        if risk <= 0:
            return None
        tp = close + tp_dist
    else:
        sl = close + sl_dist
        if structure.last_swing_high is not None:
            struct_sl = structure.last_swing_high.price + buffer
            struct_risk = struct_sl - close
            if 0 < struct_risk < sl_dist and struct_risk <= max_sl:
                sl = struct_sl
        risk = sl - close
        if risk <= 0:
            return None
        tp = close - tp_dist

    if risk > max_sl:
        return None
    return sl, tp


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

    # 3. Spike gate is applied AFTER the trend is known (direction-aware) —
    #    see below. Big with-trend candles must not freeze a trend day.

    # 4. Trend strength + sideways lockout
    if last["adx"] < config["adx_min"]:
        return None, f"market not trending (ADX {last['adx']:.1f} < {config['adx_min']})"
    range_reason = sideways_reason(df, config)
    if range_reason:
        return None, range_reason

    # 5. M5 MARKET STRUCTURE IS KING (the user's core rule):
    #    HH/HL -> uptrend -> BUY only.  LL/LH -> downtrend -> SELL only.
    structure = ms.analyze(df, config["swing_lookback"])
    ema_trend = None
    if last["close"] > last["ema_fast"] > last["ema_slow"]:
        ema_trend = ms.UPTREND
    elif last["close"] < last["ema_fast"] < last["ema_slow"]:
        ema_trend = ms.DOWNTREND

    if structure.trend in (ms.UPTREND, ms.DOWNTREND):
        trade_trend = structure.trend
        # Sanity: the EMA stack must never point the OPPOSITE way.
        if ema_trend is not None and ema_trend != trade_trend:
            return None, (f"M5 structure says {trade_trend} but EMAs say "
                          f"{ema_trend} — conflicting, waiting")
    elif ema_trend is not None:
        # Structure unclear between swings — the EMA stack carries the trend.
        trade_trend = ema_trend
    else:
        h = htf_bias if htf_bias in (ms.UPTREND, ms.DOWNTREND) else htf_trend(df, config)
        if h is None:
            return None, "no trend on M5 (structure + EMAs unclear) — waiting"
        trade_trend = h

    # 6. Higher timeframes are a VETO, not a requirement: only skip the
    #    trade when D1/H4/H1 clearly point the OPPOSITE way. Mixed/neutral
    #    higher timeframes never block an M5 structure trade.
    if htf_bias in (ms.UPTREND, ms.DOWNTREND) and htf_bias != trade_trend:
        return None, (f"M5 wants {trade_trend} but D1/H4/H1 clearly say "
                      f"{htf_bias} — not fighting the big picture")

    # 6b. Direction-aware spike gate: counter-trend spikes = full pause;
    #     with-trend spikes only need a short calm-down.
    spike_reason = spike_gate(df, trade_trend, config)
    if spike_reason:
        return None, spike_reason

    atr_value = float(last["atr"])
    close = float(last["close"])
    rr = config["min_reward_risk"]
    buffer = config["sl_atr_buffer"] * atr_value
    max_sl = config["max_sl_atr"] * atr_value
    fallback = config["fallback_sl_atr"] * atr_value

    direction = "BUY" if trade_trend == ms.UPTREND else "SELL"

    # ----- Option B: hybrid scalping (structure + aligned candles + fixed pips) -----
    if config.get("entry_mode") == "hybrid":
        n = config.get("hybrid_candle_bars", 3)
        if not candles_aligned(df, trade_trend, n):
            return None, (f"trend {trade_trend} but last {n} candles not aligned "
                          "— waiting for momentum")

        # Real trends run on the right side of BOTH EMAs; in a range price
        # oscillates around them (this stops borderline range-edge entries).
        if direction == "BUY" and not (close > float(last["ema_fast"])
                                       and close > float(last["ema_slow"])):
            return None, "price not above both EMAs — momentum not confirmed"
        if direction == "SELL" and not (close < float(last["ema_fast"])
                                        and close < float(last["ema_slow"])):
            return None, "price not below both EMAs — momentum not confirmed"

        # RSI here is a PARABOLIC guard only. In a strong M5 trend RSI lives
        # in the extreme zone for hours — that is exactly when the money is
        # made, so the hybrid mode uses wider bands than structure mode.
        rsi_hi = config.get("hybrid_rsi_overbought", 90)
        rsi_lo = config.get("hybrid_rsi_oversold", 10)
        if direction == "BUY" and last["rsi"] > rsi_hi:
            return None, f"RSI {last['rsi']:.0f} parabolic — not buying this candle"
        if direction == "SELL" and last["rsi"] < rsi_lo:
            return None, f"RSI {last['rsi']:.0f} parabolic — not selling this candle"

        conf = confidence_score(last, direction, config)
        min_conf = config.get("hybrid_min_confidence", 40.0)
        if conf < min_conf:
            return None, (f"setup confidence {conf:.0f} < {min_conf:.0f}"
                          " — watching, not trading")

        stops = hybrid_stops(direction, close, structure, atr_value, config)
        if stops is None:
            return None, "hybrid stop too wide — skipping"
        sl, tp = stops
        frame_note = ("M5 structure + D1/H4/H1 aligned" if htf_bias == trade_trend
                      else "M5 structure")
        return (
            Signal(direction, close, sl, tp,
                   f"{trade_trend} hybrid ({n} candles + {frame_note}) "
                   f"TP {config['hybrid_tp_pips']} pip",
                   confidence=conf),
            f"{direction.lower()} hybrid signal",
        )

    # ----- Classic structure mode: BOS / retest -----
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

    # 7. Trigger b) RETEST entry — buy the retest in an uptrend, sell the
    #    retest in a downtrend (the user's core entry rule).
    if trigger is None:
        if retest_entry(df, trade_trend, config):
            trigger = ("RETEST", "retest of the trend resumed (buy dip / sell rally)")
        elif structure.bos is not None and structure.bos != (
                "BULL" if trade_trend == ms.UPTREND else "BEAR"):
            return None, "BOS against the trend — ignored (counter-trend trades forbidden)"
        else:
            return None, (f"trend {trade_trend} confirmed but no entry trigger "
                          "(no BOS, no retest resume yet)")

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
    frame_note = ("M5 structure + D1/H4/H1 aligned" if htf_bias == trade_trend
                  else "M5 structure")
    return (
        Signal(direction, close, sl, tp,
               f"{trade_trend} trend ({frame_note}) + {detail}",
               confidence=conf),
        f"{direction.lower()} signal ({kind})",
    )
