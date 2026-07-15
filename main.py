"""
Gold Genious — XAUUSD M5 auto-trading bot, entry point.

Run:  python main.py
Stop: Ctrl+C (open positions keep their SL/TP on the broker side).
"""

import json
import logging
import os
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
import stop_ladder
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


def _ladder_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        CONFIG.get("ladder_state_file", "ladder_state.json"))


def _load_ladder_state() -> dict:
    path = _ladder_path()
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_ladder_state(state: dict):
    try:
        with open(_ladder_path(), "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except OSError:
        pass


def _run_stop_ladder(log, client, trader, risk, analyzed, equity, mode,
                     newest_closed_time, positions):
    """V20 cascade: one Sell/Buy Stop at a time, 10-pip TP, until MA / prev swing."""
    state = _load_ladder_state()
    pendings = client.pending_orders()

    # Target hit -> clear any leftover pending stops.
    if mode == MODE_TARGET_DONE:
        if pendings:
            trader.cancel_pending("daily target done")
        return

    # Remember a closed winner so the next step continues the ladder.
    if state.get("watching_ticket") and not positions:
        last_profits = client.today_deal_profits()
        won = bool(last_profits and last_profits[-1] > 0)
        if won and state.get("planned_tp") is not None:
            state["last_tp"] = state["planned_tp"]
            state["last_direction"] = state.get("planned_direction")
            log.info("Ladder step WON — next stop continues from TP %.3f",
                     state["last_tp"])
        else:
            # SL / scratch — restart from live price on the next clear move.
            state.pop("last_tp", None)
            log.info("Ladder step closed without TP — restarting fresh.")
        state.pop("watching_ticket", None)
        state.pop("planned_tp", None)
        state.pop("planned_direction", None)
        _save_ladder_state(state)

    if positions:
        # Exactly one live trade — wait for its fixed TP/SL. Track ticket.
        ticket = int(positions[0].ticket)
        if state.get("watching_ticket") != ticket:
            state["watching_ticket"] = ticket
            _save_ladder_state(state)
        if pendings:
            trader.cancel_pending("position already open")
        return

    allowed, block_reason = risk.can_open_trade(0)
    if not allowed:
        if pendings:
            trader.cancel_pending("risk paused")
        log.info("%s | WAIT: %s (eq %.2f, %s)",
                 newest_closed_time.strftime("%H:%M"), block_reason,
                 equity, mode)
        return

    tick = client.get_tick()
    if tick is None:
        return
    direction_hint = stop_ladder.short_direction(analyzed, CONFIG)
    market = tick.bid if direction_hint == "SELL" else tick.ask
    if direction_hint is None:
        market = (tick.bid + tick.ask) / 2.0

    # Direction flip -> drop stale pending + break the chain.
    if (pendings and direction_hint
            and state.get("planned_direction")
            and state["planned_direction"] != direction_hint):
        trader.cancel_pending("direction flipped")
        pendings = []
        state.pop("last_tp", None)
        state.pop("planned_tp", None)
        state.pop("planned_direction", None)
        _save_ladder_state(state)

    if direction_hint and stop_ladder.ma_touched(analyzed, direction_hint):
        if pendings:
            trader.cancel_pending("MA touched — ladder done")
        which = "lower" if direction_hint == "SELL" else "upper"
        log.info("%s | WAIT: %s MA touched — stop-ladder paused for this move "
                 "(eq %.2f)",
                 newest_closed_time.strftime("%H:%M"), which, equity)
        return

    if pendings:
        # Already have the one allowed pending stop — leave it alone.
        log.info("%s | WAIT: pending %s stop working (eq %.2f, %s)",
                 newest_closed_time.strftime("%H:%M"),
                 state.get("planned_direction", "?"), equity, mode)
        return

    plan, explanation = stop_ladder.plan_next(
        analyzed, CONFIG,
        market_price=market,
        last_tp=state.get("last_tp"),
        last_direction=state.get("last_direction"),
    )
    if plan is None:
        log.info("%s | WAIT: %s (eq %.2f, %s)",
                 newest_closed_time.strftime("%H:%M"), explanation,
                 equity, mode)
        return

    sl_distance = abs(plan.entry - plan.stop_loss)
    risk_pct = risk.current_risk_pct()
    volume = risk.lot_size(equity, sl_distance, client.symbol_info(), risk_pct)
    if volume <= 0:
        log.warning("Ladder plan ready but lot size is 0 — skipping.")
        return

    log.info("LADDER: %s | %s | risk %.2f%% | %.2f lots%s",
             plan.direction, plan.reason, risk_pct, volume,
             f" | {plan.terminal_note}" if plan.terminal_note else "")
    ticket = trader.place_stop_order(
        plan.direction, volume, plan.entry, plan.stop_loss, plan.take_profit,
        comment="GG ladder",
    )
    if ticket:
        state["planned_tp"] = plan.take_profit
        state["planned_direction"] = plan.direction
        state["planned_entry"] = plan.entry
        _save_ladder_state(state)
        risk.on_trade_opened()


def main():
    log = setup_logging()
    mode_name = CONFIG.get("entry_mode", "hybrid")
    log.info("=" * 60)
    log.info("GOLD GENIOUS — XAUUSD M5 starting (%s)", mode_name)
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
                if rate_failures >= 3:
                    log.warning("No market data for %d polls — reconnecting to MT5.",
                                rate_failures)
                    client.reconnect()
                    rate_failures = 0
                time.sleep(CONFIG["poll_seconds"])
                continue
            rate_failures = 0

            closed = df.iloc[:-1].reset_index(drop=True)
            newest_closed_time = closed["time"].iloc[-1]
            is_new_bar = newest_closed_time != last_bar_time

            equity = client.account_equity()
            write_heartbeat(CONFIG, equity=equity, last_bar=newest_closed_time)

            now = time.time()
            if now - last_status_log >= 300:
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

            if mode == MODE_TARGET_DONE and positions:
                trader.close_all("daily 5% target reached")
                trader.cancel_pending("daily target done")
                positions = []

            if is_new_bar:
                last_bar_time = newest_closed_time
                risk.on_new_bar()

                analyzed = add_indicators(closed, CONFIG)
                current_atr = float(analyzed["atr"].iloc[-1])

                # Hybrid/structure still use the protection ladder. Stop-ladder
                # trades already have a fixed 10-pip TP — leave them alone.
                if positions and CONFIG.get("entry_mode") != "stop_ladder":
                    structure_now = ms.analyze(analyzed, CONFIG["swing_lookback"])
                    trader.manage_positions(current_atr, structure_now)

                if (newest_closed_time.dayofweek == 4
                        and newest_closed_time.hour >= CONFIG["friday_close_hour"]):
                    if positions:
                        trader.close_all("weekend protection")
                        positions = []
                    trader.cancel_pending("weekend protection")
                    log.info("Bar %s | weekend protection active — flat until Monday.",
                             newest_closed_time)
                    time.sleep(CONFIG["poll_seconds"])
                    continue

                if CONFIG.get("entry_mode") == "stop_ladder":
                    _run_stop_ladder(log, client, trader, risk, analyzed,
                                     equity, mode, newest_closed_time, positions)
                else:
                    allowed, block_reason = risk.can_open_trade(len(positions))
                    pause_break = False
                    if not allowed and risk.in_loss_pause():
                        ok_otherwise, _ = risk.can_open_trade(
                            len(positions), ignore_pause=True)
                        if ok_otherwise:
                            pause_break = True
                            allowed = True
                    if allowed and CONFIG.get("basket_enabled"):
                        if positions and not trader.positions_risk_free(positions):
                            allowed = False
                            pause_break = False
                            block_reason = ("managing open basket — waiting until it "
                                            "is risk-free before adding more")
                    if not allowed:
                        log.info("%s | WAIT: %s (eq %.2f, %s)",
                                 newest_closed_time.strftime("%H:%M"), block_reason,
                                 equity, mode)
                    else:
                        bias, bias_detail = topdown.htf_bias(client, CONFIG)
                        signal, explanation = strategy.evaluate(
                            analyzed, CONFIG, htf_bias=bias)
                        if signal is None:
                            if pause_break:
                                log.info("%s | WAIT: %s (watching for a BOS/impulse "
                                         "to re-enter early) (eq %.2f, %s)",
                                         newest_closed_time.strftime("%H:%M"),
                                         block_reason, equity, mode)
                            else:
                                log.info("%s | WAIT: %s [HTF %s] (eq %.2f, %s)",
                                         newest_closed_time.strftime("%H:%M"),
                                         explanation, bias_detail, equity, mode)
                        elif pause_break and not risk.pause_override_ok(
                                signal.confidence, signal.reason):
                            log.info("%s | WAIT: %s — signal found (%s, conf %.0f) "
                                     "but not strong enough to break the cooldown",
                                     newest_closed_time.strftime("%H:%M"), block_reason,
                                     signal.reason, signal.confidence)
                        else:
                            if pause_break:
                                risk.break_pause(signal.reason)
                            sl_distance = abs(signal.entry_hint - signal.stop_loss)
                            risk_pct = risk.current_risk_pct(signal.confidence)
                            if pause_break:
                                risk_pct *= CONFIG.get("pause_override_risk_frac", 0.5)
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
