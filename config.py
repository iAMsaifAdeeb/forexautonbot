"""
Central configuration for the XAUUSD M15 trading bot.
Every rule from the trading plan maps to a setting here.
"""

CONFIG = {
    # ----- Instrument / timeframe -----
    "symbol": "XAUUSD",          # Rule 2: Gold vs USD
    "timeframe_minutes": 15,     # Rule 1: 15-minute chart
    "bars_to_load": 500,         # history window for analysis

    # ----- Daily profit target -----
    "daily_target_pct": 5.0,     # Rule 3/8: stop for the day once equity is +5%

    # ----- Risk management -----
    "risk_per_trade_pct": 1.0,   # % of equity risked per trade (Rule 4/10)
    "recovery_risk_pct": 0.5,    # reduced risk while recovering a drawdown
    "max_drawdown_pct": 10.0,    # Rule 11: 10% loss triggers observe/recover mode
    "observe_bars": 8,           # bars to "wait and observe" after hitting drawdown
    "max_trades_per_day": 3,     # Rule 7: not a lot of trades
    "max_open_positions": 1,     # one position at a time
    "min_reward_risk": 2.0,      # take-profit at 2x the stop distance

    # ----- Strategy quality filters (Rule 9) -----
    "ema_fast": 50,
    "ema_slow": 200,
    "adx_period": 14,
    "adx_min": 25,               # only trade when the market is clearly trending

    # ----- Sideways-market lockout (NO trading in ranges, period) -----
    "chop_period": 14,
    "chop_max": 55.0,            # Choppiness Index above this = sideways -> no work
    "ema_separation_atr": 0.30,  # EMA50/200 tangled closer than 0.3 ATR = flat market
    "range_box_bars": 36,        # look at the last 9 hours...
    "range_box_atr": 5.0,        # ...if the whole span < 5 ATR, price is boxed -> no work
    "atr_period": 14,
    "swing_lookback": 3,         # fractal size for swing high/low detection
    "sl_atr_buffer": 0.5,        # stop placed beyond the swing by 0.5 * ATR
    "max_sl_atr": 3.0,           # if the swing stop is wider than this, fall back to ATR stop
    "fallback_sl_atr": 2.0,      # ATR-based stop distance used in the fallback

    # ----- Fakeout protection -----
    "min_body_ratio": 0.40,      # breakout candle body must be >= 40% of its range
    "bos_margin_atr": 0.10,      # close must clear the level by 0.1 * ATR (no paper-thin breaks)
    "max_chase_atr": 1.0,        # never enter more than 1 ATR past the broken level
    "volume_confirm_mult": 1.05, # breakout volume must exceed 1.05x its 20-bar average
    "volume_sma_period": 20,
    "fakeout_memory_bars": 30,   # a level that already faked out recently is skipped

    # ----- Spike / news protection -----
    "spike_atr_mult": 2.5,       # candle range > 2.5 * ATR = abnormal spike
    "spike_pause_bars": 6,       # no entries for 6 bars (1.5h) after a spike
    # Server-time windows where entries are forbidden (typical high-impact
    # US news at 15:30 and 17:00 on UTC+3 brokers). Format "HH:MM-HH:MM".
    "blackout_windows": ["15:15-15:50", "16:55-17:20"],

    # ----- Higher-timeframe confirmation -----
    "htf_minutes": 60,           # confirm the trend on H1 before trading M15
    "htf_ema_fast": 20,
    "htf_ema_slow": 50,

    # ----- Momentum exhaustion filter -----
    "rsi_period": 14,
    "rsi_overbought": 80,        # no buys into a parabolic overbought market
    "rsi_oversold": 20,          # no sells into a parabolic oversold market

    # ----- Trade management -----
    "breakeven_rr": 1.0,         # move stop to entry once price moves 1R in our favor
    "trail_atr_mult": 2.0,       # then trail the stop 2 * ATR behind price

    # ----- Sessions (server time hours, inclusive start / exclusive end) -----
    # Gold moves best during London + New York. Avoid the dead Asian drift.
    "trading_hours": (7, 21),

    # ----- Execution -----
    "magic_number": 20260707,
    "deviation_points": 30,
    "poll_seconds": 10,          # how often the main loop wakes up
    "state_file": "bot_state.json",
    "log_file": "bot.log",

    # ----- MT5 account (leave None to use the terminal's logged-in account) -----
    "mt5_login": None,
    "mt5_password": None,
    "mt5_server": None,
    "mt5_terminal_path": None,   # e.g. r"C:\\Program Files\\MetaTrader 5\\terminal64.exe"
}
