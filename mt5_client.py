"""
Thin wrapper around the MetaTrader5 package: connection, market data,
account and symbol information.
"""

import logging

import MetaTrader5 as mt5
import pandas as pd

log = logging.getLogger("bot.mt5")

TIMEFRAME_MAP = {
    1: mt5.TIMEFRAME_M1,
    5: mt5.TIMEFRAME_M5,
    15: mt5.TIMEFRAME_M15,
    30: mt5.TIMEFRAME_M30,
    60: mt5.TIMEFRAME_H1,
    240: mt5.TIMEFRAME_H4,
    1440: mt5.TIMEFRAME_D1,
}


class MT5Client:
    def __init__(self, config: dict):
        self.config = config
        self.symbol = config["symbol"]
        self.timeframe = TIMEFRAME_MAP[config["timeframe_minutes"]]

    # ----- lifecycle -----

    def connect(self) -> bool:
        kwargs = {}
        if self.config.get("mt5_terminal_path"):
            kwargs["path"] = self.config["mt5_terminal_path"]
        if self.config.get("mt5_login"):
            kwargs["login"] = self.config["mt5_login"]
            kwargs["password"] = self.config["mt5_password"]
            kwargs["server"] = self.config["mt5_server"]

        if not mt5.initialize(**kwargs):
            log.error("MT5 initialize failed: %s", mt5.last_error())
            return False

        if not mt5.symbol_select(self.symbol, True):
            log.error("Could not select symbol %s: %s", self.symbol, mt5.last_error())
            return False

        info = mt5.account_info()
        log.info(
            "Connected to MT5. Account %s | balance %.2f | equity %.2f | leverage 1:%d",
            info.login, info.balance, info.equity, info.leverage,
        )
        return True

    def shutdown(self):
        mt5.shutdown()

    # ----- data -----

    def get_rates(self, count: int | None = None) -> pd.DataFrame | None:
        count = count or self.config["bars_to_load"]
        rates = mt5.copy_rates_from_pos(self.symbol, self.timeframe, 0, count)
        if rates is None or len(rates) == 0:
            log.warning("No rates received for %s: %s", self.symbol, mt5.last_error())
            return None
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        return df

    def get_tick(self):
        return mt5.symbol_info_tick(self.symbol)

    # ----- account / symbol -----

    def account_equity(self) -> float:
        info = mt5.account_info()
        return float(info.equity) if info else 0.0

    def account_balance(self) -> float:
        info = mt5.account_info()
        return float(info.balance) if info else 0.0

    def symbol_info(self):
        return mt5.symbol_info(self.symbol)

    def positions(self):
        """Open positions created by this bot (matched by magic number)."""
        positions = mt5.positions_get(symbol=self.symbol) or []
        magic = self.config["magic_number"]
        return [p for p in positions if p.magic == magic]
