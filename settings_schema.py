"""Editable bot settings shown in the Control Panel settings window."""

# (section, [(key, label, type, hint), ...])
# Types: float, int, str, bool, hours, windows, password

SETTINGS_SECTIONS = [
    ("Trading", [
        ("daily_target_pct", "Daily profit target %", "float", "Stop for the day at this gain"),
        ("max_trades_per_day", "Max trades per day", "int", "0 = unlimited until target"),
        ("min_reward_risk", "Take-profit (× risk)", "float", "Structure mode only"),
        ("entry_mode", "Entry mode", "str", "stop_ladder / hybrid / structure"),
        ("ladder_tp_pips", "Ladder TP (pips)", "int", "Fixed take-profit per stop trade"),
        ("ladder_gap_pips", "Ladder gap (pips)", "int", "Space before the next stop"),
        ("ladder_entry_offset_pips", "First stop offset (pips)", "int", "How far first stop sits beyond price"),
        ("ladder_sl_pips", "Ladder SL (pips)", "int", "Hard stop opposite the trade"),
        ("ladder_prev_margin_pips", "Prev low/high margin (pips)", "int", "Stop ladder 20–30 pips before swing"),
        ("ladder_dual_sides", "Dual grid (BUY+SELL)", "bool", "Arm both sides; first fill cancels opposite"),
        ("ladder_legs", "Stops per side", "int", "How many Buy Stops and Sell Stops to arm"),
        ("hybrid_sl_atr", "Stop size (× ATR)", "float", "SL scales with volatility"),
        ("hybrid_min_sl_pips", "Min SL (pips)", "int", "Stop never tighter than this"),
        ("hybrid_max_sl_pips", "Max SL (pips)", "int", "Stop never wider than this"),
        ("hybrid_tp_r", "TP (× risk)", "float", "When basket is off"),
        ("hybrid_candle_bars", "Hybrid candle count", "int", "Aligned candles required"),
        ("basket_enabled", "Banker + runner", "bool", "2 trades per signal, runner trails"),
        ("basket_trades", "Basket size", "int", "Positions per signal when basket on"),
        ("trading_hours", "Trading hours", "hours", "Server time, e.g. 1-23"),
        ("blackout_windows", "News blackouts", "windows", "e.g. 15:15-15:50, 16:55-17:20"),
    ]),
    ("Risk", [
        ("risk_per_trade_pct", "Risk per trade %", "float", "% of equity if SL is hit"),
        ("recovery_risk_pct", "Recovery risk %", "float", "After drawdown guard"),
        ("max_drawdown_pct", "Max drawdown %", "float", "Triggers observe/recover mode"),
        ("min_confidence", "Min confidence score", "float", "0–100, below = no trade"),
        ("high_confidence_score", "High-confidence score", "float", "Setups above get bigger risk"),
        ("high_confidence_risk_pct", "High-confidence risk %", "float", "Risk on exceptional setups"),
    ]),
    ("Loss guards", [
        ("daily_loss_limit_pct", "Daily loss limit %", "float", "Day stops at this loss"),
        ("profit_lock_trigger_pct", "Profit lock at %", "float", "Start protecting gains"),
        ("profit_lock_giveback_pct", "Max giveback %", "float", "Of peak day profit"),
        ("consec_loss_count", "Loss streak to pause", "int", "Consecutive losses"),
        ("loss_pause_bars", "Pause bars", "int", "5-min bars after streak"),
        ("pause_override_enabled", "BOS breaks cooldown", "bool", "Strong fresh setup re-enters early"),
        ("pause_override_confidence", "Cooldown-break confidence", "float", "Min score to break the pause"),
        ("max_spread_points", "Max spread (points)", "int", "Absolute blowout cap"),
        ("max_spread_sl_frac", "Spread vs SL (frac)", "float", "Spread <= this x stop distance"),
        ("friday_close_hour", "Friday close hour", "int", "Close before weekend"),
    ]),
    ("Market filters", [
        ("adx_min", "Min ADX", "float", "Trend strength minimum"),
        ("chop_max", "Max choppiness", "float", "Above = sideways, no trade"),
        ("spike_atr_mult", "Spike size (× ATR)", "float", "Abnormal candle pause"),
        ("spike_pause_bars", "Spike pause bars", "int", "After COUNTER-trend spike"),
        ("spike_calm_bars", "Spike calm bars", "int", "After WITH-trend spike"),
        ("hybrid_min_confidence", "Hybrid min confidence", "float", "0–100 gate for hybrid entries"),
        ("max_ema_distance_atr", "Late-entry guard (× ATR)", "float", "Never chase past this from EMA50"),
        ("pullback_enabled", "Pullback entries", "bool", "Buy-the-dip trigger on/off"),
        ("pullback_lookback", "Pullback lookback", "int", "Bars to find the dip"),
        ("topdown_enabled", "Top-down D1/H4/H1", "bool", "Pro multi-timeframe bias"),
        ("impulse_enabled", "Impulse entries", "bool", "Trade giant candles immediately"),
        ("sr_enabled", "S/R guard", "bool", "Never trade into a major level"),
        ("sr_reversal_enabled", "S/R bounce entries", "bool", "Trade the flip at big levels"),
    ]),
    ("Trade management", [
        ("protect_rr", "Half-risk at (R)", "float", "Stage 1 trigger"),
        ("breakeven_rr", "Breakeven at (R)", "float", "Stage 2 trigger"),
        ("lock_rr", "Lock profit at (R)", "float", "Stage 3 trigger"),
        ("trail_atr_mult", "Trail (× ATR)", "float", "Stage 4 distance"),
        ("time_stop_bars", "Time stop bars", "int", "Close flat trades"),
    ]),
    ("Startup test", [
        ("startup_test_enabled", "Run test on start", "bool", "0.01 BUY+SELL then close"),
        ("startup_test_volume", "Test lot size", "float", "Usually 0.01"),
        ("startup_test_seconds", "Test hold seconds", "int", "Before closing test trades"),
    ]),
    ("Email alerts (Resend)", [
        ("email_enabled", "Email enabled", "bool", "Turn alerts on/off"),
        ("email_to", "Send alerts to", "str", "Any email — change anytime"),
        ("email_from", "Send from", "str", "Must use your verified domain"),
        ("resend_api_key", "Resend API key", "password", "From resend.com/api-keys (starts with re_)"),
    ]),
]


def format_setting(key: str, value, ftype: str) -> str:
    if value is None:
        return ""
    if ftype == "hours" and isinstance(value, (list, tuple)):
        return f"{value[0]}-{value[1]}"
    if ftype == "windows" and isinstance(value, list):
        return ", ".join(value)
    if ftype == "bool":
        return "1" if value else "0"
    return str(value)


def parse_setting(key: str, raw: str, ftype: str):
    raw = raw.strip()
    if ftype == "bool":
        return raw.lower() in ("1", "true", "yes", "on")
    if ftype == "str":
        return raw
    if ftype == "int":
        return int(raw)
    if ftype == "float":
        return float(raw)
    if ftype == "password":
        return raw or None
    if ftype == "hours":
        start, end = raw.split("-")
        start, end = int(start), int(end)
        if not (0 <= start < end <= 24):
            raise ValueError("hours must be 0-24 with start < end")
        return [start, end]
    if ftype == "windows":
        if not raw:
            return []
        windows = [w.strip() for w in raw.split(",") if w.strip()]
        for w in windows:
            a, b = w.split("-")
            for t in (a, b):
                h, m = t.split(":")
                if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
                    raise ValueError(f"bad time in '{w}'")
        return windows
    raise ValueError(f"unknown type {ftype}")
