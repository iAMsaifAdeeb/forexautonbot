"""Startup connectivity test: open 0.01 BUY + 0.01 SELL together, close
both after a few seconds (sequential fallback on netting accounts)."""

import logging
import time

import MetaTrader5 as mt5

from email_notifier import notify_test_flight
from mt5_orders import is_hedging_account, send_deal, test_sl_tp

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
    test_magic = config["magic_number"] + TEST_MAGIC_OFFSET

    tick = client.get_tick()
    info = client.symbol_info()
    if tick is None or info is None:
        log.error("Startup test failed: no tick/symbol data.")
        return False

    log.info("=" * 60)
    log.info("STARTUP TEST — verifying BUY/SELL pipeline (%.2f lots each)", volume)
    log.info("=" * 60)

    # The user's spec: open 1 BUY + 1 SELL at the same time, wait, close both.
    # Only netting accounts can't hold both sides at once — there the two
    # orders would cancel each other, so we fall back to one-by-one.
    if is_hedging_account():
        ok = _test_parallel(symbol, volume, test_magic, config)
    else:
        log.info("Netting account detected — BUY+SELL together would cancel "
                 "out, testing one by one instead.")
        ok = _test_sequential(symbol, volume, test_magic, config)
    if not ok:
        cleanup_test_positions(config)
    return ok


def cleanup_test_positions(config: dict):
    """Force-close any leftover startup-test positions."""
    symbol = config["symbol"]
    magic = config["magic_number"] + TEST_MAGIC_OFFSET
    if _close_test_positions(symbol, magic, config, retries=8):
        log.info("Startup test cleanup: all test positions closed.")
    else:
        log.warning("Startup test cleanup: some test positions may still be open — check MT5.")


def _test_parallel(symbol, volume, magic, config) -> bool:
    """Open BUY + SELL together, wait a few seconds, close both."""
    pause = max(1, int(config.get("startup_test_seconds", 5)))
    opened = 0
    for direction, order_type in (
        ("BUY", mt5.ORDER_TYPE_BUY),
        ("SELL", mt5.ORDER_TYPE_SELL),
    ):
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            log.error("Startup test FAILED: no tick for %s.", direction)
            break
        is_buy = direction == "BUY"
        price = tick.ask if is_buy else tick.bid
        sl, tp = test_sl_tp(symbol, price, is_buy)
        ticket = _send_test_order(symbol, direction, order_type, volume, price,
                                  sl, tp, magic, config)
        if not ticket:
            log.error("Startup test FAILED: could not open %s.", direction)
            break
        log.info("Startup test OPENED %s ticket %s", direction, ticket)
        opened += 1

    if opened < 2:
        return False

    time.sleep(pause)
    if not _close_test_positions(symbol, magic, config):
        log.error("Startup test FAILED: could not close both test positions.")
        return False

    log.info("Startup test PASSED (BUY + SELL together, closed after %ds).", pause)
    log.info("=" * 60)
    notify_test_flight(config)
    return True


def _test_sequential(symbol, volume, magic, config) -> bool:
    """Open BUY → close → open SELL → close."""
    pause = max(1, int(config.get("startup_test_seconds", 3)))
    for direction, order_type in (
        ("BUY", mt5.ORDER_TYPE_BUY),
        ("SELL", mt5.ORDER_TYPE_SELL),
    ):
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            log.error("Startup test FAILED: no tick for %s.", direction)
            return False
        is_buy = direction == "BUY"
        price = tick.ask if is_buy else tick.bid
        sl, tp = test_sl_tp(symbol, price, is_buy)
        ticket = _send_test_order(symbol, direction, order_type, volume, price,
                                  sl, tp, magic, config)
        if not ticket:
            log.error("Startup test FAILED: could not open %s.", direction)
            return False
        log.info("Startup test OPENED %s ticket %s", direction, ticket)
        time.sleep(pause)
        if not _close_test_positions(symbol, magic, config):
            log.error("Startup test FAILED: could not close %s test position.", direction)
            return False
        log.info("Startup test %s leg complete (opened + closed).", direction)
        time.sleep(1)

    log.info("Startup test PASSED (sequential BUY + SELL).")
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
        "deviation": config["deviation_points"],
        "magic": magic,
        "comment": TEST_COMMENT,
        "type_time": mt5.ORDER_TIME_GTC,
    }
    if sl and tp:
        request["sl"] = sl
        request["tp"] = tp
    result = send_deal(request, symbol)
    return result.order if result and result.retcode == mt5.TRADE_RETCODE_DONE else None


def _close_test_positions(symbol, magic, config, retries: int = 5) -> bool:
    """Close all test positions; retries until flat or out of attempts."""
    for attempt in range(1, retries + 1):
        positions = [p for p in (mt5.positions_get(symbol=symbol) or []) if p.magic == magic]
        if not positions:
            return True
        for pos in positions:
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                time.sleep(1)
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
                log.info("Startup test CLOSED ticket %s", pos.ticket)
            else:
                code = result.retcode if result else mt5.last_error()
                log.warning("Close attempt %d/%d ticket %s: %s",
                            attempt, retries, pos.ticket, code)
        time.sleep(1)

    remaining = [p for p in (mt5.positions_get(symbol=symbol) or []) if p.magic == magic]
    return len(remaining) == 0
