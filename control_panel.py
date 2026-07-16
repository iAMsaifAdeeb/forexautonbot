"""
GOLD GENIOUS — XAUUSD Auto Trader, Control Panel.

Run:  python control_panel.py   (or Desktop shortcut / run_panel.bat)

Do NOT use an old frozen Gold Genious.exe for the UI — it cannot update.
If an .exe exists, rebuild with build_exe.bat (thin launcher) or delete it.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import urllib.error
import urllib.request
import webbrowser
import zipfile
from datetime import datetime
from tkinter import messagebox, ttk

from mt5_launcher import close_mt5, launch_mt5, wait_for_mt5_api
from mt5_health import mt5_status
from settings_schema import SETTINGS_SECTIONS, format_setting, parse_setting
from system_check import (
    all_passed, is_checklist_done, mark_checklist_done, run_checks,
)

try:
    from version import VERSION as APP_VERSION
except ImportError:
    APP_VERSION = "V?"

# Bump with every Control Panel UI change (must match what you see on screen).
UI_BUILD = "V29"

BASE_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))

SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")
STATE_FILE = os.path.join(BASE_DIR, "bot_state.json")
LOG_FILE = os.path.join(BASE_DIR, "bot.log")
CONFIG_FILE = os.path.join(BASE_DIR, "config.py")
UPDATE_META_FILE = os.path.join(BASE_DIR, "update_meta.json")

GITHUB_REPO = "https://github.com/iAMsaifAdeeb/forexautonbot"
GITHUB_ZIP = "https://codeload.github.com/iAMsaifAdeeb/forexautonbot/zip/refs/heads/main"
# Local files the updater must NEVER overwrite (your settings, state, logs).
UPDATE_PROTECTED = {
    "settings.json", "bot_state.json", "bot.log", "test_state.json",
    "install_checklist_done.json", "update_meta.json", "basket_state.json",
    "data_heartbeat.json", "ladder_state.json", "ten_pips_state.json",
    "panel_auto_start.json",
}

STALE_EXE_NAMES = (
    "Gold Genious.exe",
    "XAUUSD Bot Control Panel.exe",
    "Gold Sniper.exe",
)

# ---------------------------------------------------------------- palette
BG = "#0a0d12"        # window
CARD = "#11161d"      # panels
EDGE = "#1e2833"      # panel borders
FIELD = "#0b0f14"     # entry background
FG = "#e9eef4"        # primary text
MUT = "#77879a"       # secondary text
GOLD = "#d9a441"
GOLD_SOFT = "#b98c39"
GREEN = "#35c98e"
RED = "#e85d5d"
BLUE = "#5b9cf5"

FONT = "Segoe UI"
MONO = "Consolas"

MODE_LABELS = {
    "NORMAL": ("Trading normally", GREEN),
    "TARGET_DONE": ("Daily target reached — resting", GOLD),
    "OBSERVE": ("Observing market after drawdown", BLUE),
    "RECOVERY": ("Recovery mode (reduced risk)", BLUE),
    "DAY_STOPPED": ("Day stopped — funds protected", RED),
}


def find_python() -> str:
    candidates = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Python",
                     "Python312", "python.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Python",
                     "Python311", "python.exe"),
        "python",
    ]
    for c in candidates:
        if c == "python" or os.path.exists(c):
            return c
    return "python"


def find_pythonw() -> str:
    py = find_python()
    pyw = py.replace("python.exe", "pythonw.exe")
    return pyw if os.path.isfile(pyw) else py


def kill_stale_panel_exes():
    """Stop old frozen panel EXEs that cannot show the new strategy UI."""
    for name in STALE_EXE_NAMES:
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", name],
                capture_output=True, timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception:
            pass


def delete_stale_panel_exes(folder: str) -> list[str]:
    """Remove frozen UI exes so users cannot reopen the old look by accident."""
    removed = []
    for name in STALE_EXE_NAMES:
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        try:
            os.remove(path)
            removed.append(name)
        except OSError:
            pass
    return removed


def redirect_frozen_exe_to_python():
    """If someone built/bundled an old full UI into an EXE, jump to .py."""
    if not getattr(sys, "frozen", False):
        return
    # Thin launcher_stub already starts .py — only redirect full frozen panels.
    # Detect by missing strategies_catalog next to exe OR by module layout.
    panel = os.path.join(BASE_DIR, "control_panel.py")
    if not os.path.isfile(panel):
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Gold Genious",
            "control_panel.py missing.\nRun SETUP.bat in the bot folder.")
        sys.exit(1)
    # Always prefer live .py over frozen UI code
    kill_stale_panel_exes()
    subprocess.Popen([find_pythonw(), panel], cwd=BASE_DIR)
    sys.exit(0)


def verify_panel_source() -> str | None:
    """Return an error string if this folder's control_panel.py is not V27+."""
    path = os.path.join(BASE_DIR, "control_panel.py")
    if not os.path.isfile(path):
        return "control_panel.py is missing — run SETUP.bat."
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            src = f.read()
    except OSError as exc:
        return f"Cannot read control_panel.py: {exc}"
    if "Famous Strategies" not in src or "UI_BUILD" not in src:
        return ("This folder still has an OLD Control Panel file.\n\n"
                "Close Gold Genious completely, run SETUP.bat, then open "
                "the Desktop shortcut (not an old .exe).")
    if not os.path.isfile(os.path.join(BASE_DIR, "strategies_catalog.py")):
        return ("strategies_catalog.py missing — press UPDATE or run SETUP.bat.")
    return None


def load_effective_config() -> dict:
    """Execute config.py so we show the same values the bot will use
    (defaults + any saved settings.json overrides)."""
    namespace = {"__file__": CONFIG_FILE}
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        exec(f.read(), namespace)
    return namespace["CONFIG"]


def hover(widget, normal: str, lit: str):
    widget.bind("<Enter>", lambda _e: widget.config(bg=lit))
    widget.bind("<Leave>", lambda _e: widget.config(bg=normal))


# Only the account is user-facing. Everything else is auto-managed.
ACCOUNT_FIELDS = [
    ("mt5_login", "Login", "opt_int", "MT5 account number"),
    ("mt5_password", "Password", "password", "Stored only on this computer"),
    ("mt5_server", "Server", "opt_str", "e.g.  Exness-MT5Real8"),
    ("symbol", "Gold symbol", str, "As your broker names it: XAUUSD / GOLD / XAUUSDm"),
]


class ControlPanel(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"Gold Genious — Strategies {UI_BUILD}")
        self.configure(bg=BG)
        self.minsize(480, 820)
        self.geometry("520x860")
        self.bot_process: subprocess.Popen | None = None
        self.entries: dict[str, tk.Entry] = {}
        self.stat_values: dict[str, tk.Label] = {}
        self.strategy_btns: dict[str, tk.Button] = {}
        self._log_offset = 0
        self._mt5_poll_tick = 0
        self._mt5_checking = False
        self._log_win = None
        self._log_text_popup = None
        self.log_text = None  # Live Activity lives in popup only

        err = verify_panel_source()
        if err:
            messagebox.showerror("Wrong / old panel", err)
            self.destroy()
            return

        try:
            self.cfg = load_effective_config()
        except Exception as exc:
            messagebox.showerror("Error", f"Could not read config.py:\n{exc}")
            self.destroy()
            return

        self._build_ui()
        self._load_update_label()
        self._poll()
        if not is_checklist_done(BASE_DIR):
            self.after(400, self._show_install_checklist)
        self.after(300, self._maybe_auto_start)
        # One-time notice if a stale EXE still exists on Desktop / folder
        self.after(600, self._warn_if_stale_exe)

    # ------------------------------------------------------------------ UI

    def _card(self, parent, title: str) -> tk.Frame:
        outer = tk.Frame(parent, bg=EDGE, padx=1, pady=1)
        inner = tk.Frame(outer, bg=CARD, padx=12, pady=10)
        inner.pack(fill="both", expand=True)
        tk.Label(inner, text=title.upper(), bg=CARD, fg=GOLD,
                 font=(FONT, 9, "bold")).pack(anchor="w", pady=(0, 6))
        outer.inner = inner
        return outer

    def _strategy_button_text(self, label: str, on: bool) -> str:
        return f"{label}\n{'●  ON' if on else '○  OFF'}"

    def _header_btn(self, parent, text, color, command):
        btn = tk.Button(
            parent, text=text, command=command, bg=CARD, fg=color,
            activebackground=EDGE, activeforeground=color,
            font=(FONT, 9, "bold"), relief="flat", padx=10, pady=4,
            cursor="hand2", bd=0, highlightthickness=1, highlightbackground=EDGE)
        hover(btn, CARD, "#182029")
        return btn

    def _build_ui(self):
        from strategies_catalog import STRATEGIES

        # ---------- header — controls always visible ----------
        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(header, text="GOLD GENIOUS", bg=BG, fg=GOLD,
                 font=(FONT, 14, "bold")).pack(side="left")
        self.version_lbl = tk.Label(header, text=f"{APP_VERSION}/{UI_BUILD}",
                                    bg=CARD, fg=GOLD,
                                    font=(FONT, 9, "bold"), padx=7, pady=2)
        self.version_lbl.pack(side="right")
        self.status_pill = tk.Label(header, text="● OFF", bg=CARD, fg=MUT,
                                    font=(FONT, 8, "bold"), padx=7, pady=2)
        self.status_pill.pack(side="right", padx=(0, 5))
        self.mt5_pill = tk.Label(header, text="● MT5 …", bg=CARD, fg=MUT,
                                 font=(FONT, 8, "bold"), padx=7, pady=2)
        self.mt5_pill.pack(side="right", padx=(0, 5))

        # Always-visible control buttons
        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=12, pady=(0, 6))
        self._header_btn(bar, "⚙  SETTINGS", GOLD, self._open_settings).pack(
            side="left")
        self._header_btn(bar, "ACCOUNT", GREEN, self._open_account).pack(
            side="left", padx=(6, 0))
        self._header_btn(bar, "LIVE ACTIVITY", BLUE, self._open_activity).pack(
            side="left", padx=(6, 0))
        self.update_btn = self._header_btn(bar, "⟳ UPDATE", BLUE, self.start_update)
        self.update_btn.pack(side="right")

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=12, pady=2)

        # ---------- Account (top — never hidden) ----------
        acct = self._card(body, "MT5 Account")
        acct.pack(fill="x")
        for key, label, ftype, tip in ACCOUNT_FIELDS:
            row = tk.Frame(acct.inner, bg=CARD)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=label, bg=CARD, fg=MUT, width=9, anchor="w",
                     font=(FONT, 8)).pack(side="left")
            entry = tk.Entry(row, bg=FIELD, fg=FG, insertbackground=GOLD,
                             relief="flat",
                             show="•" if ftype == "password" else "",
                             font=(MONO, 9), highlightthickness=1,
                             highlightbackground=EDGE, highlightcolor=GOLD)
            entry.pack(side="left", fill="x", expand=True, ipady=2)
            value = self.cfg.get(key)
            entry.insert(0, "" if value is None else str(value))
            self.entries[key] = entry
        save = tk.Button(acct.inner, text="SAVE ACCOUNT", command=self.save_settings,
                         bg=GOLD, fg="#0a0d12", font=(FONT, 9, "bold"),
                         relief="flat", pady=6, cursor="hand2", bd=0)
        save.pack(fill="x", pady=(6, 0))
        hover(save, GOLD, "#e8b64c")

        # ---------- strategy toggles (compact — no expand so Account stays visible) ----------
        strat = self._card(body, "1. Select strategy — toggle ON / OFF")
        strat.pack(fill="x", pady=(8, 0))
        tk.Label(strat.inner,
                 text="Only ONE can be ON. Tap again to turn OFF. Then press START.",
                 bg=CARD, fg=MUT, font=(FONT, 8)).pack(anchor="w", pady=(0, 6))
        grid = tk.Frame(strat.inner, bg=CARD)
        grid.pack(fill="x")
        active = set(self.cfg.get("active_strategies") or [])
        for idx, (sid, label, implemented) in enumerate(STRATEGIES):
            r, c = divmod(idx, 2)
            on = sid in active
            btn = tk.Button(
                grid,
                text=self._strategy_button_text(label, on),
                command=lambda s=sid: self._toggle_strategy(s),
                bg="#163528" if on else "#151b22",
                fg=GREEN if on else (FG if implemented else "#5a6570"),
                activebackground="#1f4a38" if on else EDGE,
                activeforeground=GREEN if on else FG,
                font=(FONT, 9, "bold"),
                relief="flat", pady=8, cursor="hand2", bd=0,
                highlightthickness=2,
                highlightbackground=GREEN if on else EDGE,
                highlightcolor=GREEN if on else EDGE,
                justify="center",
            )
            btn.grid(row=r, column=c, sticky="ew", padx=3, pady=3)
            grid.columnconfigure(c, weight=1)
            self.strategy_btns[sid] = btn

        # ---------- BIG Start / Stop ----------
        go = self._card(body, "2. Start / Stop bot")
        go.pack(fill="x", pady=(8, 0))
        tk.Label(go.inner,
                 text="START opens MetaTrader 5 automatically, then runs the ON strategy.",
                 bg=CARD, fg=MUT, font=(FONT, 8)).pack(anchor="w", pady=(0, 6))
        go_row = tk.Frame(go.inner, bg=CARD)
        go_row.pack(fill="x")
        self.start_btn = tk.Button(
            go_row, text="▶  START", command=self.start_bot,
            bg="#163528", fg=GREEN, activebackground="#1f4a38",
            activeforeground=GREEN, font=(FONT, 16, "bold"),
            relief="flat", pady=14, cursor="hand2", bd=0,
            highlightthickness=2, highlightbackground=GREEN,
            disabledforeground="#3d4a58")
        self.start_btn.pack(side="left", fill="both", expand=True, padx=(0, 4))
        hover(self.start_btn, "#163528", "#1f4a38")
        self.stop_btn = tk.Button(
            go_row, text="■  STOP", command=self.stop_bot,
            bg="#3a1a1a", fg=RED, activebackground="#4a2222",
            activeforeground=RED, font=(FONT, 16, "bold"),
            relief="flat", pady=14, cursor="hand2", bd=0,
            highlightthickness=2, highlightbackground=RED,
            disabledforeground="#3d4a58", state="disabled")
        self.stop_btn.pack(side="left", fill="both", expand=True, padx=(4, 0))
        hover(self.stop_btn, "#3a1a1a", "#4a2222")
        self.live_lbl = tk.Label(go.inner, text="● OFFLINE", bg=CARD, fg=MUT,
                                 font=(FONT, 11, "bold"))
        self.live_lbl.pack(anchor="w", pady=(8, 0))

        # ---------- live strip ----------
        mid = tk.Frame(body, bg=BG)
        mid.pack(fill="x", pady=(8, 0))
        stats = self._card(mid, "Live")
        stats.pack(fill="x")
        strip = tk.Frame(stats.inner, bg=CARD)
        strip.pack(fill="x")
        for key, label in [("equity", "Equity"), ("day_pl", "Day %"),
                           ("trades", "Trades"), ("mode", "Mode")]:
            cell = tk.Frame(strip, bg=CARD)
            cell.pack(side="left", expand=True, fill="x")
            tk.Label(cell, text=label, bg=CARD, fg=MUT,
                     font=(FONT, 7)).pack()
            val = tk.Label(cell, text="—", bg=CARD, fg=FG,
                           font=(MONO, 9, "bold"))
            val.pack()
            self.stat_values[key] = val
        self.stat_values["balance"] = self.stat_values["equity"]

        footer = tk.Frame(self, bg=BG)
        footer.pack(fill="x", padx=12, pady=(0, 8))
        self.update_lbl = tk.Label(footer, text="", bg=BG, fg=MUT, font=(FONT, 8))
        self.update_lbl.pack(side="left")
        tk.Label(footer,
                 text="SETTINGS = bot rules  ·  ACCOUNT = MT5 login",
                 bg=BG, fg=MUT, font=(FONT, 8)).pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _open_account(self):
        """Dedicated MT5 account window (Login / Password / Server / Symbol)."""
        if getattr(self, "_acct_win", None) is not None:
            try:
                if self._acct_win.winfo_exists():
                    self._acct_win.lift()
                    return
            except tk.TclError:
                pass

        win = tk.Toplevel(self)
        win.title("MT5 Account Settings")
        win.configure(bg=BG)
        win.geometry("420x340")
        win.minsize(380, 300)
        self._acct_win = win

        tk.Label(win, text="MT5 ACCOUNT", bg=BG, fg=GOLD,
                 font=(FONT, 14, "bold")).pack(anchor="w", padx=16, pady=(14, 4))
        tk.Label(win, text="Saved on this computer only. Used when you press START.",
                 bg=BG, fg=MUT, font=(FONT, 8)).pack(anchor="w", padx=16, pady=(0, 10))

        box = tk.Frame(win, bg=CARD, padx=14, pady=12)
        box.pack(fill="both", expand=True, padx=16, pady=(0, 8))
        popup_entries: dict[str, tk.Entry] = {}
        for key, label, ftype, _tip in ACCOUNT_FIELDS:
            row = tk.Frame(box, bg=CARD)
            row.pack(fill="x", pady=4)
            tk.Label(row, text=label, bg=CARD, fg=MUT, width=10, anchor="w",
                     font=(FONT, 9)).pack(side="left")
            entry = tk.Entry(row, bg=FIELD, fg=FG, insertbackground=GOLD,
                             relief="flat",
                             show="•" if ftype == "password" else "",
                             font=(MONO, 10), highlightthickness=1,
                             highlightbackground=EDGE, highlightcolor=GOLD)
            entry.pack(side="left", fill="x", expand=True, ipady=4)
            src = self.entries.get(key)
            if src is not None:
                entry.insert(0, src.get())
            else:
                value = self.cfg.get(key)
                entry.insert(0, "" if value is None else str(value))
            popup_entries[key] = entry

        def save_and_close():
            for key, label, ftype, _tip in ACCOUNT_FIELDS:
                raw = popup_entries[key].get()
                try:
                    self._parse_value(key, ftype, raw)
                except (ValueError, IndexError):
                    messagebox.showerror(
                        "Invalid value",
                        f"'{label}' has an invalid value:\n\n  {raw!r}",
                        parent=win)
                    return
            for key, _, _, _ in ACCOUNT_FIELDS:
                if key in self.entries:
                    self.entries[key].delete(0, "end")
                    self.entries[key].insert(0, popup_entries[key].get())
            self.save_settings()
            win.destroy()

        tk.Button(box, text="SAVE ACCOUNT", command=save_and_close,
                  bg=GOLD, fg="#0a0d12", font=(FONT, 11, "bold"),
                  relief="flat", pady=10, cursor="hand2", bd=0).pack(
                      fill="x", pady=(12, 0))

        def _on_close():
            self._acct_win = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", _on_close)

    def _open_activity(self):
        """Separate Live Activity window with CLEAR."""
        if self._log_win is not None and self._log_win.winfo_exists():
            self._log_win.lift()
            return
        win = tk.Toplevel(self)
        win.title("Live Activity")
        win.configure(bg=BG)
        win.geometry("640x420")
        win.minsize(420, 280)
        top = tk.Frame(win, bg=BG)
        top.pack(fill="x", padx=12, pady=8)
        tk.Label(top, text="LIVE ACTIVITY", bg=BG, fg=GOLD,
                 font=(FONT, 11, "bold")).pack(side="left")
        clear_btn = tk.Button(top, text="CLEAR", command=self.clear_log,
                              bg=CARD, fg=MUT, font=(FONT, 9, "bold"),
                              relief="flat", padx=12, pady=4, cursor="hand2", bd=0)
        clear_btn.pack(side="right")
        hover(clear_btn, CARD, "#182029")
        text = tk.Text(win, bg=FIELD, fg="#93a8bd", relief="flat",
                       font=(MONO, 9), wrap="word", highlightthickness=0,
                       padx=10, pady=8)
        text.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        text.insert("1.0", "Waiting for activity…")
        text.config(state="disabled")
        self._log_win = win
        self._log_text_popup = text
        self.log_text = text

        def _on_close():
            self._log_win = None
            self._log_text_popup = None
            self.log_text = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", _on_close)

    def _enabled_strategies(self) -> list[str]:
        """At most one strategy id (exclusive toggle)."""
        data = self._load_settings_json()
        active = data.get("active_strategies")
        if active is None:
            active = self.cfg.get("active_strategies") or ["10_PIPS"]
        if isinstance(active, str):
            active = [active]
        active = [s for s in active if s]
        # Enforce single selection
        return active[:1]

    def _refresh_strategy_buttons(self):
        from strategies_catalog import STRATEGIES, is_implemented
        active = set(self._enabled_strategies())
        for sid, label, implemented in STRATEGIES:
            btn = self.strategy_btns.get(sid)
            if not btn:
                continue
            on = sid in active
            btn.config(
                text=self._strategy_button_text(label, on),
                bg="#163528" if on else "#151b22",
                fg=GREEN if on else (FG if implemented else "#5a6570"),
                activebackground="#1f4a38" if on else EDGE,
                highlightbackground=GREEN if on else EDGE,
                highlightcolor=GREEN if on else EDGE,
            )

    def _toggle_strategy(self, sid: str):
        """Exclusive ON/OFF toggle — selecting one turns all others OFF."""
        from strategies_catalog import is_implemented, label_for
        active = self._enabled_strategies()

        # Tap the already-ON strategy → turn OFF
        if active and active[0] == sid:
            self._apply_strategies([])
            self._refresh_strategy_buttons()
            if self.bot_process and self.bot_process.poll() is None:
                self.stop_bot()
            return

        if not is_implemented(sid):
            messagebox.showinfo(
                "Coming soon",
                f"{label_for(sid)} is not live yet.\n\n"
                "Only 10 PIPS works for now. Select 10 PIPS, then press START.")
            return

        # Exclusive: only this strategy ON
        was_running = self.bot_process and self.bot_process.poll() is None
        if was_running:
            self.stop_bot()
        self._apply_strategies([sid])
        self._refresh_strategy_buttons()
        if was_running:
            messagebox.showinfo(
                "Strategy selected",
                f"{label_for(sid)} is ON (others OFF).\n\n"
                "Bot was stopped — press START to open MT5 and run this strategy.")

    def _apply_strategies(self, active: list[str], restart: bool = False):
        # Always store at most one strategy
        active = list(active)[:1]
        data = self._load_settings_json()
        data["active_strategies"] = active
        data["entry_mode"] = active[0] if active else ""
        data["user_tuned"] = True
        data["tuned_version"] = APP_VERSION
        self._save_settings_json(data)
        self.cfg = load_effective_config()

    def _flat_broker(self):
        """Close every bot position + pending on the connected MT5."""
        try:
            from mt5_client import MT5Client
            from trade_manager import TradeManager
            client = MT5Client(self.cfg)
            if not client.connect():
                return False
            trader = TradeManager(self.cfg, client)
            trader.cancel_pending("strategy switch")
            trader.close_all("strategy switch")
            try:
                import ten_pips
                ten_pips.clear_state(self.cfg)
            except Exception:
                pass
            client.shutdown()
            return True
        except Exception:
            return False

    def _switch_strategy_worker(self, active: list[str]):
        try:
            self.after(0, lambda: self.live_lbl.config(text="●  SWITCHING…", fg=GOLD))
            if self.bot_process and self.bot_process.poll() is None:
                self.bot_process.terminate()
                self.bot_process = None
            self._kill_orphan_bots()
            self._flat_broker()
            self._apply_strategies(active)
            close_mt5()
            if not launch_mt5(self.cfg):
                self.after(0, lambda: messagebox.showerror(
                    "MT5", "Could not restart MetaTrader 5."))
                self.after(0, self._reset_start_btn)
                return
            wait_for_mt5_api(self.cfg, timeout=60)
            self.after(0, self._refresh_strategy_buttons)
            self.after(0, self.start_bot)
        except Exception as exc:
            self.after(0, lambda: messagebox.showerror(
                "Strategy switch", f"Failed:\n{exc}"))
            self.after(0, self._reset_start_btn)

    def _load_settings_json(self) -> dict:
        if os.path.isfile(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _save_settings_json(self, data: dict):
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        self.cfg = load_effective_config()

    # ---------------------------------------------------------- parsing

    def _parse_value(self, key: str, ftype, raw: str):
        raw = raw.strip()
        if ftype == str:
            if not raw:
                raise ValueError("cannot be empty")
            return raw
        if ftype == int:
            return int(raw)
        if ftype == float:
            return float(raw)
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
            for w in windows:  # validate format HH:MM-HH:MM
                a, b = w.split("-")
                for t in (a, b):
                    h, m = t.split(":")
                    if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
                        raise ValueError(f"bad time in '{w}'")
            return windows
        if ftype == "opt_int":
            return int(raw) if raw else None
        # opt_str / password
        return raw or None

    # ------------------------------------------------------------- actions

    def save_settings(self):
        overrides = self._load_settings_json()
        for key, label, ftype, _tip in ACCOUNT_FIELDS:
            raw = self.entries[key].get()
            try:
                overrides[key] = self._parse_value(key, ftype, raw)
            except (ValueError, IndexError):
                messagebox.showerror("Invalid value",
                                     f"'{label}' has an invalid value:\n\n  {raw!r}")
                return

        self._save_settings_json(overrides)

        if self.bot_process and self.bot_process.poll() is None:
            messagebox.showinfo("Saved", "Account saved.\n\nThe bot is running — "
                                "restart it (Stop, then Start) to apply.")
        else:
            messagebox.showinfo("Saved", "Account saved. Press START BOT to begin.")

    def start_bot(self):
        from strategies_catalog import is_implemented, label_for
        self.cfg = load_effective_config()
        active = self._enabled_strategies()
        if not active or not any(is_implemented(s) for s in active):
            messagebox.showwarning(
                "Select a strategy",
                "Toggle ONE strategy ON first (10 PIPS), then press START.\n\n"
                "START will open MetaTrader 5 and run that strategy.")
            return
        if self.bot_process and self.bot_process.poll() is None:
            return
        main_py = os.path.join(BASE_DIR, "main.py")
        if not os.path.exists(main_py):
            messagebox.showerror("Error", f"main.py not found in:\n{BASE_DIR}")
            return
        self.start_btn.config(state="disabled")
        self.live_lbl.config(
            text=f"● STARTING {label_for(active[0])}…", fg=GOLD)
        threading.Thread(target=self._start_worker, daemon=True).start()

    @staticmethod
    def _kill_orphan_bots():
        """Kill any leftover main.py processes from earlier sessions.
        Two bots attached to one terminal cause endless 'IPC send failed'."""
        cmd = ("Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
               "Where-Object { $_.CommandLine -match 'main\\.py' } | "
               "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                           capture_output=True, timeout=30,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception:
            pass

    def _start_worker(self):
        main_py = os.path.join(BASE_DIR, "main.py")
        try:
            self.cfg = load_effective_config()
            self._kill_orphan_bots()
            self.live_lbl.after(0, lambda: self.live_lbl.config(
                text="● OPENING MT5…", fg=GOLD))
            if not launch_mt5(self.cfg):
                self.after(0, lambda: messagebox.showerror(
                    "MetaTrader 5",
                    "Could not open MetaTrader 5.\n\nInstall MT5 and try again, "
                    "or set mt5_terminal_path in settings."))
                self.after(0, self._reset_start_btn)
                return
            if not wait_for_mt5_api(self.cfg, timeout=60):
                self.after(0, lambda: messagebox.showwarning(
                    "MetaTrader 5",
                    "MT5 opened but the bot could not connect yet.\n\n"
                    "Log in to MT5, enable Algo Trading, then press START again."))
                self.after(0, self._reset_start_btn)
                return
            self.bot_process = subprocess.Popen(
                [find_python(), main_py],
                cwd=BASE_DIR,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            self.after(0, self._on_bot_started)
        except OSError as exc:
            self.after(0, lambda: messagebox.showerror("Error", f"Could not start the bot:\n{exc}"))
            self.after(0, self._reset_start_btn)

    def _on_bot_started(self):
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status_pill.config(text="● LIVE", fg=GREEN)
        self.live_lbl.config(text="●  LIVE", fg=GREEN)

    def _reset_start_btn(self):
        self.start_btn.config(state="normal")
        self.live_lbl.config(text="●  OFFLINE", fg=MUT)

    def clear_log(self):
        """Hide everything logged so far — only new activity shows from here."""
        try:
            self._log_offset = os.path.getsize(LOG_FILE)
        except OSError:
            self._log_offset = 0
        widget = self.log_text or self._log_text_popup
        if widget is None:
            return
        widget.config(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", "Log cleared — waiting for new activity…")
        widget.config(state="disabled")

    def stop_bot(self):
        if self.bot_process and self.bot_process.poll() is None:
            self.bot_process.terminate()
        self.bot_process = None
        threading.Thread(target=self._kill_orphan_bots, daemon=True).start()
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.status_pill.config(text="● OFF", fg=MUT)
        self.live_lbl.config(text="●  OFFLINE", fg=MUT)

    # ------------------------------------------------------------- updater

    def start_update(self):
        if self.bot_process and self.bot_process.poll() is None:
            if not messagebox.askyesno(
                    "Bot is running",
                    "The bot must be stopped to update.\n\nStop it and update now?\n"
                    "(Open trades keep their SL/TP on the broker side.)"):
                return
            self.stop_bot()
        self.update_btn.config(state="disabled", text="⟳ …")
        threading.Thread(target=self._update_worker, daemon=True).start()

    def _update_worker(self):
        try:
            message = self._do_update()
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(UPDATE_META_FILE, "w", encoding="utf-8") as f:
                json.dump({"last_update": stamp}, f, indent=2)
            close_mt5()
            launch_mt5(self.cfg)
            wait_for_mt5_api(self.cfg, timeout=60)
            success = True
            message = f"{message}\n\nLast updated: {stamp}\n\nMT5 restarted. Bot will start now."
        except urllib.error.HTTPError as exc:
            message = (f"GitHub download failed (HTTP {exc.code}).\n\n"
                       "If the repository is private, either make it public or "
                       "install Git on this machine and sign in to GitHub once "
                       "— the updater will then use Git automatically.")
            success = False
            stamp = None
        except Exception as exc:
            message = f"Update failed:\n{exc}"
            success = False
            stamp = None
        self.after(0, lambda: self._update_done(success, message, stamp))

    def _do_update(self) -> str:
        # FULL A-Z update: download the complete repository zip and replace
        # EVERY file, so grand changes (new modules, renames) always arrive.
        try:
            copied = self._zip_full_update()
            self._pip_install()
            return (f"Full update from GitHub — every file replaced "
                    f"({copied} files).\n\nYour account settings and state "
                    "were kept untouched.")
        except urllib.error.HTTPError:
            # Private repo without public zip — fall back to git if available.
            if os.path.isdir(os.path.join(BASE_DIR, ".git")) and shutil.which("git"):
                result = subprocess.run(
                    ["git", "-C", BASE_DIR, "fetch", "--all"],
                    capture_output=True, text=True, timeout=180,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                result = subprocess.run(
                    ["git", "-C", BASE_DIR, "reset", "--hard", "origin/main"],
                    capture_output=True, text=True, timeout=180,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                if result.returncode == 0:
                    self._pip_install()
                    return ("Full update via Git (reset to origin/main):\n\n"
                            + result.stdout.strip()[-400:])
            raise

    def _zip_full_update(self) -> int:
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = os.path.join(tmp, "repo.zip")
            urllib.request.urlretrieve(GITHUB_ZIP, zip_path)
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(tmp)
            repo_root = next(e.path for e in os.scandir(tmp) if e.is_dir())

            copied = 0
            for dirpath, _dirnames, filenames in os.walk(repo_root):
                rel = os.path.relpath(dirpath, repo_root)
                for name in filenames:
                    if name in UPDATE_PROTECTED:
                        continue
                    dest_dir = BASE_DIR if rel == "." else os.path.join(BASE_DIR, rel)
                    os.makedirs(dest_dir, exist_ok=True)
                    shutil.copy2(os.path.join(dirpath, name),
                                 os.path.join(dest_dir, name))
                    copied += 1
            # Stale frozen UIs must not survive an update
            delete_stale_panel_exes(BASE_DIR)
            # Hard check — new strategy UI must be on disk
            panel_path = os.path.join(BASE_DIR, "control_panel.py")
            with open(panel_path, "r", encoding="utf-8", errors="replace") as f:
                body = f.read()
            if "Famous Strategies" not in body:
                raise RuntimeError(
                    "Update downloaded files but control_panel.py is still old. "
                    "Close every Gold Genious window and run SETUP.bat.")
        return copied

    def _pip_install(self):
        """Keep dependencies in sync with the updated requirements.txt."""
        req = os.path.join(BASE_DIR, "requirements.txt")
        if os.path.exists(req):
            subprocess.run(
                [find_python(), "-m", "pip", "install", "-r", req, "--quiet"],
                capture_output=True, timeout=600,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )

    def _reload_version_label(self):
        try:
            import importlib
            import version as ver_mod
            importlib.reload(ver_mod)
            self.version_lbl.config(text=f"{ver_mod.VERSION}/{UI_BUILD}")
            return ver_mod.VERSION
        except Exception:
            return APP_VERSION

    def _warn_if_stale_exe(self):
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        found = []
        for folder in (BASE_DIR, desktop):
            for name in STALE_EXE_NAMES:
                path = os.path.join(folder, name)
                if os.path.isfile(path):
                    found.append(path)
        if not found:
            return
        # Auto-remove from bot folder; warn about Desktop copies
        delete_stale_panel_exes(BASE_DIR)
        left = [p for p in found if os.path.isfile(p)]
        if left:
            messagebox.showwarning(
                "Old EXE found",
                "An old Gold Genious.exe is still on this PC.\n\n"
                "That EXE freezes the OLD look and ignores Update.\n\n"
                "Delete these files, then open the Desktop shortcut "
                "(or run_panel.bat):\n\n" + "\n".join(left))

    def _update_done(self, success: bool, message: str, stamp: str | None = None):
        self.update_btn.config(state="normal", text="⟳ UPDATE")
        if stamp:
            self._load_update_label()
        if success:
            ver = self._reload_version_label()
            if self.bot_process and self.bot_process.poll() is None:
                self.stop_bot()
            kill_stale_panel_exes()
            removed = delete_stale_panel_exes(BASE_DIR)
            extra = ""
            if removed:
                extra = ("\n\nRemoved old frozen EXE(s): "
                         + ", ".join(removed)
                         + "\n(Use Desktop shortcut / run_panel.bat from now on.)")
            flag = os.path.join(BASE_DIR, "panel_auto_start.json")
            try:
                with open(flag, "w", encoding="utf-8") as f:
                    json.dump({"start": True, "version": ver}, f)
            except OSError:
                pass
            # Verify new UI file actually landed
            check = verify_panel_source()
            if check:
                messagebox.showerror("Update incomplete", check)
                return
            messagebox.showinfo(
                "Update complete",
                f"Now running {ver} / UI {UI_BUILD}\n\n{message}{extra}\n\n"
                "Panel will reopen with STRATEGY buttons.")
            self._restart_panel()
        else:
            messagebox.showerror("Update", message)

    def _restart_panel(self):
        """Relaunch via Python so the new Control Panel UI always loads."""
        panel = os.path.join(BASE_DIR, "control_panel.py")
        kill_stale_panel_exes()
        try:
            subprocess.Popen([find_pythonw(), panel], cwd=BASE_DIR)
        except OSError as exc:
            messagebox.showerror(
                "Restart",
                f"Updated files are on disk, but the panel could not reopen:\n{exc}\n\n"
                "Close this window and run run_panel.bat (not the .exe).")
            return
        self.destroy()

    def _maybe_auto_start(self):
        """After Update restart, auto-start the bot once."""
        flag = os.path.join(BASE_DIR, "panel_auto_start.json")
        if not os.path.isfile(flag):
            return
        try:
            with open(flag, "r", encoding="utf-8") as f:
                data = json.load(f)
            os.remove(flag)
        except (OSError, json.JSONDecodeError):
            return
        if data.get("start"):
            self.after(800, self.start_bot)

    def _load_update_label(self):
        try:
            with open(UPDATE_META_FILE, "r", encoding="utf-8") as f:
                stamp = json.load(f).get("last_update")
            self.update_lbl.config(text=f"Last updated: {stamp}" if stamp else "")
        except (OSError, json.JSONDecodeError):
            self.update_lbl.config(text="")

    def _open_settings(self):
        self.cfg = load_effective_config()
        win = tk.Toplevel(self)
        win.title("Bot Settings — risk, hours, rules")
        win.configure(bg=BG)
        win.geometry("640x580")
        win.minsize(520, 480)
        win.grab_set()

        top = tk.Frame(win, bg=BG)
        top.pack(fill="x", padx=18, pady=(14, 2))
        tk.Label(top, text="BOT SETTINGS", bg=BG, fg=GOLD,
                 font=(FONT, 14, "bold")).pack(side="left")
        tk.Button(
            top, text="MT5 ACCOUNT",
            command=lambda: (win.grab_release(), win.destroy(), self._open_account()),
            bg=CARD, fg=GREEN, font=(FONT, 9, "bold"), relief="flat",
            padx=10, pady=3, cursor="hand2", bd=0,
        ).pack(side="right")
        tk.Label(win,
                 text="Risk, trading hours, and rules. MT5 login → ACCOUNT button.",
                 bg=BG, fg=MUT, font=(FONT, 9)).pack(anchor="w", padx=18, pady=(0, 8))

        style = ttk.Style(win)
        style.theme_use("clam")
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=CARD, foreground=FG, padding=(10, 5))
        style.map("TNotebook.Tab", background=[("selected", GOLD)],
                  foreground=[("selected", "#000000")])

        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=14, pady=4)

        entries: dict[str, tk.Entry] = {}
        for section, fields in SETTINGS_SECTIONS:
            tab = tk.Frame(nb, bg=CARD, padx=12, pady=8)
            nb.add(tab, text=section)
            canvas = tk.Canvas(tab, bg=CARD, highlightthickness=0)
            scroll = tk.Frame(canvas, bg=CARD)
            canvas.create_window((0, 0), window=scroll, anchor="nw")
            canvas.pack(side="left", fill="both", expand=True)
            sb = tk.Scrollbar(tab, orient="vertical", command=canvas.yview)
            sb.pack(side="right", fill="y")
            canvas.configure(yscrollcommand=sb.set)
            scroll.bind("<Configure>", lambda e, c=canvas: c.configure(scrollregion=c.bbox("all")))

            for row, (key, label, ftype, hint) in enumerate(fields):
                tk.Label(scroll, text=label, bg=CARD, fg=FG, anchor="w",
                         font=(FONT, 10)).grid(row=row * 2, column=0, sticky="w", pady=(6, 0))
                show = "*" if ftype == "password" else ""
                entry = tk.Entry(scroll, width=28, bg=FIELD, fg=FG,
                                 insertbackground=GOLD, relief="flat", show=show,
                                 font=(MONO, 10), highlightthickness=1,
                                 highlightbackground=EDGE, highlightcolor=GOLD)
                entry.grid(row=row * 2, column=1, padx=(10, 0), pady=(6, 0))
                val = self.cfg.get(key)
                entry.insert(0, format_setting(key, val, ftype))
                entries[key] = (entry, ftype, label)
                tk.Label(scroll, text=hint, bg=CARD, fg=MUT, font=(FONT, 8),
                         anchor="w").grid(row=row * 2 + 1, column=0, columnspan=2, sticky="w")

        btn_row = tk.Frame(win, bg=BG)
        btn_row.pack(fill="x", padx=18, pady=12)

        def save_bot_settings():
            data = self._load_settings_json()
            for key, (entry, ftype, label) in entries.items():
                raw = entry.get()
                try:
                    data[key] = parse_setting(key, raw, ftype)
                except (ValueError, IndexError) as exc:
                    messagebox.showerror("Invalid value", f"{label}:\n{exc}", parent=win)
                    return
            # Mark that the user explicitly tuned strategy values ON THIS
            # VERSION. config.py only honours them while the version matches;
            # after an update the new defaults win again.
            data["user_tuned"] = True
            data["tuned_version"] = APP_VERSION
            self._save_settings_json(data)
            messagebox.showinfo("Saved", "Bot settings saved.\n\nRestart the bot to apply.",
                                parent=win)

        tk.Button(btn_row, text="SAVE SETTINGS", command=save_bot_settings,
                  bg=GOLD, fg="#0a0d12", relief="flat", padx=20, pady=8,
                  font=(FONT, 10, "bold"), cursor="hand2").pack(side="right")
        tk.Button(btn_row, text="CLOSE", command=win.destroy, bg=CARD, fg=FG,
                  relief="flat", padx=16, pady=8, cursor="hand2").pack(side="right", padx=(0, 8))

    def _show_install_checklist(self):
        if is_checklist_done(BASE_DIR):
            return
        results = run_checks(BASE_DIR, self.cfg)
        win = tk.Toplevel(self)
        win.title("Setup checklist")
        win.configure(bg=BG)
        win.resizable(False, False)
        win.grab_set()
        tk.Label(win, text="SETUP CHECKLIST", bg=BG, fg=GOLD,
                 font=(FONT, 14, "bold")).pack(anchor="w", padx=20, pady=(16, 4))
        tk.Label(win, text="Install everything below. This popup won't show again once complete.",
                 bg=BG, fg=MUT, font=(FONT, 9)).pack(anchor="w", padx=20, pady=(0, 12))

        body = tk.Frame(win, bg=BG)
        body.pack(fill="both", expand=True, padx=20)

        for item in results:
            row = tk.Frame(body, bg=CARD, padx=12, pady=10)
            row.pack(fill="x", pady=4)
            mark = "✓" if item["ok"] else "✗"
            color = GREEN if item["ok"] else RED
            tk.Label(row, text=mark, bg=CARD, fg=color,
                     font=(FONT, 12, "bold"), width=2).pack(side="left")
            col = tk.Frame(row, bg=CARD)
            col.pack(side="left", fill="x", expand=True)
            tk.Label(col, text=item["label"], bg=CARD, fg=FG,
                     font=(FONT, 10, "bold"), anchor="w").pack(fill="x")
            tk.Label(col, text=item["detail"], bg=CARD, fg=MUT,
                     font=(FONT, 8), anchor="w").pack(fill="x")
            if item.get("hint"):
                tk.Label(col, text=item["hint"], bg=CARD, fg=MUT,
                         font=(FONT, 8), anchor="w").pack(fill="x")
            if item.get("download"):
                link = tk.Label(col, text="Download →", bg=CARD, fg=BLUE,
                                cursor="hand2", font=(FONT, 8, "underline"))
                link.pack(anchor="w", pady=(2, 0))
                url = item["download"]
                link.bind("<Button-1>", lambda _e, u=url: webbrowser.open(u))

        btn_row = tk.Frame(win, bg=BG)
        btn_row.pack(fill="x", padx=20, pady=16)

        def recheck():
            win.destroy()
            self._show_install_checklist()

        def continue_ok():
            fresh = run_checks(BASE_DIR, self.cfg)
            if not all_passed(fresh):
                messagebox.showwarning(
                    "Not ready",
                    "Some items are still missing.\n\nInstall them, then press Re-check.")
                return
            mark_checklist_done(BASE_DIR)
            win.destroy()
            messagebox.showinfo("Ready", "Setup complete. This checklist won't appear again.")

        tk.Button(btn_row, text="RE-CHECK", command=recheck, bg=CARD, fg=FG,
                  relief="flat", padx=16, pady=8, cursor="hand2").pack(side="left")
        tk.Button(btn_row, text="CONTINUE", command=continue_ok, bg=GOLD, fg="#0a0d12",
                  relief="flat", padx=16, pady=8, cursor="hand2",
                  font=(FONT, 10, "bold")).pack(side="right")

    def on_close(self):
        if self.bot_process and self.bot_process.poll() is None:
            if messagebox.askyesno(
                    "Bot is running",
                    "The bot is still running.\n\nStop the bot and close?\n"
                    "(Open trades keep their SL/TP on the broker side.)"):
                self.stop_bot()
            else:
                return
        self.destroy()

    # ------------------------------------------------------------- polling

    def _poll(self):
        # bot died on its own?
        if self.bot_process and self.bot_process.poll() is not None:
            self.stop_bot()

        # MT5 terminal status — checked every 3 s in the background so the
        # UI never freezes on the process lookup.
        self._mt5_poll_tick += 1
        if self._mt5_poll_tick >= 3 and not self._mt5_checking:
            self._mt5_poll_tick = 0
            self._mt5_checking = True

            def check_mt5():
                bot_on = (self.bot_process is not None
                          and self.bot_process.poll() is None)
                label, colour = mt5_status(self.cfg, LOG_FILE, bot_on)
                colours = {"green": GREEN, "gold": GOLD, "red": RED}

                def apply():
                    self._mt5_checking = False
                    self.mt5_pill.config(text=label, fg=colours.get(colour, MUT))

                self.after(0, apply)

            threading.Thread(target=check_mt5, daemon=True).start()

        # live account card
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                st = json.load(f)
            equity = st.get("last_equity")
            balance = st.get("last_balance")
            day_start = st.get("day_start_equity") or 0
            self.stat_values["equity"].config(
                text=f"{equity:,.2f}" if equity else "—")
            self.stat_values["balance"].config(
                text=f"{balance:,.2f}" if balance else "—")
            if equity and day_start:
                pl = (equity - day_start) / day_start * 100
                color = GREEN if pl >= 0 else RED
                self.stat_values["day_pl"].config(text=f"{pl:+.2f} %", fg=color)
            else:
                self.stat_values["day_pl"].config(text="—", fg=FG)
            self.stat_values["trades"].config(text=str(st.get("trades_today", "—")))
            mode_text, mode_color = MODE_LABELS.get(
                st.get("mode", ""), (st.get("mode", "—"), FG))
            self.stat_values["mode"].config(text=mode_text, fg=mode_color)
        except (OSError, json.JSONDecodeError, TypeError):
            for val in self.stat_values.values():
                val.config(text="—", fg=FG)

        # log tail
        try:
            size = os.path.getsize(LOG_FILE)
            if size < self._log_offset:      # log rotated/truncated -> show all
                self._log_offset = 0
            with open(LOG_FILE, "rb") as f:
                f.seek(self._log_offset)
                raw = f.read()
            lines = raw.decode("utf-8", errors="replace").splitlines(True)[-200:]
            text = "".join(lines)
            if not text.strip():
                text = "Log cleared — waiting for new activity…"
        except OSError:
            text = "No activity yet — save your account and press START BOT."
        widget = self.log_text or self._log_text_popup
        if widget is not None:
            widget.config(state="normal")
            if widget.get("1.0", "end-1c") != text:
                widget.delete("1.0", "end")
                widget.insert("1.0", text)
                widget.see("end")
            widget.config(state="disabled")

        self.after(1000, self._poll)


if __name__ == "__main__":
    redirect_frozen_exe_to_python()
    app = ControlPanel()
    app.mainloop()
