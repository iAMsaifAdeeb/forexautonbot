"""
Gold Genious — XAUUSD M5 auto-trading bot, entry point.

Run:  python main.py
Stop: Ctrl+C (open positions keep their SL/TP on the broker side).
"""

import logging
import sys
import time

from config import CONFIG
from data_heartbeat import write_heartbeat
from email_notifier import notify_on_duty, notify_target_completed
from indicators import add_indicators
from mt5_client import MT5Client
from risk_manager import RiskManager, MODE_TARGET_DONE
from startup_test import cleanup_test_positions, run_startup_test
from trade_manager import TradeManager
import market_structure as ms
import strategy
import topdown


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


def _mark_email_sent(risk: RiskManager, key: str):
    from datetime import date
    today = date.today().isoformat()
    sent = risk.state.setdefault("emails_sent", {})
    sent[key] = today
    risk.save()


def _try_duty_email(risk: RiskManager):
    from datetime import date
    today = date.today().isoformat()
    if risk.state.get("emails_sent", {}).get("duty") == today:
        return
    if notify_on_duty(CONFIG):
        _mark_email_sent(risk, "duty")


def _try_target_email(risk: RiskManager, equity: float):
    from datetime import date
    today = date.today().isoformat()
    if risk.state.get("emails_sent", {}).get("target") == today:
        return
    if notify_target_completed(CONFIG, equity, CONFIG["daily_target_pct"]):
        _mark_email_sent(risk, "target")


def main():
    log = setup_logging()
    log.info("=" * 60)
    log.info("GOLD GENIOUS — XAUUSD M5 hybrid scalper starting (Option B)")
    log.info("=" * 60)

    client = MT5Client(CONFIG)
    if not client.connect():
        log.error("Cannot connect to MetaTrader 5. Is the terminal installed and running?")
        sys.exit(1)

    trader = TradeManager(CONFIG, client)
    if not run_startup_test(client, CONFIG):
        log.error("Startup test did not fully pass — check MT5 Algo Trading is ON.")
        if CONFIG.get("startup_test_required", False):
            cleanup_test_positions(CONFIG)
            client.shutdown()
            sys.exit(1)
        log.warning("Continuing to live trading anyway (startup_test_required=False).")

    risk = RiskManager(CONFIG, client.account_equity())
    _try_duty_email(risk)

    last_bar_time = None
    rate_failures = 0
    last_status_log = 0.0
    try:
        while True:
            df = client.get_rates()
            if df is None or len(df) < CONFIG["ema_slow"] + 10:
                rate_failures += 1
                write_heartbeat(CONFIG, rates_ok=False)
                # ~1 minute without data usually means the terminal restarted
                # and our IPC pipe is dead — reconnect instead of waiting.
                if rate_failures >= 3:
                    log.warning("No market data for %d polls — reconnecting to MT5.",
                                rate_failures)
                    client.reconnect()
                    rate_failures = 0
                time.sleep(CONFIG["poll_seconds"])
                continue
            rate_failures = 0

            # The last row is the still-forming candle — work with closed ones.
            closed = df.iloc[:-1].reset_index(drop=True)
            newest_closed_time = closed["time"].iloc[-1]
            is_new_bar = newest_closed_time != last_bar_time

            equity = client.account_equity()
            write_heartbeat(CONFIG, equity=equity, last_bar=newest_closed_time)

            now = time.time()
            if now - last_status_log >= 60:
                log.info("Data OK | %s | equity %.2f | last bar %s",
                         CONFIG["symbol"], equity, newest_closed_time)
                last_status_log = now
            positions = client.positions()
            day_profits = client.today_deal_profits()
            mode = risk.update(equity, bool(positions), day_profits,
                               balance=client.account_balance())
            if risk.state.get("_new_day"):
                _try_duty_email(risk)
            if risk.state.get("_target_just_hit"):
                _try_target_email(risk, equity)

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
                if allowed and CONFIG.get("basket_enabled"):
                    # Basket mode: only add when every open leg is risk-free.
                    if positions and not trader.positions_risk_free(positions):
                        allowed = False
                        block_reason = ("managing open basket — waiting until it "
                                        "is risk-free before adding more")
                if not allowed:
                    log.info("Bar %s | equity %.2f | mode %s | no entry: %s",
                             newest_closed_time, equity, mode, block_reason)
                else:
                    # Top-down pre-trade routine: previous day -> H4 -> H1.
                    bias, bias_detail = topdown.htf_bias(client, CONFIG)
                    signal, explanation = strategy.evaluate(analyzed, CONFIG,
                                                            htf_bias=bias)
                    if signal is None:
                        log.info("Bar %s | equity %.2f | mode %s | [%s] %s",
                                 newest_closed_time, equity, mode,
                                 bias_detail, explanation)
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
                            opened = False
                            if CONFIG.get("basket_enabled"):
                                opened = trader.open_basket(
                                    signal.direction, volume,
                                    signal.stop_loss, signal.entry_hint,
                                    signal.reason,
                                ) > 0
                            elif trader.open_trade(
                                signal.direction, volume,
                                signal.stop_loss, signal.take_profit,
                                signal.reason,
                            ):
                                opened = True
                            if opened:
                                risk.on_trade_opened()

            time.sleep(CONFIG["poll_seconds"])

    except KeyboardInterrupt:
        log.info("Stopped by user. Open positions remain protected by SL/TP.")
    finally:
        risk.save()
        client.shutdown()


if __name__ == "__main__":
    main()
