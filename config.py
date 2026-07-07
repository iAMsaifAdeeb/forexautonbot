"""
Central configuration for Gold Genious — the XAUUSD M5 trading bot.
Every rule from the trading plan maps to a setting here.

Values below are the defaults. Anything saved from the Control Panel
(settings.json) overrides them, so you never need to edit this file by hand.
"""

import json
import os

CONFIG = {
    # ----- Instrument / timeframe -----
    "symbol": "XAUUSD",          # Gold vs USD
    "symbol_fallbacks": [        # Exness / other brokers may suffix the symbol
        "XAUUSDm", "XAUUSD.", "XAUUSDz", "GOLD",
    ],
    "timeframe_minutes": 5,      # primary trading chart: M5
    "bars_to_load": 900,         # history window (must cover the HTF EMAs)

    # ----- Daily profit target -----
    "daily_target_pct": 5.0,     # Rule 3/8: stop for the day once equity is +5%

    # ----- Risk management -----
    "risk_per_trade_pct": 1.0,   # % of equity risked per trade (Rule 4/10)
    "recovery_risk_pct": 0.5,    # reduced risk while recovering a drawdown
    "max_drawdown_pct": 10.0,    # Rule 11: 10% loss triggers observe/recover mode
    "observe_bars": 24,          # bars to "wait and observe" after drawdown (2h on M5)
    "max_trades_per_day": 0,     # 0 = unlimited: keep trading until the daily
                                 # target is reached. Every trade still needs
                                 # a full-confluence reason — no reason, no trade.
    "max_open_positions": 5,     # hybrid: up to 5 small scalps while structure holds
    "min_reward_risk": 2.0,      # used by structure mode (BOS/retest); hybrid uses fixed pips

    # ----- Entry mode -----
    # "hybrid" (Option B): M5 structure + 3 aligned candles -> fixed pip TP/SL scalps.
    # "structure": classic BOS/retest entries with optional Fable 5 basket.
    "entry_mode": "hybrid",
    "hybrid_tp_pips": 25,        # take profit at 25 pips (~$2.50 on gold, pip_size 0.10)
    "hybrid_sl_pips": 20,        # stop loss at 20 pips (~$2.00)
    "hybrid_candle_bars": 3,     # momentum window: last candle + majority aligned
    "hybrid_min_confidence": 40.0,  # hybrid entries use their own (lower) gate
    "hybrid_rsi_overbought": 90, # parabolic-only guard (trend RSI stays extreme)
    "hybrid_rsi_oversold": 10,
    "pip_size": 0.10,            # 1 pip on XAUUSD (Exness / most brokers)

    # ----- Basket entries (structure mode only) -----
    "basket_enabled": False,         # hybrid uses one trade per signal
    "basket_trades": 5,              # positions per signal when basket_enabled
    "basket_tp_r": [1.0, 1.5, 2.0, 3.0],
    "basket_runner_tp_r": 10.0,
    "basket_state_file": "basket_state.json",

    # ----- Loss guards (fund protection) -----
    "daily_loss_limit_pct": 3.0,     # day stops the moment the day is -3%
    "consec_loss_count": 3,          # after 3 losses in a row...
    "loss_pause_bars": 24,           # ...cool down for 24 bars (2 hours on M5)
    "profit_lock_trigger_pct": 2.0,  # once the day peaked at +2%...
    "profit_lock_giveback_pct": 50.0,# ...never give back more than half of it
    "max_spread_points": 60,         # skip entries when the spread is blown out
    "friday_close_hour": 21,         # close everything before the weekend gap

    # ----- AI confidence engine -----
    "min_confidence": 50.0,          # signals scoring below this are watched, not traded
    "high_confidence_score": 80.0,   # exceptional setups...
    "high_confidence_risk_pct": 1.5, # ...earn a larger (but still capped) risk %

    # ----- Strategy quality filters (Rule 9) -----
    "ema_fast": 50,
    "ema_slow": 200,
    "adx_period": 14,
    "adx_min": 20,               # only trade when the market is trending

    # ----- Sideways-market lockout (NO trading in ranges, period) -----
    "chop_period": 14,
    "chop_max": 58.0,            # Choppiness Index above this = sideways -> no work
    "ema_separation_atr": 0.30,  # EMA50/200 tangled closer than 0.3 ATR = flat market
    "range_box_bars": 60,        # look at the last 5 hours (M5)...
    "range_box_atr": 5.0,        # ...if the whole span < 5 ATR, price is boxed -> no work
    "atr_period": 14,
    "swing_lookback": 3,         # fractal size for swing high/low detection
    "sl_atr_buffer": 0.5,        # stop placed beyond the swing by 0.5 * ATR
    "max_sl_atr": 3.0,           # if the swing stop is wider than this, fall back to ATR stop
    "fallback_sl_atr": 2.0,      # ATR-based stop distance used in the fallback

    # ----- Fakeout protection -----
    "min_body_ratio": 0.35,      # breakout candle body must be >= 35% of its range
    "bos_margin_atr": 0.10,      # close must clear the level by 0.1 * ATR (no paper-thin breaks)
    "max_chase_atr": 1.0,        # never enter more than 1 ATR past the broken level
    "volume_confirm_mult": 1.0,  # breakout volume must be at least average
    "volume_sma_period": 20,
    "fakeout_memory_bars": 60,   # a level that already faked out recently is skipped (5h on M5)

    # ----- Retest entries (2nd trigger: buy the dip / sell the rally) -----
    "pullback_enabled": True,
    "pullback_lookback": 6,      # a retest (dip/rally/EMA touch) within these bars...
                                 # ...then a candle resuming the trend = entry
    "retest_zone_atr": 0.3,      # EMA50 touch counts within this ATR zone

    # ----- Spike / news protection (direction-aware since V8) -----
    "spike_atr_mult": 2.5,       # candle range > 2.5 * ATR = abnormal spike
    "spike_pause_bars": 18,      # COUNTER-trend spike: no entries for 18 bars
    "spike_calm_bars": 2,        # WITH-trend spike: wait only 2 bars, then
                                 # trade the continuation (big candles ARE the trend)
    # Server-time windows where entries are forbidden (typical high-impact
    # US news at 15:30 and 17:00 on UTC+3 brokers). Format "HH:MM-HH:MM".
    "blackout_windows": ["15:15-15:50", "16:55-17:20"],

    # ----- Higher-timeframe confirmation -----
    # Top-down analysis (the pro routine): previous D1 candle -> H4 trend ->
    # H1 trend. 2 of 3 must agree to give a directional bias; the M5 chart
    # then only times the entry in that direction.
    "topdown_enabled": True,
    # Fallback M30 resample gate (used offline/in tests when no live data):
    "htf_minutes": 30,
    "htf_ema_fast": 20,
    "htf_ema_slow": 50,

    # ----- Momentum exhaustion filter -----
    "rsi_period": 14,
    "rsi_overbought": 80,        # no buys into a parabolic overbought market
    "rsi_oversold": 20,          # no sells into a parabolic oversold market

    # ----- Trade management: staged protection ladder -----
    # Stage 1: +0.5R -> cut the remaining risk in half.
    # Stage 2: +1.0R -> breakeven (+ small buffer so spread can't make it a loss).
    # Stage 3: +1.5R -> lock in +0.5R of profit no matter what.
    # Stage 4: beyond -> trail behind market structure AND ATR to ride the trend.
    "protect_rr": 0.5,           # stage 1 trigger (in R multiples)
    "breakeven_rr": 1.0,         # stage 2 trigger
    "breakeven_buffer_atr": 0.1, # breakeven sits this far on the profit side
    "lock_rr": 1.5,              # stage 3 trigger
    "lock_keep_r": 0.5,          # profit locked at stage 3 (in R)
    "trail_atr_mult": 2.0,       # stage 4: ATR trail distance
    "trail_struct_buffer_atr": 0.3,  # stage 4: buffer beyond the trailing swing
    "min_trail_gap_atr": 0.5,    # never trail closer than this to price
    "time_stop_bars": 30,        # close trades stuck below +0.5R after 2.5h (M5)

    # ----- Sessions (server time hours, inclusive start / exclusive end) -----
    # Nearly around the clock — the sideways lockout keeps the bot out of the
    # dead hours anyway. Only the illiquid rollover hour is excluded.
    "trading_hours": (1, 23),

    # ----- Execution -----
    "magic_number": 20260707,
    "deviation_points": 30,
    "poll_seconds": 10,          # how often the main loop wakes up
    "state_file": "bot_state.json",
    "heartbeat_file": "data_heartbeat.json",
    "log_file": "bot.log",

    # ----- Startup connectivity test -----
    "startup_test_enabled": True,
    "startup_test_volume": 0.01,
    "startup_test_seconds": 3,       # seconds between open and close per leg
    "startup_test_required": False,  # False = bot keeps running even if test fails

    # ----- Email alerts (Resend — domain usdtlocal.com) -----
    "email_enabled": True,
    "email_to": "saifadeeb@gmail.com",          # change in ⚙ settings anytime
    "email_from": "Gold Genious <bot@usdtlocal.com>",
    "resend_api_key": None,                     # paste your Resend API key in ⚙ settings

    # ----- MT5 account (leave None to use the terminal's logged-in account) -----
    "mt5_login": None,
    "mt5_password": None,
    "mt5_server": None,
    "mt5_terminal_path": None,   # e.g. r"C:\\Program Files\\MetaTrader 5\\terminal64.exe"
}

# Overrides saved by the Control Panel take priority over the defaults above.
#
# IMPORTANT: unless the user explicitly tuned strategy values in the ⚙
# settings window ("user_tuned": true), only ACCOUNT-level keys are applied.
# This stops a stale settings.json (written by an older version) from
# silently overriding improved strategy defaults after an update.
_ACCOUNT_KEYS = {
    "symbol", "mt5_login", "mt5_password", "mt5_server", "mt5_terminal_path",
    "email_enabled", "email_to", "email_from", "resend_api_key",
}
_SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
if os.path.exists(_SETTINGS_FILE):
    try:
        with open(_SETTINGS_FILE, "r", encoding="utf-8") as _f:
            _overrides = json.load(_f)
        if not _overrides.get("user_tuned"):
            _overrides = {k: v for k, v in _overrides.items() if k in _ACCOUNT_KEYS}
        _overrides.pop("user_tuned", None)
        CONFIG.update(_overrides)
    except (json.JSONDecodeError, OSError):
        pass  # unreadable settings file -> fall back to defaults
