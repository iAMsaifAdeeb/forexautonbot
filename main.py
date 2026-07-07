"""
XAUUSD M15 auto-trading bot — entry point.

Run:  python main.py
Stop: Ctrl+C (open positions keep their SL/TP on the broker side).
"""

import logging
import sys
import time

from config import CONFIG
from indicators import add_indicators
from mt5_client import MT5Client
from risk_manager import RiskManager, MODE_TARGET_DONE
from startup_test import run_startup_test
from trade_manager import TradeManager
import market_structure as ms
import strategy


def setup_logging():
    fmt = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(CONFIG["log_file"], encoding="utf-8"),
        ],
    )
    return logging.getLogger("bot")


def main():
    log = setup_logging()
    log.info("=" * 60)
    log.info("XAUUSD M15 structure-following bot starting")
    log.info("=" * 60)

    client = MT5Client(CONFIG)
    if not client.connect():
        log.error("Cannot connect to MetaTrader 5. Is the terminal installed and running?")
        sys.exit(1)

    trader = TradeManager(CONFIG, client)
    if not run_startup_test(client, CONFIG):
        log.error("Startup test failed — fix MT5 connection and try again.")
        client.shutdown()
        sys.exit(1)

    risk = RiskManager(CONFIG, client.account_equity())

    last_bar_time = None
    try:
        while True:
            df = client.get_rates()
            if df is None or len(df) < CONFIG["ema_slow"] + 10:
                time.sleep(CONFIG["poll_seconds"])
                continue

            # The last row is the still-forming candle — work with closed ones.
            closed = df.iloc[:-1].reset_index(drop=True)
            newest_closed_time = closed["time"].iloc[-1]
            is_new_bar = newest_closed_time != last_bar_time

            equity = client.account_equity()
            positions = client.positions()
            day_profits = client.today_deal_profits()
            mode = risk.update(equity, bool(positions), day_profits,
                               balance=client.account_balance())

            # Rule 8: target reached -> close everything, wait for tomorrow.
            if mode == MODE_TARGET_DONE and positions:
                trader.close_all("daily 5% target reached")
                positions = []

            if is_new_bar:
                last_bar_time = newest_closed_time
                risk.on_new_bar()

                analyzed = add_indicators(closed, CONFIG)
                current_atr = float(analyzed["atr"].iloc[-1])

                # Manage running trades every bar: staged protection ladder
                # (half-risk -> breakeven -> lock -> structure/ATR trailing).
                if positions:
                    structure_now = ms.analyze(analyzed, CONFIG["swing_lookback"])
                    trader.manage_positions(current_atr, structure_now)

                # Weekend protection: never hold gold over the weekend gap.
                if (newest_closed_time.dayofweek == 4
                        and newest_closed_time.hour >= CONFIG["friday_close_hour"]):
                    if positions:
                        trader.close_all("weekend protection")
                        positions = []
                    log.info("Bar %s | weekend protection active — flat until Monday.",
                             newest_closed_time)
                    time.sleep(CONFIG["poll_seconds"])
                    continue

                allowed, block_reason = risk.can_open_trade(len(positions))
                if not allowed:
                    log.info("Bar %s | equity %.2f | mode %s | no entry: %s",
                             newest_closed_time, equity, mode, block_reason)
                else:
                    signal, explanation = strategy.evaluate(analyzed, CONFIG)
                    if signal is None:
                        log.info("Bar %s | equity %.2f | mode %s | %s",
                                 newest_closed_time, equity, mode, explanation)
                    else:
                        sl_distance = abs(signal.entry_hint - signal.stop_loss)
                        risk_pct = risk.current_risk_pct(signal.confidence)
                        volume = risk.lot_size(equity, sl_distance,
                                               client.symbol_info(), risk_pct)
                        if volume <= 0:
                            log.warning("Signal found but lot size is 0 — skipping.")
                        else:
                            log.info("SIGNAL: %s | %s | confidence %.0f/100 | "
                                     "risk %.2f%% | %.2f lots",
                                     signal.direction, signal.reason,
                                     signal.confidence, risk_pct, volume)
                            if trader.open_trade(
                                signal.direction, volume,
                                signal.stop_loss, signal.take_profit,
                                signal.reason,
                            ):
                                risk.on_trade_opened()

            time.sleep(CONFIG["poll_seconds"])

    except KeyboardInterrupt:
        log.info("Stopped by user. Open positions remain protected by SL/TP.")
    finally:
        risk.save()
        client.shutdown()


if __name__ == "__main__":
    main()
