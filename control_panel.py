"""
GOLD GENIOUS — XAUUSD Auto Trader, Control Panel.

Minimal by design: you enter your trading account (login / password / server),
everything else — strategy, risk, loss guards, trade management — is
auto-managed with the optimal settings and sized live from your equity.

Run directly:      python control_panel.py
Or use the built:  "Gold Genious.exe"
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

BASE_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
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
    "data_heartbeat.json",
}

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
        self.title("Gold Genious — XAUUSD Auto Trader")
        self.configure(bg=BG)
        self.minsize(1020, 620)
        self.bot_process: subprocess.Popen | None = None
        self.entries: dict[str, tk.Entry] = {}
        self.stat_values: dict[str, tk.Label] = {}
        self._log_offset = 0     # bytes of bot.log hidden by the CLEAR button
        self._mt5_poll_tick = 0  # MT5 process check every few seconds
        self._mt5_checking = False

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

    # ------------------------------------------------------------------ UI

    def _card(self, parent, title: str) -> tk.Frame:
        outer = tk.Frame(parent, bg=EDGE, padx=1, pady=1)
        inner = tk.Frame(outer, bg=CARD, padx=18, pady=14)
        inner.pack(fill="both", expand=True)
        tk.Label(inner, text=title.upper(), bg=CARD, fg=GOLD,
                 font=(FONT, 10, "bold")).pack(anchor="w", pady=(0, 10))
        outer.inner = inner
        return outer

    def _build_ui(self):
        # ---------- header ----------
        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", padx=26, pady=(20, 8))
        tk.Label(header, text="GOLD  GENIOUS", bg=BG, fg=GOLD,
                 font=(FONT, 20, "bold")).pack(side="left")
        tk.Label(header, text="   XAUUSD · M5 · fully auto-managed", bg=BG,
                 fg=MUT, font=(FONT, 11)).pack(side="left", pady=(6, 0))

        self.version_lbl = tk.Label(header, text=APP_VERSION, bg=CARD, fg=GOLD,
                                    font=(FONT, 10, "bold"), padx=10, pady=4)
        self.version_lbl.pack(side="right", padx=(0, 10))
        self.status_pill = tk.Label(header, text="  ●  BOT OFFLINE  ", bg=CARD, fg=MUT,
                                    font=(FONT, 10, "bold"), padx=10, pady=5)
        self.status_pill.pack(side="right")
        self.mt5_pill = tk.Label(header, text="  ●  MT5 …  ", bg=CARD, fg=MUT,
                                 font=(FONT, 10, "bold"), padx=10, pady=5)
        self.mt5_pill.pack(side="right", padx=(0, 8))
        settings_btn = tk.Button(header, text="⚙", command=self._open_settings,
                                 bg=CARD, fg=GOLD, activebackground=EDGE,
                                 font=(FONT, 14), relief="flat", padx=10, pady=2,
                                 cursor="hand2", bd=0)
        settings_btn.pack(side="right", padx=(0, 8))
        hover(settings_btn, CARD, "#182029")

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=26, pady=6)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        # ---------- left column ----------
        left = tk.Frame(body, bg=BG)
        left.grid(row=0, column=0, sticky="nsw", padx=(0, 18))

        acct = self._card(left, "Trading account")
        acct.pack(fill="x")
        for key, label, ftype, tip in ACCOUNT_FIELDS:
            tk.Label(acct.inner, text=label, bg=CARD, fg=FG,
                     font=(FONT, 10)).pack(anchor="w", pady=(8, 2))
            entry = tk.Entry(acct.inner, width=30, bg=FIELD, fg=FG,
                             insertbackground=GOLD, relief="flat",
                             show="•" if ftype == "password" else "",
                             font=(MONO, 11), highlightthickness=1,
                             highlightbackground=EDGE, highlightcolor=GOLD)
            entry.pack(fill="x", ipady=5)
            value = self.cfg.get(key)
            entry.insert(0, "" if value is None else str(value))
            self.entries[key] = entry
            tk.Label(acct.inner, text=tip, bg=CARD, fg=MUT,
                     font=(FONT, 8)).pack(anchor="w")

        save = tk.Button(acct.inner, text="SAVE ACCOUNT", command=self.save_settings,
                         bg=GOLD, fg="#0a0d12", activebackground=GOLD_SOFT,
                         font=(FONT, 10, "bold"), relief="flat", pady=9,
                         cursor="hand2", bd=0)
        save.pack(fill="x", pady=(16, 2))
        hover(save, GOLD, "#e8b64c")

        stats = self._card(left, "Live account")
        stats.pack(fill="x", pady=(14, 0))
        for key, label in [("equity", "Equity"), ("balance", "Balance"),
                           ("day_pl", "Today's P/L"), ("trades", "Trades today"),
                           ("mode", "Status")]:
            row = tk.Frame(stats.inner, bg=CARD)
            row.pack(fill="x", pady=3)
            tk.Label(row, text=label, bg=CARD, fg=MUT, width=13, anchor="w",
                     font=(FONT, 10)).pack(side="left")
            val = tk.Label(row, text="—", bg=CARD, fg=FG, anchor="e",
                           font=(MONO, 11, "bold"))
            val.pack(side="right")
            self.stat_values[key] = val

        note = tk.Label(left, justify="left", bg=BG, fg=MUT, font=(FONT, 8),
                        text="Press ⚙ in the header to view or change all bot\n"
                             "defaults (risk, guards, strategy, email alerts).")
        note.pack(anchor="w", pady=(12, 0))

        # ---------- right column ----------
        right = tk.Frame(body, bg=BG)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        controls = tk.Frame(right, bg=BG)
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 12))

        def control_button(text, color, command):
            btn = tk.Button(controls, text=text, command=command, bg=CARD,
                            fg=color, activebackground=EDGE, activeforeground=color,
                            font=(FONT, 16, "bold"), relief="flat", padx=18,
                            pady=6, cursor="hand2", bd=0,
                            highlightthickness=1, highlightbackground=EDGE,
                            disabledforeground="#3d4a58")
            hover(btn, CARD, "#182029")
            return btn

        # Icon-only controls: ▶ start · ■ stop · ⟳ update
        self.start_btn = control_button("▶", GREEN, self.start_bot)
        self.start_btn.pack(side="left")
        self.stop_btn = control_button("■", RED, self.stop_bot)
        self.stop_btn.pack(side="left", padx=(10, 0))
        self.stop_btn.config(state="disabled")
        self.update_btn = control_button("⟳", BLUE, self.start_update)
        self.update_btn.pack(side="left", padx=(10, 0))

        # Big LIVE / OFFLINE indicator next to the icons.
        self.live_lbl = tk.Label(controls, text="●  OFFLINE", bg=BG, fg=MUT,
                                 font=(FONT, 13, "bold"))
        self.live_lbl.pack(side="left", padx=(18, 0))

        log_card = self._card(right, "Live activity")
        log_card.grid(row=1, column=0, sticky="nsew")
        clear_btn = tk.Button(log_card.inner, text="CLEAR", command=self.clear_log,
                              bg=CARD, fg=MUT, activebackground=EDGE,
                              activeforeground=FG, font=(FONT, 8, "bold"),
                              relief="flat", padx=10, pady=2, cursor="hand2", bd=0,
                              highlightthickness=1, highlightbackground=EDGE)
        clear_btn.place(relx=1.0, y=-6, anchor="ne")
        hover(clear_btn, CARD, "#182029")
        self.log_text = tk.Text(log_card.inner, bg=FIELD, fg="#93a8bd",
                                relief="flat", font=(MONO, 9), state="disabled",
                                wrap="word", height=10,
                                highlightthickness=0, padx=10, pady=8)
        self.log_text.pack(fill="both", expand=True)

        # ---------- footer ----------
        footer = tk.Frame(self, bg=BG)
        footer.pack(fill="x", padx=26, pady=(4, 12))
        link = tk.Label(footer, text=GITHUB_REPO.replace("https://", ""), bg=BG,
                        fg=MUT, cursor="hand2", font=(FONT, 8, "underline"))
        link.pack(side="right")
        link.bind("<Button-1>", lambda _e: webbrowser.open(GITHUB_REPO))
        self.update_lbl = tk.Label(footer, text="", bg=BG, fg=MUT, font=(FONT, 8))
        self.update_lbl.pack(side="right", padx=(0, 16))
        tk.Label(footer, text="Every trade opens with SL + TP · 5% daily target · "
                              "full loss-guard stack", bg=BG, fg=MUT,
                 font=(FONT, 8)).pack(side="left")

        self.protocol("WM_DELETE_WINDOW", self.on_close)

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
        if self.bot_process and self.bot_process.poll() is None:
            return
        main_py = os.path.join(BASE_DIR, "main.py")
        if not os.path.exists(main_py):
            messagebox.showerror("Error", f"main.py not found in:\n{BASE_DIR}")
            return
        self.start_btn.config(state="disabled")
        self.live_lbl.config(text="●  STARTING…", fg=GOLD)
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
            self._kill_orphan_bots()
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
                    "Log in to MT5, enable algo trading, then press START again."))
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
        self.status_pill.config(text="  ●  BOT LIVE  ", fg=GREEN)
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
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("1.0", "Log cleared — waiting for new activity…")
        self.log_text.config(state="disabled")

    def stop_bot(self):
        if self.bot_process and self.bot_process.poll() is None:
            self.bot_process.terminate()
        self.bot_process = None
        threading.Thread(target=self._kill_orphan_bots, daemon=True).start()
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.status_pill.config(text="  ●  BOT OFFLINE  ", fg=MUT)
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
        self.update_btn.config(state="disabled", text="⟳")
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
            self.version_lbl.config(text=ver_mod.VERSION)
            return ver_mod.VERSION
        except Exception:
            return APP_VERSION

    def _update_done(self, success: bool, message: str, stamp: str | None = None):
        self.update_btn.config(state="normal", text="⟳")
        if stamp:
            self._load_update_label()
        if success:
            ver = self._reload_version_label()
            messagebox.showinfo("Update complete",
                                f"Now running {ver}\n\n{message}")
            if self.bot_process and self.bot_process.poll() is None:
                self.stop_bot()
            self.start_bot()
        else:
            messagebox.showerror("Update", message)

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
        win.title("Bot settings — defaults & rules")
        win.configure(bg=BG)
        win.geometry("620x560")
        win.minsize(520, 480)
        win.grab_set()

        tk.Label(win, text="BOT SETTINGS", bg=BG, fg=GOLD,
                 font=(FONT, 14, "bold")).pack(anchor="w", padx=18, pady=(14, 2))
        tk.Label(win, text="All values the bot follows. Saved to settings.json — restart bot to apply.",
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
            # Mark that the user explicitly tuned strategy values, so config.py
            # applies them (account-only files never freeze strategy defaults).
            data["user_tuned"] = True
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
        self.log_text.config(state="normal")
        if self.log_text.get("1.0", "end-1c") != text:
            self.log_text.delete("1.0", "end")
            self.log_text.insert("1.0", text)
            self.log_text.see("end")
        self.log_text.config(state="disabled")

        self.after(1000, self._poll)


if __name__ == "__main__":
    app = ControlPanel()
    app.mainloop()
