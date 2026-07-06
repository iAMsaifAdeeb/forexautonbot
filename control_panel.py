"""
XAUUSD Bot Control Panel — a windowed app to configure and run the bot.

- Edit every important setting in a friendly window.
- "Save Settings" writes settings.json, which the bot loads on startup
  (defaults in config.py are never touched).
- Start / stop the bot with one click and watch its live log.
- Shows live account state: mode, trades today, day-start equity.

Run directly:      python control_panel.py
Or use the built:  "XAUUSD Bot Control Panel.exe"
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
from tkinter import messagebox, ttk

BASE_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")
STATE_FILE = os.path.join(BASE_DIR, "bot_state.json")
LOG_FILE = os.path.join(BASE_DIR, "bot.log")
CONFIG_FILE = os.path.join(BASE_DIR, "config.py")

GITHUB_REPO = "https://github.com/iAMsaifAdeeb/forexautonbot"
GITHUB_ZIP = "https://codeload.github.com/iAMsaifAdeeb/forexautonbot/zip/refs/heads/main"
# Local files the updater must NEVER overwrite (your settings, state, logs).
UPDATE_PROTECTED = {"settings.json", "bot_state.json", "bot.log", "test_state.json"}

BG = "#101418"
PANEL = "#1a2027"
FG = "#e8edf2"
ACCENT = "#d4a017"      # gold
GREEN = "#2ecc71"
RED = "#e74c3c"


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


# (key, label, type, tooltip) grouped by section
SECTIONS = [
    ("Trading", [
        ("symbol", "Symbol", str, "Broker's exact name: XAUUSD / GOLD / XAUUSDm"),
        ("daily_target_pct", "Daily profit target %", float, "Stop trading for the day at this gain"),
        ("max_trades_per_day", "Max trades per day", int, "0 = unlimited (trade until target)"),
        ("min_reward_risk", "Take-profit (x risk)", float, "TP distance = this x SL distance"),
    ]),
    ("Risk", [
        ("risk_per_trade_pct", "Risk per trade %", float, "% of equity lost if SL is hit"),
        ("recovery_risk_pct", "Recovery-mode risk %", float, "Reduced risk after a drawdown"),
        ("max_drawdown_pct", "Max drawdown %", float, "Loss that triggers observe/recover mode"),
        ("min_confidence", "Min setup confidence", float, "Signals scoring below this are skipped (0-100)"),
        ("high_confidence_score", "High-confidence score", float, "Setups above this earn bigger risk"),
        ("high_confidence_risk_pct", "High-confidence risk %", float, "Risk used on exceptional setups"),
    ]),
    ("Loss guards", [
        ("daily_loss_limit_pct", "Daily loss limit %", float, "Day stops at this loss from day start"),
        ("profit_lock_trigger_pct", "Profit lock trigger %", float, "Start protecting day profit at this gain"),
        ("profit_lock_giveback_pct", "Max giveback %", float, "Never give back more than this share of the peak"),
        ("consec_loss_count", "Losses in a row to pause", int, "Cooldown trigger"),
        ("loss_pause_bars", "Cooldown length (bars)", int, "15-min bars to sit out after the streak"),
        ("max_spread_points", "Max spread (points)", int, "Skip entries when spread is wider"),
        ("friday_close_hour", "Friday close hour", int, "Close everything at this server hour on Friday"),
    ]),
    ("Market filters", [
        ("adx_min", "Min ADX (trend strength)", float, "Higher = only strong trends"),
        ("chop_max", "Max choppiness", float, "Lower = stricter sideways lockout"),
        ("spike_atr_mult", "Spike size (x ATR)", float, "Candles bigger than this pause trading"),
        ("spike_pause_bars", "Pause after spike (bars)", int, "15-min bars to wait after a spike"),
    ]),
    ("Trade management", [
        ("protect_rr", "Half-risk at (R)", float, "Cut remaining risk in half at this profit"),
        ("breakeven_rr", "Breakeven at (R)", float, "Stop moves to entry + buffer here"),
        ("lock_rr", "Lock profit at (R)", float, "Guarantees +0.5R once reached"),
        ("trail_atr_mult", "Trail distance (x ATR)", float, "How far the trailing stop follows"),
        ("time_stop_bars", "Time stop (bars)", int, "Close flat trades after this many bars"),
    ]),
    ("Sessions (server time)", [
        ("trading_hours", "Trading hours (start-end)", "hours", "e.g. 7-21"),
        ("blackout_windows", "News blackouts", "windows", "e.g. 15:15-15:50, 16:55-17:20"),
    ]),
    ("MT5 account (optional)", [
        ("mt5_login", "Login (number)", "opt_int", "Leave empty to use the logged-in terminal"),
        ("mt5_password", "Password", "password", "Stored locally in settings.json only"),
        ("mt5_server", "Server", "opt_str", "e.g. Exness-MT5Trial"),
    ]),
]


class ControlPanel(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("XAUUSD Trading Bot — Control Panel")
        self.configure(bg=BG)
        self.resizable(False, True)
        self.bot_process: subprocess.Popen | None = None
        self.entries: dict[str, tk.Entry] = {}

        try:
            cfg = load_effective_config()
        except Exception as exc:  # config.py missing/broken
            messagebox.showerror("Error", f"Could not read config.py:\n{exc}")
            self.destroy()
            return
        self.cfg = cfg

        self._build_ui()
        self._poll()

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=PANEL, foreground=FG, padding=(12, 6))
        style.map("TNotebook.Tab", background=[("selected", ACCENT)],
                  foreground=[("selected", "#000000")])

        header = tk.Label(self, text="XAUUSD  M15  TRADING BOT", bg=BG, fg=ACCENT,
                          font=("Segoe UI", 15, "bold"), pady=8)
        header.pack(fill="x")

        repo_link = tk.Label(self, text=GITHUB_REPO, bg=BG, fg="#58a6ff",
                             cursor="hand2", font=("Segoe UI", 9, "underline"))
        repo_link.pack()
        repo_link.bind("<Button-1>", lambda _e: webbrowser.open(GITHUB_REPO))

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=10)

        # ----- left: settings notebook -----
        left = tk.Frame(body, bg=BG)
        left.pack(side="left", fill="y")

        notebook = ttk.Notebook(left)
        notebook.pack(fill="both", expand=True)

        for section, fields in SECTIONS:
            tab = tk.Frame(notebook, bg=PANEL, padx=12, pady=10)
            notebook.add(tab, text=section)
            for row, (key, label, ftype, tip) in enumerate(fields):
                tk.Label(tab, text=label, bg=PANEL, fg=FG, anchor="w",
                         font=("Segoe UI", 10)).grid(row=row * 2, column=0,
                                                     sticky="w", pady=(6, 0))
                show = "*" if ftype == "password" else ""
                entry = tk.Entry(tab, width=26, bg="#0d1117", fg=FG,
                                 insertbackground=FG, relief="flat", show=show,
                                 font=("Consolas", 11))
                entry.grid(row=row * 2, column=1, padx=(12, 0), pady=(6, 0))
                entry.insert(0, self._format_value(key, ftype))
                self.entries[key] = entry
                tk.Label(tab, text=tip, bg=PANEL, fg="#7a8794", anchor="w",
                         font=("Segoe UI", 8)).grid(row=row * 2 + 1, column=0,
                                                    columnspan=2, sticky="w")

        save = tk.Button(left, text="SAVE  SETTINGS", command=self.save_settings,
                         bg=ACCENT, fg="#000000", font=("Segoe UI", 11, "bold"),
                         relief="flat", pady=8, cursor="hand2")
        save.pack(fill="x", pady=8)

        # ----- right: bot control + status + log -----
        right = tk.Frame(body, bg=BG)
        right.pack(side="left", fill="both", expand=True, padx=(12, 0))

        controls = tk.Frame(right, bg=BG)
        controls.pack(fill="x")
        self.start_btn = tk.Button(controls, text="START BOT", command=self.start_bot,
                                   bg=GREEN, fg="#000000", font=("Segoe UI", 11, "bold"),
                                   relief="flat", padx=16, pady=8, cursor="hand2")
        self.start_btn.pack(side="left")
        self.stop_btn = tk.Button(controls, text="STOP BOT", command=self.stop_bot,
                                  bg=RED, fg="#000000", font=("Segoe UI", 11, "bold"),
                                  relief="flat", padx=16, pady=8, cursor="hand2",
                                  state="disabled")
        self.stop_btn.pack(side="left", padx=(8, 0))
        self.update_btn = tk.Button(controls, text="UPDATE FROM GITHUB",
                                    command=self.start_update,
                                    bg="#3b82f6", fg="#000000",
                                    font=("Segoe UI", 11, "bold"),
                                    relief="flat", padx=16, pady=8, cursor="hand2")
        self.update_btn.pack(side="left", padx=(8, 0))
        self.status_lbl = tk.Label(controls, text="Bot: stopped", bg=BG, fg="#7a8794",
                                   font=("Segoe UI", 10, "bold"))
        self.status_lbl.pack(side="left", padx=16)

        self.state_lbl = tk.Label(right, text="", bg=BG, fg=FG, anchor="w",
                                  justify="left", font=("Consolas", 9))
        self.state_lbl.pack(fill="x", pady=(8, 4))

        tk.Label(right, text="LIVE LOG", bg=BG, fg=ACCENT,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self.log_text = tk.Text(right, width=88, height=28, bg="#0d1117", fg="#9fb3c8",
                                relief="flat", font=("Consolas", 9), state="disabled",
                                wrap="none")
        self.log_text.pack(fill="both", expand=True, pady=(2, 10))

        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------------------------------------------------------- formatting

    def _format_value(self, key: str, ftype) -> str:
        value = self.cfg.get(key)
        if ftype == "hours":
            return f"{value[0]}-{value[1]}"
        if ftype == "windows":
            return ", ".join(value)
        if value is None:
            return ""
        return str(value)

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
        overrides = {}
        for section, fields in SECTIONS:
            for key, label, ftype, _tip in fields:
                raw = self.entries[key].get()
                try:
                    overrides[key] = self._parse_value(key, ftype, raw)
                except (ValueError, IndexError):
                    messagebox.showerror(
                        "Invalid value",
                        f"'{label}' has an invalid value:\n\n  {raw!r}")
                    return

        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(overrides, f, indent=2)

        if self.bot_process and self.bot_process.poll() is None:
            messagebox.showinfo(
                "Saved", "Settings saved.\n\nThe bot is running — restart it "
                "(Stop, then Start) to apply the new settings.")
        else:
            messagebox.showinfo("Saved", "Settings saved. They will apply the "
                                         "next time the bot starts.")

    def start_bot(self):
        if self.bot_process and self.bot_process.poll() is None:
            return
        main_py = os.path.join(BASE_DIR, "main.py")
        if not os.path.exists(main_py):
            messagebox.showerror("Error", f"main.py not found in:\n{BASE_DIR}")
            return
        try:
            self.bot_process = subprocess.Popen(
                [find_python(), main_py],
                cwd=BASE_DIR,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except OSError as exc:
            messagebox.showerror("Error", f"Could not start the bot:\n{exc}")
            return
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status_lbl.config(text="Bot: RUNNING", fg=GREEN)

    def stop_bot(self):
        if self.bot_process and self.bot_process.poll() is None:
            self.bot_process.terminate()
        self.bot_process = None
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.status_lbl.config(text="Bot: stopped", fg="#7a8794")

    # ------------------------------------------------------------- updater

    def start_update(self):
        if self.bot_process and self.bot_process.poll() is None:
            if not messagebox.askyesno(
                    "Bot is running",
                    "The bot must be stopped to update.\n\nStop it and update now?\n"
                    "(Open trades keep their SL/TP on the broker side.)"):
                return
            self.stop_bot()
        self.update_btn.config(state="disabled", text="UPDATING…")
        threading.Thread(target=self._update_worker, daemon=True).start()

    def _update_worker(self):
        try:
            message = self._do_update()
            success = True
        except urllib.error.HTTPError as exc:
            message = (f"GitHub download failed (HTTP {exc.code}).\n\n"
                       "If the repository is private, either make it public or "
                       "install Git on this machine and sign in to GitHub once "
                       "— the updater will then use Git automatically.")
            success = False
        except Exception as exc:
            message = f"Update failed:\n{exc}"
            success = False
        self.after(0, lambda: self._update_done(success, message))

    def _do_update(self) -> str:
        # Prefer a real git pull when this folder is a git clone.
        if os.path.isdir(os.path.join(BASE_DIR, ".git")) and shutil.which("git"):
            result = subprocess.run(
                ["git", "-C", BASE_DIR, "pull", "--ff-only"],
                capture_output=True, text=True, timeout=180,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode == 0:
                self._pip_install()
                return "Updated via Git:\n\n" + result.stdout.strip()[-500:]
            # git failed (conflicts, no credentials, ...) -> fall back to zip

        # Zip download straight from GitHub — works on any VPS, no Git needed.
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

        self._pip_install()
        return (f"Downloaded the latest version from GitHub — {copied} files "
                "updated.\n\nYour settings, state and logs were kept untouched.")

    def _pip_install(self):
        """Keep dependencies in sync with the updated requirements.txt."""
        req = os.path.join(BASE_DIR, "requirements.txt")
        if os.path.exists(req):
            subprocess.run(
                [find_python(), "-m", "pip", "install", "-r", req, "--quiet"],
                capture_output=True, timeout=600,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )

    def _update_done(self, success: bool, message: str):
        self.update_btn.config(state="normal", text="UPDATE FROM GITHUB")
        if success:
            messagebox.showinfo("Update complete",
                                message + "\n\nPress START BOT to run the new version.")
        else:
            messagebox.showerror("Update", message)

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

        # account state
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                st = json.load(f)
            self.state_lbl.config(text=(
                f"day: {st.get('day', '?')}   mode: {st.get('mode', '?')}   "
                f"trades today: {st.get('trades_today', '?')}   "
                f"day-start equity: {st.get('day_start_equity', 0):.2f}"))
        except (OSError, json.JSONDecodeError, TypeError):
            self.state_lbl.config(text="(no bot state yet — start the bot)")

        # log tail
        try:
            with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-200:]
            text = "".join(lines)
        except OSError:
            text = "(no log yet — start the bot to see activity here)"
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
