"""First-run install checklist for the control panel."""

import importlib.util
import json
import os
import subprocess

from mt5_launcher import find_mt5_exe

CHECKLIST_FILE = "install_checklist_done.json"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CHECKS = [
    {
        "id": "python",
        "label": "Python 3.12 (64-bit)",
        "download": "https://www.python.org/downloads/",
        "hint": "Tick 'Add Python to PATH' during install.",
    },
    {
        "id": "pip_packages",
        "label": "Bot libraries (MetaTrader5, pandas, numpy)",
        "download": None,
        "hint": "Run: pip install -r requirements.txt",
    },
    {
        "id": "mt5",
        "label": "MetaTrader 5 terminal",
        "download": "https://www.metatrader5.com/en/download",
        "hint": "Install from your broker or MetaQuotes, then log in once.",
    },
    {
        "id": "bot_files",
        "label": "Bot files (main.py present)",
        "download": "https://github.com/iAMsaifAdeeb/forexautonbot",
        "hint": "Copy the project folder or press UPDATE FROM GITHUB.",
    },
]


def _python_ok() -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["python", "--version"], capture_output=True, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode != 0:
            # try common local path
            local = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs",
                                 "Python", "Python312", "python.exe")
            if os.path.isfile(local):
                return True, "Python 3.12 found"
            return False, "Python not found"
        ver = result.stdout.strip() or result.stderr.strip()
        return True, ver
    except OSError:
        local = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs",
                             "Python", "Python312", "python.exe")
        if os.path.isfile(local):
            return True, "Python 3.12 found"
        return False, "Python not installed"


def _pip_ok() -> tuple[bool, str]:
    missing = []
    for pkg in ("MetaTrader5", "pandas", "numpy"):
        if importlib.util.find_spec(pkg) is None:
            missing.append(pkg)
    if missing:
        return False, "Missing: " + ", ".join(missing)
    return True, "All libraries installed"


def _mt5_ok(config: dict | None = None) -> tuple[bool, str]:
    path = find_mt5_exe(config)
    if path:
        return True, path
    return False, "terminal64.exe not found"


def _bot_files_ok(base_dir: str) -> tuple[bool, str]:
    main_py = os.path.join(base_dir, "main.py")
    if os.path.isfile(main_py):
        return True, main_py
    return False, "main.py missing"


def run_checks(base_dir: str, config: dict | None = None) -> list[dict]:
    runners = {
        "python": _python_ok,
        "pip_packages": _pip_ok,
        "mt5": lambda: _mt5_ok(config),
        "bot_files": lambda: _bot_files_ok(base_dir),
    }
    results = []
    for item in CHECKS:
        ok, detail = runners[item["id"]]()
        results.append({**item, "ok": ok, "detail": detail})
    return results


def all_passed(results: list[dict]) -> bool:
    return all(r["ok"] for r in results)


def is_checklist_done(base_dir: str) -> bool:
    path = os.path.join(base_dir, CHECKLIST_FILE)
    if not os.path.isfile(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return bool(data.get("completed"))
    except (json.JSONDecodeError, OSError):
        return False


def mark_checklist_done(base_dir: str):
    path = os.path.join(base_dir, CHECKLIST_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"completed": True}, f, indent=2)
