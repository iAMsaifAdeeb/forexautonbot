"""Vectorized indicator calculations on OHLC dataframes."""

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / period, adjust=False).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index — measures trend strength (not direction)."""
    up_move = df["high"].diff()
    down_move = -df["low"].diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr_smooth = true_range(df).ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / tr_smooth
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / tr_smooth

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / period, adjust=False).mean().fillna(0)


def choppiness(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Choppiness Index: > 61.8 = sideways chop, < 38.2 = strong trend.
    Purpose-built to answer 'is this market ranging?'"""
    tr_sum = true_range(df).rolling(period).sum()
    hh = df["high"].rolling(period).max()
    ll = df["low"].rolling(period).min()
    span = (hh - ll).replace(0, np.nan)
    chop = 100 * np.log10(tr_sum / span) / np.log10(period)
    return chop.fillna(50)


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def add_indicators(df: pd.DataFrame, config: dict) -> pd.DataFrame:
    df = df.copy()
    df["ema_fast"] = ema(df["close"], config["ema_fast"])
    df["ema_slow"] = ema(df["close"], config["ema_slow"])
    df["atr"] = atr(df, config["atr_period"])
    df["adx"] = adx(df, config["adx_period"])
    df["rsi"] = rsi(df["close"], config["rsi_period"])
    df["vol_sma"] = df["tick_volume"].rolling(config["volume_sma_period"]).mean()
    df["chop"] = choppiness(df, config["chop_period"])
    return df
