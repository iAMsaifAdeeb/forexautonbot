"""
Trade execution and open-position management:
- market orders with SL/TP,
- close-all (used when the daily target is hit),
- breakeven move at +1R and ATR trailing stop afterwards.
"""

import logging

import MetaTrader5 as mt5

log = logging.getLogger("bot.trade")

FILLING_MODES = [mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN]


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

        if direction == "BUY":
            order_type = mt5.ORDER_TYPE_BUY
            price = tick.ask
        else:
            order_type = mt5.ORDER_TYPE_SELL
            price = tick.bid

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": volume,
            "type": order_type,
            "price": price,
            "sl": round(sl, 3),
            "tp": round(tp, 3),
            "deviation": self.config["deviation_points"],
            "magic": self.config["magic_number"],
            "comment": comment[:31],
            "type_time": mt5.ORDER_TIME_GTC,
        }

        for filling in FILLING_MODES:
            request["type_filling"] = filling
            result = mt5.order_send(request)
            if result is None:
                continue
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                log.info(
                    "OPENED %s %.2f lots %s @ %.3f | SL %.3f | TP %.3f | %s",
                    direction, volume, self.symbol, result.price, sl, tp, comment,
                )
                self._verify_sl_tp(sl, tp)
                return True
            if result.retcode != mt5.TRADE_RETCODE_INVALID_FILL:
                log.error("Order failed: retcode=%s comment=%s", result.retcode, result.comment)
                return False

        log.error("Order failed with every filling mode: %s", mt5.last_error())
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
        for filling in FILLING_MODES:
            request["type_filling"] = filling
            result = mt5.order_send(request)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                log.info("CLOSED ticket %s (%.2f lots) — %s | P/L %.2f",
                         pos.ticket, pos.volume, reason, pos.profit)
                return True
            if result and result.retcode != mt5.TRADE_RETCODE_INVALID_FILL:
                break
        log.error("Failed to close ticket %s: %s", pos.ticket, mt5.last_error())
        return False

    # ----- management: breakeven + trailing -----

    def manage_positions(self, current_atr: float):
        tick = self.client.get_tick()
        if tick is None:
            return

        for pos in self.client.positions():
            is_buy = pos.type == mt5.POSITION_TYPE_BUY
            price = tick.bid if is_buy else tick.ask
            entry = pos.price_open
            sl = pos.sl

            initial_risk = abs(entry - sl) if sl else current_atr
            if initial_risk <= 0:
                continue

            profit_distance = (price - entry) if is_buy else (entry - price)
            new_sl = None

            # 1) Breakeven once the trade is +1R
            be_trigger = self.config["breakeven_rr"] * initial_risk
            sl_below_entry = (sl < entry) if is_buy else (sl > entry)
            if profit_distance >= be_trigger and (not sl or sl_below_entry):
                new_sl = entry

            # 2) ATR trailing stop once past breakeven
            trail = self.config["trail_atr_mult"] * current_atr
            if profit_distance > be_trigger:
                candidate = (price - trail) if is_buy else (price + trail)
                improves = (
                    (is_buy and candidate > max(sl, entry))
                    or (not is_buy and candidate < min(sl, entry))
                )
                if improves:
                    new_sl = candidate

            if new_sl is not None and abs(new_sl - sl) > 1e-6:
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
