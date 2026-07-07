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
        path_kw = {}
        if self.config.get("mt5_terminal_path"):
            path_kw["path"] = self.config["mt5_terminal_path"]

        # 1) Attach to the terminal the user already logged into (most stable
        #    on VPS when Algo Trading is enabled in that same window).
        if mt5.initialize(**path_kw):
            if self._finish_connect():
                return True
            mt5.shutdown()

        # 2) Fall back to explicit account credentials.
        kwargs = dict(path_kw)
        if self.config.get("mt5_login"):
            kwargs["login"] = int(self.config["mt5_login"])
            kwargs["password"] = self.config["mt5_password"]
            kwargs["server"] = self.config["mt5_server"]
            if mt5.initialize(**kwargs):
                if self._finish_connect():
                    return True

        log.error("MT5 initialize failed: %s", mt5.last_error())
        return False

    def _finish_connect(self) -> bool:
        if not mt5.symbol_select(self.symbol, True):
            log.error("Could not select symbol %s: %s", self.symbol, mt5.last_error())
            return False
        info = mt5.account_info()
        if info is None:
            log.error("MT5 connected but no account info: %s", mt5.last_error())
            return False
        log.info(
            "Connected to MT5. Account %s | balance %.2f | equity %.2f | leverage 1:%d",
            info.login, info.balance, info.equity, info.leverage,
        )
        return True

    def shutdown(self):
        mt5.shutdown()

    def reconnect(self) -> bool:
        """Drop the broken IPC connection and connect again (e.g. after the
        MT5 terminal restarted). Retries with growing pauses."""
        import time
        log.warning("Reconnecting to MetaTrader 5…")
        for attempt, pause in enumerate((2, 5, 10), start=1):
            try:
                mt5.shutdown()
            except Exception:
                pass
            time.sleep(pause)
            if self.connect():
                return True
            log.warning("Reconnect attempt %d failed.", attempt)
        return False

    # ----- data -----

    def get_rates(self, count: int | None = None) -> pd.DataFrame | None:
        count = count or self.config["bars_to_load"]
        for attempt in (1, 2):
            rates = mt5.copy_rates_from_pos(self.symbol, self.timeframe, 0, count)
            if rates is not None and len(rates) > 0:
                df = pd.DataFrame(rates)
                df["time"] = pd.to_datetime(df["time"], unit="s")
                return df
            err = mt5.last_error()
            log.warning("No rates received for %s (attempt %d): %s",
                        self.symbol, attempt, err)
            if attempt == 1:
                mt5.symbol_select(self.symbol, True)
                if mt5.terminal_info() is None or mt5.account_info() is None:
                    self.reconnect()
        return None

    def get_rates_tf(self, minutes: int, count: int) -> pd.DataFrame | None:
        """Fetch candles for any timeframe (used by the top-down analysis)."""
        tf = TIMEFRAME_MAP.get(minutes)
        if tf is None:
            return None
        rates = mt5.copy_rates_from_pos(self.symbol, tf, 0, count)
        if rates is None or len(rates) == 0:
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

    def today_deal_profits(self) -> list[float]:
        """Profits of this bot's trades closed today, in chronological order.
        Used by the consecutive-loss cooldown."""
        from datetime import datetime, timedelta

        start = datetime.combine(datetime.now().date(), datetime.min.time())
        deals = mt5.history_deals_get(start, datetime.now() + timedelta(hours=1)) or []
        magic = self.config["magic_number"]
        return [d.profit for d in deals
                if d.magic == magic and d.entry == mt5.DEAL_ENTRY_OUT]
