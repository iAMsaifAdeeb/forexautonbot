"""
Thin launcher for Gold Genious.exe — NEVER freezes the UI.

Always starts the live control_panel.py next to this exe so Update
can change the panel without rebuilding the executable.
"""

import os
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox


def find_python() -> str:
    candidates = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Python",
                     "Python312", "pythonw.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Python",
                     "Python312", "python.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Python",
                     "Python311", "pythonw.exe"),
        "C:\\Program Files\\Python312\\pythonw.exe",
        "C:\\Program Files\\Python312\\python.exe",
        "C:\\Program Files\\Python313\\pythonw.exe",
        "pythonw",
        "python",
    ]
    for c in candidates:
        if c in ("python", "pythonw") or os.path.isfile(c):
            return c
    return "python"


def main():
    if getattr(sys, "frozen", False):
        base = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    panel = os.path.join(base, "control_panel.py")
    if not os.path.isfile(panel):
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Gold Genious",
            "control_panel.py not found next to this launcher.\n\n"
            "Run SETUP.bat in the bot folder, then try again.")
        return 1
    py = find_python()
    subprocess.Popen([py, panel], cwd=base)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
