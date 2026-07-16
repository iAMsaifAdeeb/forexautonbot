"""
10 PIPS strategy — ONE trade at a time (Buy Stop OR Sell Stop), never a grid.

Style (same as your sample):
  - Buy Stop ABOVE market, TP = +10 pips
  - Sell Stop BELOW market, TP = -10 pips
  - NO hard stop-loss

Rules:
  - Max 1 pending order OR 1 live position (excluding hedge/recovery flow).
  - Direction from HH/HL / LL/LH + EMA 5.
  - Stay 30 pips before previous high/low rejection after a profit on that side.
  - If price runs 50 pips against without TP → HEDGE (never SL), wait 15 min,
    then recovery. When floating covers hedge loss → close all + clear state.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass

import MetaTrader5 as mt5
import pandas as pd

import market_structure as ms
from indicators import ema

log = logging.getLogger("bot.ten_pips")

STATE_FILE_DEFAULT = "ten_pips_state.json"

PHASE_IDLE = "IDLE"
PHASE_ARMED = "ARMED"
PHASE_HEDGE_WAIT = "HEDGE_WAIT"
PHASE_RECOVERY = "RECOVERY"


@dataclass
class GridLevel:
    direction: str
    entry: float
    take_profit: float


def pip_size(config: dict) -> float:
    return float(config.get("pip_size", 0.10))


def state_path(config: dict) -> str:
    name = config.get("ten_pips_state_file", STATE_FILE_DEFAULT)
    if os.path.isabs(name):
        return name
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


def load_state(config: dict) -> dict:
    path = state_path(config)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "phase": PHASE_IDLE,
        "grid_armed": False,
        "hedge_ticket": None,
        "hedge_loss": 0.0,
        "hedge_at": 0.0,
        "victim_ticket": None,
        "recovery_ticket": None,
        "last_buy_profit_at": None,
        "last_sell_profit_at": None,
        "mid_price": None,
    }


def save_state(config: dict, state: dict):
    try:
        with open(state_path(config), "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except OSError:
        pass


def clear_state(config: dict):
    save_state(config, {
        "phase": PHASE_IDLE,
        "grid_armed": False,
        "hedge_ticket": None,
        "hedge_loss": 0.0,
        "hedge_at": 0.0,
        "victim_ticket": None,
        "recovery_ticket": None,
        "last_buy_profit_at": None,
        "last_sell_profit_at": None,
        "mid_price": None,
    })
    log.info("10 PIPS dashboard cleared.")


def attach_ema5(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ema5"] = ema(out["close"], 5)
    return out


def rejection_levels(df: pd.DataFrame, config: dict) -> tuple[float | None, float | None]:
    lookback = int(config.get("swing_lookback", 3))
    st = ms.analyze(df, lookback)
    hi = st.last_swing_high.price if st.last_swing_high else None
    lo = st.last_swing_low.price if st.last_swing_low else None
    return hi, lo


def structure_bias(df: pd.DataFrame, config: dict) -> str | None:
    """HH/HL → BUY; LL/LH → SELL. EMA5 as tie-break."""
    st = ms.analyze(df, int(config.get("swing_lookback", 3)))
    if st.trend == ms.UPTREND:
        return "BUY"
    if st.trend == ms.DOWNTREND:
        return "SELL"
    if "ema5" in df.columns and len(df) >= 2:
        if float(df["close"].iloc[-1]) > float(df["ema5"].iloc[-1]):
            return "BUY"
        if float(df["close"].iloc[-1]) < float(df["ema5"].iloc[-1]):
            return "SELL"
    return None


def plan_one_trade(mid: float, direction: str, config: dict,
                   reject_hi: float | None = None,
                   reject_lo: float | None = None,
                   lock_buys: bool = False,
                   lock_sells: bool = False) -> GridLevel | None:
    """One Buy Stop above mid OR one Sell Stop below mid (never both)."""
    pip = pip_size(config)
    offset = float(config.get("ten_pips_entry_offset_pips", 20)) * pip
    tp_dist = float(config.get("ten_pips_tp_pips", 10)) * pip
    margin = float(config.get("ten_pips_reject_margin_pips", 30)) * pip

    if direction == "BUY":
        entry = mid + offset
        if lock_buys and reject_hi is not None and entry > reject_hi - margin:
            return None
        return GridLevel("BUY", entry, entry + tp_dist)

    if direction == "SELL":
        entry = mid - offset
        if lock_sells and reject_lo is not None and entry < reject_lo + margin:
            return None
        return GridLevel("SELL", entry, entry - tp_dist)

    return None


def build_grid(mid: float, config: dict,
               reject_hi: float | None = None,
               reject_lo: float | None = None,
               lock_buys: bool = False,
               lock_sells: bool = False) -> list[GridLevel]:
    """Legacy helper — now returns at most ONE level (one-trade mode)."""
    # Prefer BUY first if both would have been planned; callers should use plan_one_trade.
    bias = "BUY"
    one = plan_one_trade(mid, bias, config, reject_hi, reject_lo, lock_buys, lock_sells)
    return [one] if one else []


def adverse_pips(pos, pip: float) -> float:
    if pos.type == mt5.POSITION_TYPE_BUY:
        move = float(pos.price_open) - float(pos.price_current)
    else:
        move = float(pos.price_current) - float(pos.price_open)
    return max(0.0, move / pip) if pip > 0 else 0.0


def _is_hedge_or_recovery(pos, state: dict) -> bool:
    ticket = int(pos.ticket)
    return ticket in (
        int(state.get("hedge_ticket") or 0),
        int(state.get("recovery_ticket") or 0),
    )


def run(log_bot, client, trader, risk, analyzed, equity, config: dict,
        newest_closed_time, positions) -> None:
    """Main 10 PIPS tick — one trade at a time."""
    state = load_state(config)
    pip = pip_size(config)
    df = attach_ema5(analyzed)
    reject_hi, reject_lo = rejection_levels(df, config)
    tick = client.get_tick()
    if tick is None:
        return

    mid = (float(tick.bid) + float(tick.ask)) / 2.0
    pendings = client.pending_orders()
    hedge_pips = float(config.get("ten_pips_hedge_pips", 50))
    wait_sec = float(config.get("ten_pips_hedge_wait_seconds", 15 * 60))

    # ----- recovery cover complete? -----
    if state.get("phase") == PHASE_RECOVERY and positions:
        hedge_loss = abs(float(state.get("hedge_loss") or 0.0))
        floating = sum(float(p.profit) for p in positions)
        if hedge_loss > 0 and floating >= hedge_loss:
            log_bot.info("10 PIPS: recovery covered hedge loss %.2f — closing ALL.",
                         hedge_loss)
            trader.close_all("10 pips recovery complete")
            trader.cancel_pending("10 pips clear")
            clear_state(config)
            return
        log_bot.info("%s | 10 PIPS RECOVERY: floating %.2f / need %.2f",
                     newest_closed_time.strftime("%H:%M"), floating, hedge_loss)
        return

    if state.get("phase") == PHASE_RECOVERY and not positions:
        clear_state(config)
        state = load_state(config)

    # ----- hedge wait → recovery -----
    if state.get("phase") == PHASE_HEDGE_WAIT:
        elapsed = time.time() - float(state.get("hedge_at") or 0.0)
        left = max(0.0, wait_sec - elapsed)
        if left > 0:
            log_bot.info("%s | 10 PIPS HEDGE WAIT: %.0f sec left (eq %.2f)",
                         newest_closed_time.strftime("%H:%M"), left, equity)
            return
        bias = structure_bias(df, config)
        if bias is None:
            log_bot.info("%s | 10 PIPS: wait ended — no HH/HL or LL/LH bias yet",
                         newest_closed_time.strftime("%H:%M"))
            return
        hedge_loss = abs(float(state.get("hedge_loss") or 0.0))
        sl_proxy = hedge_pips * pip
        volume = risk.lot_size(equity, sl_proxy, client.symbol_info(),
                               risk.current_risk_pct())
        if volume <= 0:
            return
        far = max(sl_proxy * 3, pip * 50)
        tp = (float(tick.ask) + far) if bias == "BUY" else (float(tick.bid) - far)
        ok = trader.open_trade_no_sl(bias, volume, tp, "GG recover")
        if ok:
            state["phase"] = PHASE_RECOVERY
            save_state(config, state)
            log_bot.info("10 PIPS RECOVERY %s %.2f lots — cover hedge %.2f",
                         bias, volume, hedge_loss)
            risk.on_trade_opened()
        return

    # ----- 50-pip adverse → HEDGE (no SL) -----
    for pos in positions:
        if _is_hedge_or_recovery(pos, state):
            continue
        if adverse_pips(pos, pip) < hedge_pips:
            continue
        if state.get("victim_ticket") == int(pos.ticket) and state.get("hedge_ticket"):
            continue
        hedge_dir = "SELL" if pos.type == mt5.POSITION_TYPE_BUY else "BUY"
        loss_now = abs(min(0.0, float(pos.profit)))
        ok = trader.open_hedge(hedge_dir, float(pos.volume), "GG hedge")
        if ok:
            fresh = client.positions()
            hedge_pos = None
            for p in fresh:
                if (hedge_dir == "BUY" and p.type == mt5.POSITION_TYPE_BUY) or (
                        hedge_dir == "SELL" and p.type == mt5.POSITION_TYPE_SELL):
                    if int(p.ticket) != int(pos.ticket):
                        hedge_pos = p
            state["phase"] = PHASE_HEDGE_WAIT
            state["victim_ticket"] = int(pos.ticket)
            state["hedge_ticket"] = int(hedge_pos.ticket) if hedge_pos else None
            state["hedge_loss"] = loss_now
            state["hedge_at"] = time.time()
            trader.cancel_pending("hedge — pause")
            save_state(config, state)
            log_bot.warning(
                "10 PIPS HEDGE: ticket %s was -%.0f pips — opened %s hedge. "
                "Waiting 15 min. Est loss %.2f",
                pos.ticket, hedge_pips, hedge_dir, loss_now)
            return

    if state.get("phase") in (PHASE_HEDGE_WAIT, PHASE_RECOVERY):
        return

    # Track profits for rejection lockout
    profits = client.today_deal_profits()
    if profits and profits[-1] > 0:
        last = df.iloc[-1]
        if float(last["close"]) >= float(last.get("ema5", last["close"])):
            state["last_buy_profit_at"] = float(last["close"])
        else:
            state["last_sell_profit_at"] = float(last["close"])
        save_state(config, state)

    # Live positions (excluding hedge/recovery) → one trade already open
    live = [p for p in positions if not _is_hedge_or_recovery(p, state)]
    if live:
        # Enforce one position — close extras if any (safety)
        if len(live) > 1:
            log_bot.warning("10 PIPS: %d positions — closing extras (one-trade rule).",
                            len(live))
            for extra in live[1:]:
                trader._close_position(extra, "10 pips one-trade only")
            trader.cancel_pending("one-trade only")
        pos = live[0]
        side = "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL"
        log_bot.info("%s | 10 PIPS LIVE %s ticket %s | EMA5 %.2f | eq %.2f",
                     newest_closed_time.strftime("%H:%M"), side, pos.ticket,
                     float(df["ema5"].iloc[-1]), equity)
        state["phase"] = PHASE_ARMED
        save_state(config, state)
        # No new pending while a trade is open
        if pendings:
            trader.cancel_pending("position open — one trade only")
        return

    # Pending: keep at most ONE; cancel the rest (clears old multi-grid)
    if len(pendings) > 1:
        log_bot.warning("10 PIPS: %d pendings found — keeping 1, cancelling extras.",
                        len(pendings))
        # Cancel all then re-arm one below (cleanest)
        trader.cancel_pending("one-trade only — clear multi grid")
        pendings = []

    if pendings:
        buy_n, sell_n = trader.pending_side_counts()
        log_bot.info("%s | 10 PIPS ARMED: 1 pending (%dB/%dS) | EMA5 %.2f | eq %.2f",
                     newest_closed_time.strftime("%H:%M"),
                     buy_n, sell_n, float(df["ema5"].iloc[-1]), equity)
        state["phase"] = PHASE_ARMED
        state["grid_armed"] = True
        save_state(config, state)
        return

    # Flat → place ONE stop in structure direction
    bias = structure_bias(df, config)
    if bias is None:
        log_bot.info("%s | 10 PIPS: waiting for HH/HL or LL/LH + EMA5 bias (eq %.2f)",
                     newest_closed_time.strftime("%H:%M"), equity)
        return

    lock_buys = state.get("last_buy_profit_at") is not None
    lock_sells = state.get("last_sell_profit_at") is not None
    level = plan_one_trade(mid, bias, config, reject_hi, reject_lo,
                           lock_buys=lock_buys, lock_sells=lock_sells)
    if level is None:
        log_bot.info("%s | 10 PIPS: %s blocked near rejection zone (eq %.2f)",
                     newest_closed_time.strftime("%H:%M"), bias, equity)
        return

    sl_proxy = hedge_pips * pip
    volume = risk.lot_size(equity, sl_proxy, client.symbol_info(),
                           risk.current_risk_pct())
    if volume <= 0:
        log_bot.warning("10 PIPS: lot size 0 — check equity / symbol.")
        return

    ticket = trader.place_stop_order(
        level.direction, volume, level.entry, sl=0.0, tp=level.take_profit,
        comment="GG 10pips", allow_no_sl=True,
    )
    if ticket:
        state["phase"] = PHASE_ARMED
        state["grid_armed"] = True
        state["mid_price"] = mid
        save_state(config, state)
        risk.on_trade_opened()
        log_bot.info(
            "10 PIPS ONE TRADE: %s STOP @ %.3f | TP %.3f (+%d pips) | "
            "NO SL (hedge @ %.0f) | mid %.2f | eq %.2f",
            level.direction, level.entry, level.take_profit,
            int(config.get("ten_pips_tp_pips", 10)),
            hedge_pips, mid, equity)
    else:
        log_bot.warning("10 PIPS: broker rejected the single pending stop.")
