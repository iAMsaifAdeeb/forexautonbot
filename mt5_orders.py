"""Shared MT5 order helpers — broker-correct filling modes and deal send."""

import logging

import MetaTrader5 as mt5

log = logging.getLogger("bot.orders")


def filling_modes(symbol: str) -> list:
    """Return filling modes this broker actually supports for the symbol."""
    info = mt5.symbol_info(symbol)
    if info is None:
        return [mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN]
    fm = int(info.filling_mode)
    modes = []
    # Bitmask per MT5: FOK=1, IOC=2, RETURN/BOC=4
    if fm & 1:
        modes.append(mt5.ORDER_FILLING_FOK)
    if fm & 2:
        modes.append(mt5.ORDER_FILLING_IOC)
    if fm & 4:
        modes.append(mt5.ORDER_FILLING_RETURN)
    if not modes:
        modes = [mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN]
    return modes


def is_hedging_account() -> bool:
    acc = mt5.account_info()
    if acc is None:
        return False
    return acc.margin_mode == mt5.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING


def round_price(symbol: str, price: float) -> float:
    info = mt5.symbol_info(symbol)
    digits = info.digits if info else 3
    return round(price, digits)


def send_deal(request: dict, symbol: str) -> object | None:
    """Send a market deal trying each broker-supported filling mode."""
    for filling in filling_modes(symbol):
        req = dict(request, type_filling=filling)
        result = mt5.order_send(req)
        if result is None:
            continue
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            return result
        if result.retcode != mt5.TRADE_RETCODE_INVALID_FILL:
            log.warning("order_send %s: %s %s", symbol, result.retcode, result.comment)
            return result
    log.warning("order_send %s: all filling modes rejected (%s)",
                symbol, mt5.last_error())
    return None
