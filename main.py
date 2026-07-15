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


def _place_plans(log, trader, risk, client, equity, plans, legs, note):
    """Place a list of pending stop plans. Returns how many were accepted."""
    risk_pct = risk.current_risk_pct()
    leg_risk = risk_pct / max(legs, 1)
    placed = 0
    for plan in plans:
        sl_distance = abs(plan.entry - plan.stop_loss)
        volume = risk.lot_size(equity, sl_distance, client.symbol_info(),
                               leg_risk)
        if volume <= 0:
            continue
        ticket = trader.place_stop_order(
            plan.direction, volume, plan.entry, plan.stop_loss,
            plan.take_profit, comment="GG grid",
        )
        if ticket:
            placed += 1
    if placed:
        log.info("GRID PLACED: %d stops | %s | leg risk %.2f%% | eq %.2f",
                 placed, note, leg_risk, equity)
        risk.on_trade_opened()
    return placed


def _refresh_reversal_guard(log, trader, risk, client, analyzed, equity,
                            state, active, tick):
    """Best-security opposite Stop (structure + pip dump-cap).

    Armed only after the active side has booked at least one win — so we do
    not sell into the opening noise of a brand-new breakout.
    """
    if not CONFIG.get("ladder_reversal_guard", True):
        return
    need_wins = int(CONFIG.get("ladder_guard_after_wins", 1))
    if int(state.get("side_wins", 0)) < need_wins:
        return
    extreme = state.get("move_extreme")
    if extreme is None or active not in ("BUY", "SELL"):
        return
    guard = stop_ladder.build_guard_plan(active, float(extreme), analyzed, CONFIG)
    pip = stop_ladder.pip_size(CONFIG)
    if guard.direction == "SELL" and guard.entry >= tick.bid - pip * 0.5:
        return
    if guard.direction == "BUY" and guard.entry <= tick.ask + pip * 0.5:
        return

    prev = state.get("guard_price")
    if prev is not None and abs(float(prev) - guard.entry) < pip * 2:
        buy_n, sell_n = trader.pending_side_counts()
        opp_n = sell_n if guard.direction == "SELL" else buy_n
        if opp_n >= 1:
            return

    opp = guard.direction
    trader.cancel_pending("refresh secure guard", direction=opp)
    sl_distance = abs(guard.entry - guard.stop_loss)
    volume = risk.lot_size(equity, sl_distance, client.symbol_info(),
                           risk.current_risk_pct())
    if volume <= 0:
        return
    ticket = trader.place_stop_order(
        guard.direction, volume, guard.entry, guard.stop_loss,
        guard.take_profit, comment="GG guard",
    )
    if ticket:
        log.info("SECURE GUARD armed: %s @ %.3f (extreme %.3f | wins %d)",
                 guard.direction, guard.entry, float(extreme),
                 int(state.get("side_wins", 0)))
        state["guard_ticket"] = ticket
        state["guard_price"] = guard.entry
        _save_ladder_state(state)


def _run_stop_ladder(log, client, trader, risk, analyzed, equity, mode,
                     newest_closed_time, positions):
    """V23 best-security dual grid:
      - Buy Stops above + Sell Stops below
      - After first win: one SECURE GUARD (structure break OR 50-pip dump cap)
      - On pullback: cancel stale same-side stops so tops don't fill into a dump
      - On reverse: flip and cascade the new side
    """
    state = _load_ladder_state()
    pendings = client.pending_orders()
    dual = CONFIG.get("ladder_dual_sides", True)
    legs = max(1, int(CONFIG.get("ladder_legs", 5)))
    last_bar = analyzed.iloc[-1]

    if mode == MODE_TARGET_DONE:
        if pendings:
            trader.cancel_pending("daily target done")
        state.clear()
        _save_ladder_state(state)
        return

    # A closed ladder trade — continue on the winning side, or reset to dual.
    if state.get("watching_ticket") and not positions:
        last_profits = client.today_deal_profits()
        won = bool(last_profits and last_profits[-1] > 0)
        closed_was_guard = state.get("guard_watching")
        if won and state.get("planned_tp") is not None and not closed_was_guard:
            state["last_tp"] = state["planned_tp"]
            state["active_side"] = state.get("planned_direction")
            state["last_direction"] = state.get("planned_direction")
            state["side_wins"] = int(state.get("side_wins", 0)) + 1
            log.info("Ladder step WON (#%d) — continue %s side from TP %.3f",
                     state["side_wins"], state.get("active_side"),
                     state["last_tp"])
        elif closed_was_guard:
            side = state.get("planned_direction")
            state["active_side"] = side
            state["last_direction"] = side
            state["side_wins"] = 1 if won else 0
            state.pop("last_tp", None)
            state.pop("move_extreme", None)
            log.info("SECURE GUARD filled — flipping to %s cascade.", side)
        else:
            state.pop("last_tp", None)
            state.pop("active_side", None)
            state.pop("last_direction", None)
            state.pop("move_extreme", None)
            state["side_wins"] = 0
            log.info("Ladder step closed without TP — re-arm dual grid.")
        state.pop("watching_ticket", None)
        state.pop("planned_tp", None)
        state.pop("planned_direction", None)
        state.pop("guard_watching", None)
        _save_ladder_state(state)

    tick = client.get_tick()
    if tick is None:
        return

    if positions:
        pos = positions[0]
        ticket = int(pos.ticket)
        side = "BUY" if int(pos.type) == 0 else "SELL"
        opposite = "SELL" if side == "BUY" else "BUY"
        # Speculative opposite grid goes away — but we re-arm a REVERSAL GUARD.
        trader.cancel_pending(f"{side} filled — clear {opposite} grid",
                              direction=opposite)
        same = trader.cancel_pending("position open — park same side",
                                     direction=side)
        if same:
            log.info("Parked %d same-side pending(s) while trade is open.", same)
        state["watching_ticket"] = ticket
        state["active_side"] = side
        state["planned_direction"] = side
        if pos.tp:
            state["planned_tp"] = float(pos.tp)
        # Was this the guard that filled?
        if state.get("guard_ticket") == ticket or (
                state.get("guard_price")
                and abs(float(pos.price_open) - float(state["guard_price"])) < 0.5):
            state["guard_watching"] = True
        stop_ladder.update_move_extreme(
            state, side, bid=tick.bid, ask=tick.ask,
            bar_high=float(last_bar["high"]), bar_low=float(last_bar["low"]))
        _refresh_reversal_guard(log, trader, risk, client, analyzed, equity,
                                state, side, tick)
        _save_ladder_state(state)
        return

    allowed, block_reason = risk.can_open_trade(0)
    if not allowed:
        if pendings:
            trader.cancel_pending("risk paused")
        log.info("%s | WAIT: %s (eq %.2f, %s)",
                 newest_closed_time.strftime("%H:%M"), block_reason,
                 equity, mode)
        return

    active = state.get("active_side")

    # Track peak/trough while a side is live.
    if active:
        extreme = stop_ladder.update_move_extreme(
            state, active, bid=tick.bid, ask=tick.ask,
            bar_high=float(last_bar["high"]), bar_low=float(last_bar["low"]))
        _save_ladder_state(state)
        price = tick.bid if active == "BUY" else tick.ask

        # Defensive: pullback from the high → cancel unfilled same-side stops
        # (the "uppermost Buy Stop that never filled" problem).
        if stop_ladder.stale_same_side_cancel(active, extreme, price, CONFIG):
            killed = trader.cancel_pending(
                "stale same-side cancel on pullback", direction=active)
            if killed:
                log.info("STALE %s stops cancelled after %.0f-pip pullback "
                         "from extreme %.3f — staying defensive.",
                         active,
                         stop_ladder.pullback_from_extreme(active, extreme, price)
                         / stop_ladder.pip_size(CONFIG),
                         float(extreme))

        if stop_ladder.reversal_hit(active, extreme, price, CONFIG, analyzed):
            trader.cancel_pending(f"REVERSAL — cancel {active} stops",
                                  direction=active)
            flip = "SELL" if active == "BUY" else "BUY"
            log.warning(
                "SECURE REVERSAL: %s extreme %.3f → price %.3f — "
                "flipping to %s grid.",
                active, float(extreme), price, flip)
            state["active_side"] = flip
            state["side_wins"] = 0
            state.pop("last_tp", None)
            state.pop("move_extreme", None)
            state.pop("guard_ticket", None)
            state.pop("guard_price", None)
            _save_ladder_state(state)
            market = tick.bid if flip == "SELL" else tick.ask
            plans = stop_ladder.build_side_plans(flip, market, analyzed, CONFIG)
            if plans:
                _place_plans(log, trader, risk, client, equity, plans, legs,
                             f"reversal {flip} grid")
            return
        _refresh_reversal_guard(log, trader, risk, client, analyzed, equity,
                                state, active, tick)

    if active and stop_ladder.ma_touched(analyzed, active):
        trader.cancel_pending("MA touched — ladder done")
        which = "lower" if active == "SELL" else "upper"
        log.info("%s | WAIT: %s MA touched — dual grid paused (eq %.2f)",
                 newest_closed_time.strftime("%H:%M"), which, equity)
        state.pop("active_side", None)
        state.pop("last_tp", None)
        state.pop("move_extreme", None)
        _save_ladder_state(state)
        return

    pendings = client.pending_orders()
    buy_n, sell_n = trader.pending_side_counts()

    # ----- Active side continuation (after a fill) -----
    if active:
        # Drop any leftover speculative opposite (guard is refreshed above).
        pendings = client.pending_orders()
        # Count only SAME-side pendings as "continuation working".
        same_side = sell_n if active == "SELL" else buy_n
        if same_side > 0:
            log.info("%s | WAIT: %s side pending working (%d) + guard "
                     "(eq %.2f, %s)",
                     newest_closed_time.strftime("%H:%M"), active,
                     same_side, equity, mode)
            return

        market = tick.bid if active == "SELL" else tick.ask
        plan, explanation = stop_ladder.plan_next(
            analyzed, CONFIG,
            market_price=market,
            last_tp=state.get("last_tp"),
            last_direction=active,
            force_direction=active,
        )
        if plan is None:
            log.info("%s | WAIT: %s — re-arm dual grid (eq %.2f)",
                     newest_closed_time.strftime("%H:%M"), explanation, equity)
            state.pop("active_side", None)
            state.pop("last_tp", None)
            state.pop("move_extreme", None)
            _save_ladder_state(state)
            active = None
        else:
            sl_distance = abs(plan.entry - plan.stop_loss)
            risk_pct = risk.current_risk_pct()
            volume = risk.lot_size(equity, sl_distance, client.symbol_info(),
                                   risk_pct)
            if volume <= 0:
                return
            log.info("LADDER CONTINUE: %s | %s | %.2f lots",
                     plan.direction, plan.reason, volume)
            ticket = trader.place_stop_order(
                plan.direction, volume, plan.entry, plan.stop_loss,
                plan.take_profit, comment="GG ladder",
            )
            if ticket:
                state["planned_tp"] = plan.take_profit
                state["planned_direction"] = plan.direction
                _save_ladder_state(state)
                risk.on_trade_opened()
            return

    # ----- Dual grid arming (both sides) -----
    if not dual:
        direction_hint = stop_ladder.short_direction(analyzed, CONFIG)
        market = tick.bid if direction_hint == "SELL" else tick.ask
        if direction_hint is None:
            market = (tick.bid + tick.ask) / 2.0
        if pendings:
            log.info("%s | WAIT: pending working (eq %.2f, %s)",
                     newest_closed_time.strftime("%H:%M"), equity, mode)
            return
        plan, explanation = stop_ladder.plan_next(
            analyzed, CONFIG, market_price=market,
            last_tp=state.get("last_tp"),
            last_direction=state.get("last_direction"),
        )
        if plan is None:
            log.info("%s | WAIT: %s (eq %.2f, %s)",
                     newest_closed_time.strftime("%H:%M"), explanation,
                     equity, mode)
            return
        plans = [plan]
        note = plan.reason
    else:
        if buy_n > 0 and sell_n > 0:
            log.info("%s | WAIT: dual grid armed (%d BUY + %d SELL stops) "
                     "(eq %.2f, %s)",
                     newest_closed_time.strftime("%H:%M"), buy_n, sell_n,
                     equity, mode)
            return
        if pendings:
            trader.cancel_pending("rebuild dual grid")

        plans, note = stop_ladder.plan_dual_grid(
            analyzed, CONFIG, bid=tick.bid, ask=tick.ask)
        if not plans:
            log.info("%s | WAIT: %s (eq %.2f, %s)",
                     newest_closed_time.strftime("%H:%M"), note,
                     equity, mode)
            return

    placed = _place_plans(log, trader, risk, client, equity, plans, legs, note)
    if placed:
        state["grid_armed"] = True
        state.pop("active_side", None)
        state.pop("move_extreme", None)
        _save_ladder_state(state)
    else:
        log.warning("Dual grid planned but 0 orders accepted by broker.")


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
