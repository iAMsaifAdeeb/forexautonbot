"""Startup connectivity test: open 0.01 BUY + 0.01 SELL, close after 10 s."""

import logging
import time

import MetaTrader5 as mt5

from email_notifier import notify_test_flight

log = logging.getLogger("bot.startup_test")

TEST_COMMENT = "STARTUP TEST"
TEST_MAGIC_OFFSET = 1  # use magic_number + 1 so test trades are easy to find


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
    # Wide SL/TP so the test cannot hit them in 10 seconds.
    distance = max(info.trade_tick_size * 500, point * 500)

    log.info("=" * 60)
    log.info("STARTUP TEST — verifying BUY/SELL pipeline (%.2f lots each)", volume)
    log.info("=" * 60)

    tickets = []
    for direction, order_type, price in (
        ("BUY", mt5.ORDER_TYPE_BUY, tick.ask),
        ("SELL", mt5.ORDER_TYPE_SELL, tick.bid),
    ):
        sl = price - distance if direction == "BUY" else price + distance
        tp = price + distance if direction == "BUY" else price - distance
        ticket = _send_test_order(symbol, direction, order_type, volume, price,
                                  sl, tp, test_magic, config)
        if ticket:
            tickets.append(ticket)
            log.info("Startup test OPENED %s ticket %s", direction, ticket)
        else:
            log.warning("Startup test could not open %s (hedging may be disabled).", direction)

    # If hedging blocked the second leg, run buy then sell sequentially.
    if len(tickets) < 2:
        _close_test_positions(symbol, test_magic, config)
        tickets.clear()
        for direction, order_type in (
            ("BUY", mt5.ORDER_TYPE_BUY),
            ("SELL", mt5.ORDER_TYPE_SELL),
        ):
            tick = client.get_tick()
            if tick is None:
                log.error("Startup test FAILED: lost tick data.")
                _close_test_positions(symbol, test_magic, config)
                return False
            price = tick.ask if direction == "BUY" else tick.bid
            sl = price - distance if direction == "BUY" else price + distance
            tp = price + distance if direction == "BUY" else price - distance
            ticket = _send_test_order(symbol, direction, order_type, volume, price,
                                      sl, tp, test_magic, config)
            if not ticket:
                log.error("Startup test FAILED on %s.", direction)
                _close_test_positions(symbol, test_magic, config)
                return False
            log.info("Startup test OPENED %s ticket %s (sequential mode)", direction, ticket)
            time.sleep(1)
            _close_test_positions(symbol, test_magic, config)
            time.sleep(1)
        log.info("Startup test PASSED (sequential BUY + SELL).")
        log.info("=" * 60)
        notify_test_flight(config)
        return True

    log.info("Holding test positions for %d seconds…", hold)
    time.sleep(hold)
    closed = _close_test_positions(symbol, test_magic, config)
    if closed == 0:
        log.error("Startup test FAILED: could not close test positions.")
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
        "sl": round(sl, 3),
        "tp": round(tp, 3),
        "deviation": config["deviation_points"],
        "magic": magic,
        "comment": TEST_COMMENT,
        "type_time": mt5.ORDER_TIME_GTC,
    }
    for filling in (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN):
        request["type_filling"] = filling
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            return result.order
        if result and result.retcode != mt5.TRADE_RETCODE_INVALID_FILL:
            log.warning("Test %s order failed: %s %s", direction, result.retcode, result.comment)
            return None
    return None


def _close_test_positions(symbol, magic, config) -> int:
    closed = 0
    positions = mt5.positions_get(symbol=symbol) or []
    for pos in [p for p in positions if p.magic == magic]:
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
        for filling in (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN):
            request["type_filling"] = filling
            result = mt5.order_send(request)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                closed += 1
                break
    return closed
