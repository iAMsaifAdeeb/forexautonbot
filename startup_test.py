"""Startup connectivity test: open 0.01 BUY + 0.01 SELL, close after 10 s."""

import logging
import time

import MetaTrader5 as mt5

from email_notifier import notify_test_flight
from mt5_orders import is_hedging_account, round_price, send_deal

log = logging.getLogger("bot.startup_test")

TEST_COMMENT = "STARTUP TEST"
TEST_MAGIC_OFFSET = 1


def run_startup_test(client, config: dict) -> bool:
    """Verify the full trade pipeline works before live trading."""
    if not config.get("startup_test_enabled", True):
        log.info("Startup test skipped (disabled in config).")
        return True

    symbol = config["symbol"]
    volume = config.get("startup_test_volume", 0.01)
    hold = config.get("startup_test_seconds", 10)
    test_magic = config["magic_number"] + TEST_MAGIC_OFFSET

    tick = client.get_tick()
    info = client.symbol_info()
    if tick is None or info is None:
        log.error("Startup test failed: no tick/symbol data.")
        return False

    point = info.point or 0.01
    distance = max(info.trade_tick_size * 500, point * 500)

    log.info("=" * 60)
    log.info("STARTUP TEST — verifying BUY/SELL pipeline (%.2f lots each)", volume)
    log.info("=" * 60)

    # Netting accounts (most Exness demos): BUY then SELL nets to flat, so
    # always test one leg at a time. Hedging accounts can hold both together.
    if is_hedging_account():
        return _test_hedging(symbol, volume, hold, test_magic, distance, config)
    return _test_sequential(symbol, volume, test_magic, distance, config)


def _test_sequential(symbol, volume, magic, distance, config) -> bool:
    """Open BUY → close → open SELL → close (works on netting accounts)."""
    for direction, order_type in (
        ("BUY", mt5.ORDER_TYPE_BUY),
        ("SELL", mt5.ORDER_TYPE_SELL),
    ):
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            log.error("Startup test FAILED: no tick for %s.", direction)
            _close_test_positions(symbol, magic, config)
            return False
        price = tick.ask if direction == "BUY" else tick.bid
        sl = price - distance if direction == "BUY" else price + distance
        tp = price + distance if direction == "BUY" else price - distance
        ticket = _send_test_order(symbol, direction, order_type, volume, price,
                                  sl, tp, magic, config)
        if not ticket:
            log.error("Startup test FAILED: could not open %s.", direction)
            _close_test_positions(symbol, magic, config)
            return False
        log.info("Startup test OPENED %s ticket %s", direction, ticket)
        time.sleep(1)
        if not _close_test_positions(symbol, magic, config):
            log.error("Startup test FAILED: could not close %s test position.", direction)
            return False
        time.sleep(1)

    log.info("Startup test PASSED (sequential BUY + SELL on netting account).")
    log.info("=" * 60)
    notify_test_flight(config)
    return True


def _test_hedging(symbol, volume, hold, magic, distance, config) -> bool:
    """Open BUY and SELL together, hold, then close both (hedging accounts)."""
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return False

    opened = 0
    for direction, order_type, price in (
        ("BUY", mt5.ORDER_TYPE_BUY, tick.ask),
        ("SELL", mt5.ORDER_TYPE_SELL, tick.bid),
    ):
        sl = price - distance if direction == "BUY" else price + distance
        tp = price + distance if direction == "BUY" else price - distance
        ticket = _send_test_order(symbol, direction, order_type, volume, price,
                                  sl, tp, magic, config)
        if ticket:
            opened += 1
            log.info("Startup test OPENED %s ticket %s", direction, ticket)

    if opened == 0:
        log.error("Startup test FAILED: no test orders opened.")
        return False

    log.info("Holding test positions for %d seconds…", hold)
    time.sleep(hold)
    closed = _close_test_positions(symbol, magic, config)
    if closed < opened:
        log.error("Startup test FAILED: closed %d/%d test positions.", closed, opened)
        return False

    log.info("Startup test PASSED — closed %d test position(s).", closed)
    log.info("=" * 60)
    notify_test_flight(config)
    return True


def _send_test_order(symbol, direction, order_type, volume, price, sl, tp,
                     magic, config) -> int | None:
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": order_type,
        "price": price,
        "sl": round_price(symbol, sl),
        "tp": round_price(symbol, tp),
        "deviation": config["deviation_points"],
        "magic": magic,
        "comment": TEST_COMMENT,
        "type_time": mt5.ORDER_TIME_GTC,
    }
    result = send_deal(request, symbol)
    return result.order if result and result.retcode == mt5.TRADE_RETCODE_DONE else None


def _close_test_positions(symbol, magic, config) -> bool:
    """Close all test positions; returns True if none remain."""
    closed_any = False
    for pos in [p for p in (mt5.positions_get(symbol=symbol) or []) if p.magic == magic]:
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            continue
        if pos.type == mt5.POSITION_TYPE_BUY:
            order_type, price = mt5.ORDER_TYPE_SELL, tick.bid
        else:
            order_type, price = mt5.ORDER_TYPE_BUY, tick.ask
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": pos.volume,
            "type": order_type,
            "position": pos.ticket,
            "price": price,
            "deviation": config["deviation_points"],
            "magic": magic,
            "comment": "TEST CLOSE",
            "type_time": mt5.ORDER_TIME_GTC,
        }
        result = send_deal(request, symbol)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            closed_any = True
            log.info("Startup test CLOSED ticket %s", pos.ticket)
        else:
            code = result.retcode if result else mt5.last_error()
            log.error("Startup test could not close ticket %s: %s", pos.ticket, code)

    remaining = [p for p in (mt5.positions_get(symbol=symbol) or []) if p.magic == magic]
    return len(remaining) == 0
