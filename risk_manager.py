"""
Risk manager — the "care taker" of the account.

Responsibilities:
- Daily 5% equity target: once reached, close everything and stop until the
  next trading day (Rules 3 & 8).
- 10% drawdown guard: pause, observe the market, then trade at reduced risk
  until the loss is recovered, then resume normal rules (Rule 11).
- Position sizing from live account equity and the stop distance (Rule 10).
- Trade-count limits so the bot never over-trades (Rule 7).

All state survives restarts via a JSON file.
"""

import json
import logging
import os
from datetime import date

log = logging.getLogger("bot.risk")

MODE_NORMAL = "NORMAL"
MODE_TARGET_DONE = "TARGET_DONE"   # +5% hit, done for the day
MODE_OBSERVE = "OBSERVE"           # -10% hit, watching the market
MODE_RECOVERY = "RECOVERY"         # trading small until drawdown recovered
MODE_DAY_STOPPED = "DAY_STOPPED"   # daily loss limit or profit lock triggered


class RiskManager:
    def __init__(self, config: dict, current_equity: float):
        self.config = config
        self.state_path = config["state_file"]
        self.state = self._load_state(current_equity)

    # ----- persistence -----

    def _default_state(self, equity: float) -> dict:
        return {
            "day": date.today().isoformat(),
            "day_start_equity": equity,
            "day_peak_equity": equity,
            "baseline_equity": equity,   # high-water mark for drawdown tracking
            "trades_today": 0,
            "mode": MODE_NORMAL,
            "observe_bars_left": 0,
            "pause_bars_left": 0,        # loss-streak cooldown
            "last_consec_losses": 0,
        }

    def _load_state(self, equity: float) -> dict:
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, "r", encoding="utf-8") as f:
                    state = json.load(f)
                log.info("Loaded state: %s", state)
                return state
            except (json.JSONDecodeError, OSError):
                log.warning("State file unreadable, starting fresh.")
        return self._default_state(equity)

    def save(self):
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2)

    # ----- daily rollover -----

    def roll_day_if_needed(self, equity: float) -> bool:
        """Returns True when the calendar day just rolled over."""
        today = date.today().isoformat()
        if self.state["day"] != today:
            log.info("New trading day %s. Day-start equity: %.2f", today, equity)
            mode = self.state["mode"]
            self.state["day"] = today
            self.state["day_start_equity"] = equity
            self.state["day_peak_equity"] = equity
            self.state["trades_today"] = 0
            self.state["pause_bars_left"] = 0
            self.state["last_consec_losses"] = 0
            # A finished/stopped day unlocks trading again; an active drawdown
            # recovery carries over to the next day (Rule 11).
            if mode in (MODE_TARGET_DONE, MODE_DAY_STOPPED):
                self.state["mode"] = MODE_NORMAL
            self.state["baseline_equity"] = max(self.state["baseline_equity"], equity)
            self.save()
            return True
        return False

    # ----- mode transitions -----

    def update(self, equity: float, has_open_positions: bool,
               day_profits: list[float] | None = None,
               balance: float | None = None) -> str:
        """Evaluate equity against the daily target, the drawdown guard and
        all loss guards. `day_profits` is the ordered list of today's closed
        trade results (used for the consecutive-loss cooldown).
        Returns the current mode."""
        new_day = self.roll_day_if_needed(equity)
        st = self.state
        prev_mode = st["mode"]

        # Live snapshot for the control panel display.
        st["last_equity"] = equity
        if balance is not None:
            st["last_balance"] = balance

        # Track the high-water marks.
        st["day_peak_equity"] = max(st.get("day_peak_equity", equity), equity)
        if st["mode"] == MODE_NORMAL:
            st["baseline_equity"] = max(st["baseline_equity"], equity)

        day_start = st["day_start_equity"]
        day_pct = (equity - day_start) / day_start * 100
        peak_pct = (st["day_peak_equity"] - day_start) / day_start * 100

        target_equity = st["day_start_equity"] * (1 + self.config["daily_target_pct"] / 100)
        drawdown_equity = st["baseline_equity"] * (1 - self.config["max_drawdown_pct"] / 100)

        if st["mode"] in (MODE_NORMAL, MODE_RECOVERY) and equity >= target_equity:
            log.info(
                "DAILY TARGET REACHED: equity %.2f >= %.2f (+%s%%). Done for today.",
                equity, target_equity, self.config["daily_target_pct"],
            )
            st["mode"] = MODE_TARGET_DONE
            st["baseline_equity"] = max(st["baseline_equity"], equity)

        elif st["mode"] == MODE_NORMAL and equity <= drawdown_equity:
            log.warning(
                "DRAWDOWN GUARD: equity %.2f <= %.2f (-%s%% from %.2f). "
                "Pausing to observe the market.",
                equity, drawdown_equity, self.config["max_drawdown_pct"],
                st["baseline_equity"],
            )
            st["mode"] = MODE_OBSERVE
            st["observe_bars_left"] = self.config["observe_bars"]

        elif st["mode"] == MODE_OBSERVE and st["observe_bars_left"] <= 0:
            log.info("Observation finished. Entering RECOVERY mode (reduced risk).")
            st["mode"] = MODE_RECOVERY

        elif st["mode"] == MODE_RECOVERY and equity >= st["baseline_equity"]:
            log.info("Drawdown fully recovered (equity %.2f). Back to normal rules.", equity)
            st["mode"] = MODE_NORMAL

        # Daily loss circuit-breaker: a bad day ends early, long before the
        # 10% drawdown guard would. (The drawdown guard above wins if both hit.)
        elif st["mode"] == MODE_NORMAL and day_pct <= -self.config["daily_loss_limit_pct"]:
            log.warning(
                "DAILY LOSS LIMIT: day P/L %.2f%% <= -%s%%. "
                "Trading stopped until tomorrow — protecting the funds.",
                day_pct, self.config["daily_loss_limit_pct"],
            )
            st["mode"] = MODE_DAY_STOPPED

        # Profit lock: once the day made real money, never let it all bleed back.
        elif (st["mode"] == MODE_NORMAL
              and peak_pct >= self.config["profit_lock_trigger_pct"]
              and day_pct <= peak_pct * (1 - self.config["profit_lock_giveback_pct"] / 100)):
            log.warning(
                "PROFIT LOCK: day peaked at +%.2f%%, now +%.2f%%. "
                "Locking in the day's profit — no more trades today.",
                peak_pct, day_pct,
            )
            st["mode"] = MODE_DAY_STOPPED

        # Consecutive-loss cooldown: 3 losses in a row means the market is
        # not cooperating right now — step back and let it develop.
        if day_profits is not None:
            consec = 0
            for profit in reversed(day_profits):
                if profit < 0:
                    consec += 1
                else:
                    break
            if (consec >= self.config["consec_loss_count"]
                    and consec > st.get("last_consec_losses", 0)):
                st["pause_bars_left"] = self.config["loss_pause_bars"]
                log.warning(
                    "%d consecutive losses — cooling down for %d bars.",
                    consec, self.config["loss_pause_bars"],
                )
            st["last_consec_losses"] = consec

        self.save()
        st["_new_day"] = new_day
        st["_target_just_hit"] = (
            prev_mode not in (MODE_TARGET_DONE,) and st["mode"] == MODE_TARGET_DONE
        )
        return st["mode"]

    def on_new_bar(self):
        if self.state["mode"] == MODE_OBSERVE and self.state["observe_bars_left"] > 0:
            self.state["observe_bars_left"] -= 1
        if self.state.get("pause_bars_left", 0) > 0:
            self.state["pause_bars_left"] -= 1
        self.save()

    def on_trade_opened(self):
        self.state["trades_today"] += 1
        self.save()

    # ----- permissions -----

    def can_open_trade(self, open_positions: int) -> tuple[bool, str]:
        st = self.state
        if st["mode"] == MODE_TARGET_DONE:
            return False, "daily 5% target already reached — waiting for next day"
        if st["mode"] == MODE_DAY_STOPPED:
            return False, "day stopped by loss limit / profit lock — waiting for next day"
        if st["mode"] == MODE_OBSERVE:
            return False, f"observing market after drawdown ({st['observe_bars_left']} bars left)"
        if st.get("pause_bars_left", 0) > 0:
            return False, f"cooling down after consecutive losses ({st['pause_bars_left']} bars left)"
        if open_positions >= self.config["max_open_positions"]:
            return False, "max open positions reached"
        cap = self.config["max_trades_per_day"]
        if cap > 0 and st["trades_today"] >= cap:
            return False, "max trades for today reached"
        return True, ""

    def current_risk_pct(self, confidence: float | None = None) -> float:
        """Risk tier: reduced in recovery, boosted (but capped) for
        exceptional-confidence setups, normal otherwise."""
        if self.state["mode"] == MODE_RECOVERY:
            return self.config["recovery_risk_pct"]
        if (confidence is not None
                and confidence >= self.config["high_confidence_score"]):
            return self.config["high_confidence_risk_pct"]
        return self.config["risk_per_trade_pct"]

    # ----- position sizing -----

    def lot_size(self, equity: float, sl_distance: float, symbol_info,
                 risk_pct: float | None = None) -> float:
        """Volume such that hitting the stop loses `risk_pct` of equity.

        loss_per_lot = (sl_distance / tick_size) * tick_value
        """
        pct = risk_pct if risk_pct is not None else self.current_risk_pct()
        risk_amount = equity * pct / 100.0
        tick_size = symbol_info.trade_tick_size
        tick_value = symbol_info.trade_tick_value
        if tick_size <= 0 or tick_value <= 0 or sl_distance <= 0:
            return 0.0

        loss_per_lot = (sl_distance / tick_size) * tick_value
        lots = risk_amount / loss_per_lot

        step = symbol_info.volume_step or 0.01
        lots = max(symbol_info.volume_min, min(symbol_info.volume_max, lots))
        lots = round(lots / step) * step
        # Guard against floating point artifacts like 0.30000000000000004
        return round(lots, 8)
