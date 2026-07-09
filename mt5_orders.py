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


def clean_comment(text: str, limit: int = 20) -> str:
    """MT5's python API rejects the whole order when the comment is too long
    or has odd characters ((-2, 'Invalid "comment" argument')). Keep it short
    and plain ASCII."""
    ascii_only = (text or "").encode("ascii", "ignore").decode()
    kept = "".join(ch for ch in ascii_only if ch.isalnum() or ch in " +._-")
    return " ".join(kept.split())[:limit].strip()


def send_deal(request: dict, symbol: str) -> object | None:
    """Send a market deal trying each broker-supported filling mode."""
    if "comment" in request:
        request = dict(request, comment=clean_comment(request["comment"]))
    for filling in filling_modes(symbol):
        req = dict(request, type_filling=filling)
        result = mt5.order_send(req)
        if result is None:
            # Library-level rejection (never reached the broker). If it is
            # the comment argument, drop the comment — a trade matters more
            # than its label.
            err = str(mt5.last_error() or "")
            if "comment" in err and "comment" in req:
                bare = {k: v for k, v in req.items() if k != "comment"}
                result = mt5.order_send(bare)
            if result is None:
                continue
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            return result
        # Invalid stops — retry once without SL/TP (startup test / edge cases).
        if (result.retcode == mt5.TRADE_RETCODE_INVALID_STOPS
                and ("sl" in req or "tp" in req)):
            bare = {k: v for k, v in req.items() if k not in ("sl", "tp")}
            result2 = mt5.order_send(bare)
            if result2 and result2.retcode == mt5.TRADE_RETCODE_DONE:
                return result2
        if result.retcode != mt5.TRADE_RETCODE_INVALID_FILL:
            log.warning("order_send %s: %s %s", symbol, result.retcode, result.comment)
            return result
    log.warning("order_send %s: all filling modes rejected (%s)",
                symbol, mt5.last_error())
    return None


def test_sl_tp(symbol: str, price: float, is_buy: bool) -> tuple[float, float]:
    """SL/TP for startup test orders — respects broker minimum stop distance."""
    info = mt5.symbol_info(symbol)
    if info is None:
        return 0.0, 0.0
    point = info.point or 0.01
    min_pts = max(int(info.trade_stops_level or 0), 20)
    dist = min_pts * point * 2
    if is_buy:
        return round_price(symbol, price - dist), round_price(symbol, price + dist)
    return round_price(symbol, price + dist), round_price(symbol, price - dist)
