"""
Trade execution and open-position management:
- market orders with SL/TP (never without),
- close-all (used when the daily target is hit),
- staged protection ladder: half-risk -> breakeven -> profit lock -> trailing
  behind market structure and ATR,
- time stop for trades that go nowhere.
"""

import logging

import MetaTrader5 as mt5

from mt5_orders import round_price, send_deal

log = logging.getLogger("bot.trade")


def compute_protective_sl(is_buy: bool, entry: float, sl: float, tp: float,
                          price: float, atr: float, structure, config: dict):
    """The staged stop-loss ladder. Returns the new SL, or None if the
    current stop should stay where it is. Stops only ever move in the
    trade's favor — never backwards.

    R = the trade's initial risk (recovered from the TP distance, which is
    always `min_reward_risk` x the initial stop distance)."""
    sign = 1 if is_buy else -1

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
                min_reward_risk: float) -> float:
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

    # ----- opening -----

    def open_trade(self, direction: str, volume: float, sl: float, tp: float, comment: str) -> bool:
        # HARD RULE: no trade ever leaves without both a stop-loss and a
        # take-profit. If either is missing the order is refused outright.
        if not sl or not tp or sl <= 0 or tp <= 0:
            log.error("REFUSED order: SL/TP missing (sl=%s, tp=%s).", sl, tp)
            return False
        if direction == "BUY" and not (sl < tp):
            log.error("REFUSED BUY: SL %.3f must be below TP %.3f.", sl, tp)
            return False
        if direction == "SELL" and not (sl > tp):
            log.error("REFUSED SELL: SL %.3f must be above TP %.3f.", sl, tp)
            return False

        tick = self.client.get_tick()
        if tick is None:
            log.error("No tick data, cannot open trade.")
            return False

        # Spread guard: a blown-out spread (news, rollover, thin market)
        # ruins the trade's math before it even starts.
        info = self.client.symbol_info()
        point = info.point if info and info.point > 0 else 0.01
        spread_points = (tick.ask - tick.bid) / point
        if spread_points > self.config["max_spread_points"]:
            log.warning("REFUSED order: spread %.0f points > max %d — waiting for "
                        "normal conditions.", spread_points, self.config["max_spread_points"])
            return False

        if direction == "BUY":
            order_type = mt5.ORDER_TYPE_BUY
            price = tick.ask
        else:
            order_type = mt5.ORDER_TYPE_SELL
            price = tick.bid

        # Margin guard: never send an order the account cannot comfortably hold.
        margin_needed = mt5.order_calc_margin(order_type, self.symbol, volume, price)
        account = mt5.account_info()
        if margin_needed and account and margin_needed > account.margin_free * 0.9:
            log.error("REFUSED order: needs %.2f margin, only %.2f free.",
                      margin_needed, account.margin_free)
            return False

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
            return True
        if result:
            log.error("Order failed: retcode=%s comment=%s", result.retcode, result.comment)
        else:
            log.error("Order failed: %s", mt5.last_error())
        return False

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

        for pos in self.client.positions():
            is_buy = pos.type == mt5.POSITION_TYPE_BUY
            price = tick.bid if is_buy else tick.ask

            # Time stop: a trade that has produced nothing after N bars is
            # dead weight drifting toward its stop — cut it while it's small.
            profit_r = profit_in_r(is_buy, pos.price_open, pos.tp, price,
                                   self.config["min_reward_risk"])
            bars_open = (tick.time - pos.time) / bar_seconds
            if (bars_open >= self.config["time_stop_bars"]
                    and profit_r < self.config["protect_rr"]):
                self._close_position(pos, "time stop — trade going nowhere")
                continue

            new_sl = compute_protective_sl(
                is_buy, pos.price_open, pos.sl, pos.tp,
                price, current_atr, structure, self.config,
            )
            if new_sl is not None:
                self._modify_sl(pos, new_sl)

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
