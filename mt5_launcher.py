"""Launch and close the MetaTrader 5 terminal on Windows."""

import os
import subprocess
import time

COMMON_MT5_PATHS = [
    r"C:\Program Files\MetaTrader 5\terminal64.exe",
    r"C:\Program Files (x86)\MetaTrader 5\terminal64.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "MetaTrader 5", "terminal64.exe"),
]


def _mt5():
    """Lazy import — avoids crashing the GUI at startup if numpy/MT5 isn't ready."""
    import MetaTrader5 as mt5
    return mt5


def find_mt5_exe(config: dict | None = None) -> str | None:
    if config and config.get("mt5_terminal_path"):
        path = config["mt5_terminal_path"]
        if os.path.isfile(path):
            return path
    for path in COMMON_MT5_PATHS:
        if os.path.isfile(path):
            return path
    return None


def is_mt5_running() -> bool:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq terminal64.exe"],
            capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return "terminal64.exe" in result.stdout
    except OSError:
        return False


def launch_mt5(config: dict | None = None, wait_seconds: int = 25) -> bool:
    if is_mt5_running():
        return True
    exe = find_mt5_exe(config)
    if not exe:
        return False
    subprocess.Popen([exe], creationflags=subprocess.CREATE_NO_WINDOW)
    for _ in range(wait_seconds):
        if is_mt5_running():
            time.sleep(3)  # give the terminal a moment to finish booting
            return True
        time.sleep(1)
    return False


def close_mt5(wait_seconds: int = 8) -> bool:
    if not is_mt5_running():
        return True
    subprocess.run(
        ["taskkill", "/IM", "terminal64.exe"],
        capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    for _ in range(wait_seconds):
        if not is_mt5_running():
            try:
                _mt5().shutdown()
            except Exception:
                pass
            return True
        time.sleep(1)
    return False


def wait_for_mt5_api(config: dict | None = None, timeout: int = 60) -> bool:
    """Poll until the MT5 Python API can connect."""
    mt5 = _mt5()
    kwargs = {}
    if config:
        if config.get("mt5_terminal_path"):
            kwargs["path"] = config["mt5_terminal_path"]
        if config.get("mt5_login"):
            kwargs["login"] = config["mt5_login"]
            kwargs["password"] = config["mt5_password"]
            kwargs["server"] = config["mt5_server"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        if mt5.initialize(**kwargs):
            mt5.shutdown()
            return True
        time.sleep(2)
    return False
