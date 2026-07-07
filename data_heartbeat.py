"""Lightweight data-flow marker — the panel reads this while the bot holds
the MT5 API connection (no second initialize needed)."""

import json
import os
from datetime import datetime


def heartbeat_path(config: dict) -> str:
    name = config.get("heartbeat_file", "data_heartbeat.json")
    if os.path.isabs(name):
        return name
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, name)


def write_heartbeat(config: dict, *, equity: float | None = None,
                    last_bar=None, rates_ok: bool = True):
    payload = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "symbol": config.get("symbol"),
        "rates_ok": rates_ok,
        "equity": equity,
        "last_bar": str(last_bar) if last_bar is not None else None,
    }
    path = heartbeat_path(config)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except OSError:
        pass


def heartbeat_fresh(config: dict, within_seconds: float = 120) -> bool:
    path = heartbeat_path(config)
    if not os.path.isfile(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not data.get("rates_ok", False):
            return False
        ts = datetime.fromisoformat(data["ts"])
        age = (datetime.now() - ts).total_seconds()
        return age <= within_seconds
    except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError):
        return False
