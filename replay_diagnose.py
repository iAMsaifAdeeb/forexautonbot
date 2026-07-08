"""Replay real gold M5 data through the strategy and count WHY every bar
was rejected. Run: python replay_diagnose.py"""

from collections import Counter

import pandas as pd
import yfinance as yf

from config import CONFIG
from indicators import add_indicators
import strategy

# Gold futures ~ XAUUSD (yfinance has no spot gold M5)
raw = yf.download("GC=F", interval="5m", period="5d", progress=False,
                  auto_adjust=True)
if isinstance(raw.columns, pd.MultiIndex):
    raw.columns = [c[0].lower() for c in raw.columns]
else:
    raw.columns = [c.lower() for c in raw.columns]
raw = raw.reset_index()
time_col = "Datetime" if "Datetime" in raw.columns else raw.columns[0]
df = pd.DataFrame({
    "time": pd.to_datetime(raw[time_col]).dt.tz_localize(None),
    "open": raw["open"].astype(float),
    "high": raw["high"].astype(float),
    "low": raw["low"].astype(float),
    "close": raw["close"].astype(float),
    "tick_volume": raw["volume"].fillna(0).astype(float).clip(lower=1),
})
df = df.dropna().reset_index(drop=True)
print(f"bars: {len(df)}  |  {df['time'].iloc[0]} -> {df['time'].iloc[-1]}")
print(f"price range: {df['low'].min():.1f} .. {df['high'].max():.1f}")

reasons = Counter()
signals = []
warmup = max(CONFIG["ema_slow"] + 20, 260)
for end in range(warmup, len(df)):
    w = add_indicators(df.iloc[:end + 1].reset_index(drop=True), CONFIG)
    s, why = strategy.evaluate(w, CONFIG)
    if s:
        signals.append((str(w['time'].iloc[-1]), s.direction, s.confidence))
        reasons["SIGNAL"] += 1
    else:
        # normalise the reason so similar messages group together
        key = why
        for pat, name in (
            ("outside trading hours", "session"),
            ("blackout", "news blackout"),
            ("spike", "spike gate"),
            ("not trending (ADX", "ADX too low"),
            ("SIDEWAYS", "sideways lockout"),
            ("candles not aligned", "candles not aligned"),
            ("not above both EMAs", "EMA side (buy)"),
            ("not below both EMAs", "EMA side (sell)"),
            ("conflicting", "structure vs EMA conflict"),
            ("no trend on M5", "no trend"),
            ("confidence", "confidence gate"),
            ("parabolic", "RSI parabolic"),
            ("not fighting the big picture", "HTF veto"),
        ):
            if pat in why:
                key = name
                break
        reasons[key] += 1

print("\n--- rejection reasons (per bar) ---")
for k, v in reasons.most_common():
    print(f"{v:5d}  {k}")

print(f"\n--- signals: {len(signals)} ---")
for t, d, c in signals[:40]:
    print(f"{t}  {d}  conf {c}")
