"""MT5 health checks for the control panel (no bot subprocess interference)."""

import os
import subprocess
from datetime import datetime

from data_heartbeat import heartbeat_fresh


def terminal_running() -> bool:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq terminal64.exe"],
            capture_output=True, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return "terminal64.exe" in result.stdout
    except OSError:
        return False


def _parse_log_time(line: str) -> datetime | None:
    try:
        return datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def bot_data_flowing(log_path: str, within_seconds: float = 90) -> bool:
    """True when the bot log shows a recent successful bar / signal (not just
    warnings). Used while the bot subprocess holds the MT5 API connection."""
    if not os.path.isfile(log_path):
        return False
    try:
        size = os.path.getsize(log_path)
        with open(log_path, "rb") as f:
            f.seek(max(0, size - 12000))
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return False

    now = datetime.now()
    last_good = None
    last_bad = None
    for line in reversed(text.splitlines()):
        ts = _parse_log_time(line)
        if ts is None:
            continue
        age = (now - ts).total_seconds()
        if age > within_seconds * 2:
            break
        if any(k in line for k in (
            "| bot | Bar ",
            "| bot | Data OK",
            "SIGNAL:",
            "Connected to MT5",
            "| bot | equity ",
        )):
            if last_good is None:
                last_good = age
        if "No rates received" in line or "Reconnecting to MetaTrader" in line:
            if last_bad is None:
                last_bad = age

    if last_good is not None and last_good <= within_seconds:
        if last_bad is None or last_good <= last_bad:
            return True
    return False


def api_data_ok(config: dict) -> bool:
    """Live API probe — only call when the bot is NOT running (otherwise the
    bot subprocess owns the MT5 pipe and we would disrupt it)."""
    try:
        import MetaTrader5 as mt5
    except ImportError:
        return False

    symbol = config.get("symbol", "XAUUSD")
    fallbacks = list(config.get("symbol_fallbacks") or [])
    candidates = []
    seen = set()
    for sym in [symbol] + fallbacks:
        if sym and sym not in seen:
            seen.add(sym)
            candidates.append(sym)
    path_kw = {}
    if config.get("mt5_terminal_path"):
        path_kw["path"] = config["mt5_terminal_path"]

    ok = False
    try:
        if not mt5.initialize(**path_kw):
            kwargs = dict(path_kw)
            login = config.get("mt5_login")
            if login:
                kwargs["login"] = int(login)
                kwargs["password"] = config.get("mt5_password")
                kwargs["server"] = config.get("mt5_server")
            if not mt5.initialize(**kwargs):
                return False
        for sym in candidates:
            if not mt5.symbol_select(sym, True):
                continue
            rates = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M5, 0, 10)
            if rates is not None and len(rates) > 0:
                ok = True
                break
        else:
            ok = False
    finally:
        try:
            mt5.shutdown()
        except Exception:
            pass
    return ok


def mt5_status(config: dict, log_path: str, bot_running: bool) -> tuple[str, str]:
    """Returns (label, colour_key) where colour_key is green / gold / red."""
    if not terminal_running():
        return "  ●  MT5 OFFLINE  ", "red"
    if bot_running:
        # Bot owns the MT5 pipe — use heartbeat file + log tail (not a 2nd API).
        if heartbeat_fresh(config):
            return "  ●  MT5 ONLINE  ", "green"
        if bot_data_flowing(log_path):
            return "  ●  MT5 ONLINE  ", "green"
        return "  ●  MT5 NO DATA  ", "gold"
    if api_data_ok(config):
        return "  ●  MT5 ONLINE  ", "green"
    return "  ●  MT5 NO DATA  ", "gold"
