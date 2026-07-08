"""Bar-by-bar decision trace for TODAY's session (timestamps shifted to
broker/UTC time so the session filter behaves like the live bot).
Run: python replay_today.py"""

import pandas as pd
import yfinance as yf

from config import CONFIG
from indicators import add_indicators
import strategy

raw = yf.download("GC=F", interval="5m", period="3d", progress=False,
                  auto_adjust=True)
if isinstance(raw.columns, pd.MultiIndex):
    raw.columns = [c[0].lower() for c in raw.columns]
else:
    raw.columns = [c.lower() for c in raw.columns]
raw = raw.reset_index()
time_col = "Datetime" if "Datetime" in raw.columns else raw.columns[0]

# yfinance GC=F timestamps are US-Eastern; broker server runs on UTC (+4h).
times = pd.to_datetime(raw[time_col])
try:
    times = times.dt.tz_convert("UTC").dt.tz_localize(None)
except TypeError:
    times = times + pd.Timedelta(hours=4)

df = pd.DataFrame({
    "time": times,
    "open": raw["open"].astype(float),
    "high": raw["high"].astype(float),
    "low": raw["low"].astype(float),
    "close": raw["close"].astype(float),
    "tick_volume": raw["volume"].fillna(0).astype(float).clip(lower=1),
}).dropna().reset_index(drop=True)

last_day = df["time"].dt.date.iloc[-1]
print(f"bars: {len(df)} | today = {last_day} | "
      f"{df['time'].iloc[0]} .. {df['time'].iloc[-1]} (UTC)")

warmup = max(CONFIG["ema_slow"] + 20, 260)
for end in range(warmup, len(df)):
    w = add_indicators(df.iloc[:end + 1].reset_index(drop=True), CONFIG)
    bar_time = w["time"].iloc[-1]
    if bar_time.date() != last_day or bar_time.hour < 5:
        continue
    s, why = strategy.evaluate(w, CONFIG)
    last = w.iloc[-1]
    tag = f"SIGNAL {s.direction} conf {s.confidence}" if s else why
    print(f"{bar_time:%H:%M} close {last['close']:8.1f} "
          f"adx {last['adx']:4.1f} rsi {last['rsi']:4.1f} "
          f"chop {last['chop']:4.1f} | {tag}")
