"""
Trade execution and open-position management:
- BASKET entries: one signal opens several small positions with scaled
  take-profits (TP1 hit -> everything to breakeven) plus a runner that
  trails behind market structure to ride the whole move,
- market orders with SL/TP (never without),
- close-all (used when the daily target is hit),
- staged protection ladder: half-risk -> breakeven -> profit lock -> trailing
  behind market structure and ATR,
- time stop for trades that go nowhere.
"""

import json
import logging
import os

import MetaTrader5 as mt5

from mt5_orders import round_price, send_deal, send_pending

log = logging.getLogger("bot.trade")


def split_basket_volumes(total: float, legs: int, vol_min: float,
                         vol_step: float) -> list[float]:
    """Split a total volume into up to `legs` equal parts, respecting the
    broker's minimum and step. Remainder goes to the LAST leg (the runner).
    Returns [] when the total cannot even fund one minimum-size position."""
    if total < vol_min or legs < 1:
        return []
    step = vol_step or 0.01
    n = min(legs, int(round(total / vol_min, 8)))
    if n < 1:
        return []
    per = max(vol_min, int((total / n) / step) * step)
    volumes = [round(per, 8)] * n
    used = per * n
    leftover = int(round((total - used) / step)) * step
    if leftover > 0:
        volumes[-1] = round(volumes[-1] + leftover, 8)
    return volumes


def basket_take_profits(entry: float, sl: float, is_buy: bool,
                        legs: int, config: dict) -> list[float]:
    """TP ladder for a basket: e.g. 1R, 1.5R, 2R, 3R … and a far TP for the
    last leg (the runner — its real exit is the trailing stop)."""
    risk_unit = abs(entry - sl)
    sign = 1 if is_buy else -1
    levels = list(config.get("basket_tp_r", [1.0, 1.5, 2.0, 3.0]))
    runner_r = config.get("basket_runner_tp_r", 10.0)

    tps = []
    for i in range(legs):
        if i == legs - 1:
            r = runner_r                       # runner: far TP + trailing stop
        elif i < len(levels):
            r = levels[i]
        else:
            r = levels[-1]
        tps.append(entry + sign * r * risk_unit)
    return tps


def compute_protective_sl(is_buy: bool, entry: float, sl: float, tp: float,
                          price: float, atr: float, structure, config: dict,
                          risk_unit: float | None = None):
    """The staged stop-loss ladder. Returns the new SL, or None if the
    current stop should stay where it is. Stops only ever move in the
    trade's favor — never backwards.

    R = the trade's initial risk. For basket legs it is stored at open time
    (`risk_unit`); otherwise it is recovered from the TP distance."""
    sign = 1 if is_buy else -1

    if risk_unit is None:
        risk_unit = abs(tp - entry) / config["min_reward_risk"] if tp else atr
    if risk_unit <= 0 or atr <= 0:
        return None

    profit = (price - entry) * sign
    profit_r = profit / risk_unit

    candidates = []

    # Stage 1: +0.5R -> halve the remaining risk.
    if profit_r >= config["protect_rr"]:
        candidates.append(entry - sign * 0.5 * risk_unit)

    # Stage 2: +1R -> breakeven plus a buffer (spread can't turn it red).
    if profit_r >= config["breakeven_rr"]:
        candidates.append(entry + sign * config["breakeven_buffer_atr"] * atr)

    # Stage 3: +1.5R -> lock in real profit.
    if profit_r >= config["lock_rr"]:
        candidates.append(entry + sign * config["lock_keep_r"] * risk_unit)

    # Stage 4: trailing — take the TIGHTER of the ATR trail and the
    # structure trail (behind the last swing low/high). In a healthy trend
    # price should never revisit the last swing, so that's the natural line
    # in the sand; ATR keeps us honest when structure is far away.
    if profit_r > config["breakeven_rr"]:
        trail = price - sign * config["trail_atr_mult"] * atr
        swing = None
        if structure is not None:
            swing = structure.last_swing_low if is_buy else structure.last_swing_high
        if swing is not None:
            struct_trail = swing.price - sign * config["trail_struct_buffer_atr"] * atr
            trail = max(trail, struct_trail) if is_buy else min(trail, struct_trail)
        # ...but never suffocate the trade: keep a minimum gap to price.
        max_tight = price - sign * config["min_trail_gap_atr"] * atr
        trail = min(trail, max_tight) if is_buy else max(trail, max_tight)
        candidates.append(trail)

    if not candidates:
        return None

    best = max(candidates) if is_buy else min(candidates)

    # Only ever tighten, never loosen.
    if sl and ((is_buy and best <= sl + 1e-9) or (not is_buy and best >= sl - 1e-9)):
        return None
    return best


def profit_in_r(is_buy: bool, entry: float, tp: float, price: float,
                min_reward_risk: float, risk_unit: float | None = None) -> float:
    if risk_unit is None:
        risk_unit = abs(tp - entry) / min_reward_risk if tp else 0.0
    if risk_unit <= 0:
        return 0.0
    profit = (price - entry) if is_buy else (entry - price)
    return profit / risk_unit


class TradeManager:
    def __init__(self, config: dict, client):
        self.config = config
        self.client = client
        self.symbol = config["symbol"]
        self.basket_file = config.get("basket_state_file", "basket_state.json")

    # ----- basket state (ticket -> initial risk / runner flag) -----

    def _load_basket_state(self) -> dict:
        if os.path.exists(self.basket_file):
            try:
                with open(self.basket_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _save_basket_state(self, state: dict):
        try:
            with open(self.basket_file, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        except OSError:
            pass

    # ----- opening -----

    def open_basket(self, direction: str, total_volume: float, sl: float,
                    entry_hint: float, comment: str) -> int:
        """One signal -> several small positions ('jab jab structure bane,
        chote chote trades'):

        - legs share the same SL,
        - TPs laddered at 1R / 1.5R / 2R / 3R,
        - the LAST leg is the runner: far TP, exits on the trailing stop,
        - when price reaches TP1 (+1R) every leg's ladder moves its stop to
          breakeven — so after the first take-profit the whole basket is
          risk-free.

        Returns the number of positions opened."""
        info = self.client.symbol_info()
        if info is None:
            return 0
        legs_wanted = max(1, int(self.config.get("basket_trades", 5)))
        volumes = split_basket_volumes(total_volume, legs_wanted,
                                       info.volume_min or 0.01,
                                       info.volume_step or 0.01)
        if not volumes:
            log.warning("Basket skipped: total volume %.2f can't fund one "
                        "minimum position.", total_volume)
            return 0

        is_buy = direction == "BUY"
        tps = basket_take_profits(entry_hint, sl, is_buy, len(volumes), self.config)
        risk_unit = abs(entry_hint - sl)

        state = self._load_basket_state()
        opened = 0
        for i, (vol, tp) in enumerate(zip(volumes, tps)):
            runner = i == len(volumes) - 1 and len(volumes) > 1
            label = "runner" if runner else f"TP{i + 1}"
            # Comment stays short and plain — MT5's API rejects the whole
            # order over a fancy comment. The full reason is in the log.
            ticket = self._open_market(direction, vol, sl, tp, f"GG {label}")
            if ticket:
                opened += 1
                state[str(ticket)] = {"risk_unit": risk_unit, "runner": runner}
            else:
                log.warning("Basket leg %d/%d (%s) failed to open.",
                            i + 1, len(volumes), label)
        self._save_basket_state(state)
        if opened:
            log.info("BASKET OPENED: %d/%d %s positions | shared SL %.3f | "
                     "TP ladder %s + runner trail",
                     opened, len(volumes), direction, sl,
                     [round(t, 2) for t in tps[:-1]])
        return opened

    def open_trade(self, direction: str, volume: float, sl: float, tp: float,
                   comment: str) -> bool:
        return self._open_market(direction, volume, sl, tp, comment) is not None

    def place_stop_order(self, direction: str, volume: float, entry: float,
                         sl: float, tp: float, comment: str = "GG ladder") -> int | None:
        """Place a single Buy Stop or Sell Stop (TRADE_ACTION_PENDING).
        Exactly one pending at a time — callers must cancel first if needed."""
        if not sl or not tp or sl <= 0 or tp <= 0:
            log.error("REFUSED pending: SL/TP missing (sl=%s, tp=%s).", sl, tp)
            return None
        if direction == "BUY" and not (sl < entry < tp):
            log.error("REFUSED Buy Stop: need SL < entry < TP (%.3f / %.3f / %.3f).",
                      sl, entry, tp)
            return None
        if direction == "SELL" and not (tp < entry < sl):
            log.error("REFUSED Sell Stop: need TP < entry < SL (%.3f / %.3f / %.3f).",
                      tp, entry, sl)
            return None

        tick = self.client.get_tick()
        if tick is None:
            log.error("No tick data, cannot place pending stop.")
            return None

        info = self.client.symbol_info()
        point = info.point if info and info.point > 0 else 0.01
        stops_level = (info.trade_stops_level or 0) * point if info else 0.0
        min_gap = max(stops_level, point * 10)

        if direction == "BUY":
            order_type = mt5.ORDER_TYPE_BUY_STOP
            if entry <= tick.ask + min_gap:
                log.warning("REFUSED Buy Stop %.3f — must be > ask %.3f + gap.",
                            entry, tick.ask)
                return None
        else:
            order_type = mt5.ORDER_TYPE_SELL_STOP
            if entry >= tick.bid - min_gap:
                log.warning("REFUSED Sell Stop %.3f — must be < bid %.3f - gap.",
                            entry, tick.bid)
                return None

        spread = tick.ask - tick.bid
        spread_points = spread / point
        if spread_points > self.config["max_spread_points"]:
            log.warning("REFUSED pending: spread %.0f points > max %d.",
                        spread_points, self.config["max_spread_points"])
            return None

        request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": self.symbol,
            "volume": volume,
            "type": order_type,
            "price": round_price(self.symbol, entry),
            "sl": round_price(self.symbol, sl),
            "tp": round_price(self.symbol, tp),
            "deviation": self.config["deviation_points"],
            "magic": self.config["magic_number"],
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
        }
        result = send_pending(request, self.symbol)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            log.info("PENDING %s STOP %.2f lots %s @ %.3f | SL %.3f | TP %.3f | %s",
                     direction, volume, self.symbol, entry, sl, tp, comment)
            return int(result.order)
        if result:
            log.error("Pending failed: retcode=%s comment=%s",
                      result.retcode, result.comment)
        else:
            log.error("Pending failed: %s", mt5.last_error())
        return None

    def cancel_pending(self, reason: str = "cancel") -> int:
        """Cancel every pending order belonging to this bot. Returns count."""
        cancelled = 0
        for order in self.client.pending_orders():
            request = {
                "action": mt5.TRADE_ACTION_REMOVE,
                "order": order.ticket,
                "comment": reason[:31],
            }
            result = mt5.order_send(request)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                cancelled += 1
                log.info("CANCELLED pending %s (%s)", order.ticket, reason)
            else:
                log.warning("Could not cancel pending %s: %s",
                            order.ticket,
                            result.comment if result else mt5.last_error())
        return cancelled

    def _open_market(self, direction: str, volume: float, sl: float, tp: float,
                     comment: str) -> int | None:
        # HARD RULE: no trade ever leaves without both a stop-loss and a
        # take-profit. If either is missing the order is refused outright.
        if not sl or not tp or sl <= 0 or tp <= 0:
            log.error("REFUSED order: SL/TP missing (sl=%s, tp=%s).", sl, tp)
            return None
        if direction == "BUY" and not (sl < tp):
            log.error("REFUSED BUY: SL %.3f must be below TP %.3f.", sl, tp)
            return None
        if direction == "SELL" and not (sl > tp):
            log.error("REFUSED SELL: SL %.3f must be above TP %.3f.", sl, tp)
            return None

        tick = self.client.get_tick()
        if tick is None:
            log.error("No tick data, cannot open trade.")
            return None

        if direction == "BUY":
            order_type = mt5.ORDER_TYPE_BUY
            price = tick.ask
        else:
            order_type = mt5.ORDER_TYPE_SELL
            price = tick.bid

        # Spread guard, two layers:
        #  1) absolute cap — catches true blowouts (news, rollover),
        #  2) relative cap — the spread must stay a small fraction of the
        #     stop distance, so wide-spread brokers (trial/cent servers)
        #     can still trade when the ATR stop is proportionally big.
        info = self.client.symbol_info()
        point = info.point if info and info.point > 0 else 0.01
        spread = tick.ask - tick.bid
        spread_points = spread / point
        if spread_points > self.config["max_spread_points"]:
            log.warning("REFUSED order: spread %.0f points > absolute max %d — "
                        "waiting for normal conditions.",
                        spread_points, self.config["max_spread_points"])
            return None
        sl_dist = abs(price - sl)
        max_frac = self.config.get("max_spread_sl_frac", 0.30)
        if sl_dist > 0 and spread > max_frac * sl_dist:
            log.warning("REFUSED order: spread %.2f is %.0f%% of the %.2f stop "
                        "(max %.0f%%) — trade math too expensive, waiting.",
                        spread, spread / sl_dist * 100, sl_dist, max_frac * 100)
            return None

        # Margin guard: never send an order the account cannot comfortably hold.
        margin_needed = mt5.order_calc_margin(order_type, self.symbol, volume, price)
        account = mt5.account_info()
        if margin_needed and account and margin_needed > account.margin_free * 0.9:
            log.error("REFUSED order: needs %.2f margin, only %.2f free.",
                      margin_needed, account.margin_free)
            return None

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": volume,
            "type": order_type,
            "price": price,
            "sl": round_price(self.symbol, sl),
            "tp": round_price(self.symbol, tp),
            "deviation": self.config["deviation_points"],
            "magic": self.config["magic_number"],
            "comment": comment[:31],
            "type_time": mt5.ORDER_TIME_GTC,
        }

        result = send_deal(request, self.symbol)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            log.info(
                "OPENED %s %.2f lots %s @ %.3f | SL %.3f | TP %.3f | %s",
                direction, volume, self.symbol, result.price, sl, tp, comment,
            )
            self._verify_sl_tp(sl, tp)
            return int(result.order)
        if result:
            log.error("Order failed: retcode=%s comment=%s", result.retcode, result.comment)
        else:
            log.error("Order failed: %s", mt5.last_error())
        return None

    @staticmethod
    def positions_risk_free(positions) -> bool:
        """True when every open position's stop already protects the entry
        (breakeven or better) — the basket carries zero risk, so a fresh
        structure signal may open a new basket while the runner rides."""
        for pos in positions:
            if not pos.sl:
                return False
            if pos.type == mt5.POSITION_TYPE_BUY and pos.sl < pos.price_open:
                return False
            if pos.type == mt5.POSITION_TYPE_SELL and pos.sl > pos.price_open:
                return False
        return True

    def _verify_sl_tp(self, sl: float, tp: float):
        """Some brokers strip SL/TP on market orders. Verify every open
        position is protected; re-attach the levels if they are missing."""
        for pos in self.client.positions():
            if pos.sl and pos.tp:
                continue
            log.warning("Position %s missing SL/TP on broker side — fixing.", pos.ticket)
            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "symbol": self.symbol,
                "position": pos.ticket,
                "sl": round(pos.sl or sl, 3),
                "tp": round(pos.tp or tp, 3),
            }
            result = mt5.order_send(request)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                log.info("Position %s protected (SL %.3f / TP %.3f).",
                         pos.ticket, request["sl"], request["tp"])
            else:
                log.error("COULD NOT protect position %s — closing it for safety.",
                          pos.ticket)
                self._close_position(pos, "unprotected position safety close")

    # ----- closing -----

    def close_all(self, reason: str):
        for pos in self.client.positions():
            self._close_position(pos, reason)

    def _close_position(self, pos, reason: str) -> bool:
        tick = self.client.get_tick()
        if tick is None:
            return False
        if pos.type == mt5.POSITION_TYPE_BUY:
            order_type = mt5.ORDER_TYPE_SELL
            price = tick.bid
        else:
            order_type = mt5.ORDER_TYPE_BUY
            price = tick.ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": pos.volume,
            "type": order_type,
            "position": pos.ticket,
            "price": price,
            "deviation": self.config["deviation_points"],
            "magic": self.config["magic_number"],
            "comment": reason[:31],
            "type_time": mt5.ORDER_TIME_GTC,
        }
        result = send_deal(request, self.symbol)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            log.info("CLOSED ticket %s (%.2f lots) — %s | P/L %.2f",
                     pos.ticket, pos.volume, reason, pos.profit)
            return True
        log.error("Failed to close ticket %s: %s", pos.ticket,
                  result.comment if result else mt5.last_error())
        return False

    # ----- management: protection ladder + time stop -----

    def manage_positions(self, current_atr: float, structure=None):
        tick = self.client.get_tick()
        if tick is None:
            return

        bar_seconds = self.config["timeframe_minutes"] * 60
        state = self._load_basket_state()
        open_tickets = set()

        for pos in self.client.positions():
            open_tickets.add(str(pos.ticket))
            is_buy = pos.type == mt5.POSITION_TYPE_BUY
            price = tick.bid if is_buy else tick.ask
            meta = state.get(str(pos.ticket), {})
            risk_unit = meta.get("risk_unit")

            # Time stop: a trade that has produced nothing after N bars is
            # dead weight drifting toward its stop — cut it while it's small.
            profit_r = profit_in_r(is_buy, pos.price_open, pos.tp, price,
                                   self.config["min_reward_risk"], risk_unit)
            bars_open = (tick.time - pos.time) / bar_seconds
            if (bars_open >= self.config["time_stop_bars"]
                    and profit_r < self.config["protect_rr"]):
                self._close_position(pos, "time stop — trade going nowhere")
                continue

            new_sl = compute_protective_sl(
                is_buy, pos.price_open, pos.sl, pos.tp,
                price, current_atr, structure, self.config,
                risk_unit=risk_unit,
            )
            if new_sl is not None:
                self._modify_sl(pos, new_sl)

        # Forget closed tickets so the state file never grows unbounded.
        stale = [t for t in state if t not in open_tickets]
        if stale:
            for t in stale:
                state.pop(t, None)
            self._save_basket_state(state)

    def _modify_sl(self, pos, new_sl: float):
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": self.symbol,
            "position": pos.ticket,
            "sl": round(new_sl, 3),
            "tp": pos.tp,
        }
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            log.info("Ticket %s: stop moved to %.3f", pos.ticket, new_sl)
        else:
            log.warning("Could not modify SL for ticket %s: %s",
                        pos.ticket, result.comment if result else mt5.last_error())
