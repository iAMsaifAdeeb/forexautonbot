"""
10 PIPS strategy (V25) — M5 gold grid with hedge recovery, NEVER a hard SL.

On Start (flat account):
  - From live mid-price, arm 10 Buy Stops ABOVE and 10 Sell Stops BELOW
    spanning 50 pips each side (20 pending orders total).
  - Every fill has a fixed 10-pip take-profit. No stop-loss.

Structure guards (EMA 5 + swings + HH/HL/LL/LH):
  - Respect previous high / low rejection zones: do not keep taking trades
    into a zone once price is within 30 pips of that rejection level after
    a profit was booked on this side.

Adverse move:
  - If a live position drifts 50 pips against without hitting TP → HEDGE
    (open equal opposite market order). Never SL.
  - After hedge: wait 15 minutes, then open a recovery trade with the
    structure (HH/HL = buy recovery, LL/LH = sell recovery).
  - When floating + closed profit covers the hedge loss → close EVERYTHING
    and clear the ladder state (dashboard reset).
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
PHASE_GRID = "GRID"
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
    """Nearest structural rejection high / low (prev swing + HH/HL context)."""
    lookback = int(config.get("swing_lookback", 3))
    st = ms.analyze(df, lookback)
    hi = st.last_swing_high.price if st.last_swing_high else None
    lo = st.last_swing_low.price if st.last_swing_low else None
    return hi, lo


def structure_bias(df: pd.DataFrame, config: dict) -> str | None:
    """HH/HL → BUY recovery; LL/LH → SELL recovery. EMA5 as tie-break."""
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


def build_grid(mid: float, config: dict,
               reject_hi: float | None = None,
               reject_lo: float | None = None,
               lock_buys: bool = False,
               lock_sells: bool = False) -> list[GridLevel]:
    """20 stops: 10 Buy above mid, 10 Sell below mid, spanning ``band_pips``.

    After a profit on a side, ``lock_buys`` / ``lock_sells`` skips new stops
    that sit within ``reject_margin`` of the previous high / low rejection.
    """
    pip = pip_size(config)
    band = float(config.get("ten_pips_band_pips", 50)) * pip
    legs = max(1, int(config.get("ten_pips_legs_per_side", 10)))
    tp_dist = float(config.get("ten_pips_tp_pips", 10)) * pip
    margin = float(config.get("ten_pips_reject_margin_pips", 30)) * pip
    step = band / legs

    levels: list[GridLevel] = []
    for i in range(1, legs + 1):
        buy_entry = mid + i * step
        sell_entry = mid - i * step
        # Buy side — independently of sells
        buy_blocked = (
            lock_buys and reject_hi is not None
            and buy_entry > reject_hi - margin
        )
        if not buy_blocked:
            levels.append(GridLevel("BUY", buy_entry, buy_entry + tp_dist))
        # Sell side — independently of buys
        sell_blocked = (
            lock_sells and reject_lo is not None
            and sell_entry < reject_lo + margin
        )
        if not sell_blocked:
            levels.append(GridLevel("SELL", sell_entry, sell_entry - tp_dist))
    return levels


def adverse_pips(pos, pip: float) -> float:
    """How many pips a position is underwater (0 if in profit)."""
    if pos.type == mt5.POSITION_TYPE_BUY:
        move = float(pos.price_open) - float(pos.price_current)
    else:
        move = float(pos.price_current) - float(pos.price_open)
    return max(0.0, move / pip) if pip > 0 else 0.0


def run(log_bot, client, trader, risk, analyzed, equity, config: dict,
        newest_closed_time, positions) -> None:
    """Main 10 PIPS tick — called every new M5 bar (and safe to call more often)."""
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

    # ----- PHASE: recovery cover complete? -----
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
        # Recovery closed early — reset
        clear_state(config)
        state = load_state(config)

    # ----- PHASE: hedge wait → then recovery entry -----
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
        # Recovery: market order WITH structure (HH/HL → BUY, LL/LH → SELL).
        # No hard SL. Far TP; manage cover by floating P/L vs hedge_loss.
        hedge_loss = abs(float(state.get("hedge_loss") or 0.0))
        sl_proxy = hedge_pips * pip
        volume = risk.lot_size(equity, sl_proxy, client.symbol_info(),
                               risk.current_risk_pct())
        if volume <= 0:
            return
        far = max(sl_proxy * 3, pip * 50)
        if bias == "BUY":
            tp = float(tick.ask) + far
        else:
            tp = float(tick.bid) - far
        ok = trader.open_trade_no_sl(bias, volume, tp, "GG recover")
        if ok:
            state["phase"] = PHASE_RECOVERY
            save_state(config, state)
            log_bot.info("10 PIPS RECOVERY %s %.2f lots — cover hedge %.2f",
                         bias, volume, hedge_loss)
            risk.on_trade_opened()
        return

    # ----- Monitor open positions for 50-pip adverse → HEDGE -----
    for pos in positions:
        # Skip if already the hedge / recovery ticket
        if int(pos.ticket) in (
                int(state.get("hedge_ticket") or 0),
                int(state.get("recovery_ticket") or 0)):
            continue
        if adverse_pips(pos, pip) < hedge_pips:
            continue
        # Already hedged this victim?
        if state.get("victim_ticket") == int(pos.ticket) and state.get("hedge_ticket"):
            continue
        # Open opposite hedge, NO SL
        hedge_dir = "SELL" if pos.type == mt5.POSITION_TYPE_BUY else "BUY"
        # Snapshot loss before hedge (approx)
        loss_now = abs(min(0.0, float(pos.profit)))
        ok = trader.open_hedge(hedge_dir, float(pos.volume), "GG hedge")
        if ok:
            # Find newest opposite position as hedge ticket
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
            trader.cancel_pending("hedge — pause grid")
            save_state(config, state)
            log_bot.warning(
                "10 PIPS HEDGE: %s ticket %s was -%.0f pips — opened %s hedge. "
                "Waiting 15 min. Est loss %.2f",
                "BUY" if pos.type == 0 else "SELL", pos.ticket, hedge_pips,
                hedge_dir, loss_now)
            return

    # ----- Arm / maintain grid when flat-ish and not in hedge flow -----
    if state.get("phase") in (PHASE_HEDGE_WAIT, PHASE_RECOVERY):
        return

    # Track profits for rejection-zone lockout
    profits = client.today_deal_profits()
    if profits and profits[-1] > 0:
        # Tag last side from open positions empty — use mid vs last close
        last = df.iloc[-1]
        if float(last["close"]) >= float(last.get("ema5", last["close"])):
            state["last_buy_profit_at"] = float(last["close"])
        else:
            state["last_sell_profit_at"] = float(last["close"])
        save_state(config, state)

    if pendings or positions:
        buy_n, sell_n = trader.pending_side_counts()
        log_bot.info("%s | 10 PIPS GRID: %d pending (%dB/%dS) | %d open | "
                     "EMA5 %.2f | rej H=%s L=%s (eq %.2f)",
                     newest_closed_time.strftime("%H:%M"),
                     len(pendings), buy_n, sell_n, len(positions),
                     float(df["ema5"].iloc[-1]),
                     f"{reject_hi:.1f}" if reject_hi else "-",
                     f"{reject_lo:.1f}" if reject_lo else "-",
                     equity)
        state["phase"] = PHASE_GRID
        state["grid_armed"] = True
        save_state(config, state)
        return

    # Fresh grid — after a side books profit, lock new stops near that
    # rejection zone (30 pips before prev high / low).
    lock_buys = state.get("last_buy_profit_at") is not None
    lock_sells = state.get("last_sell_profit_at") is not None
    levels = build_grid(mid, config, reject_hi, reject_lo,
                        lock_buys=lock_buys, lock_sells=lock_sells)
    if not levels:
        log_bot.info("%s | 10 PIPS: no safe grid levels near rejection (eq %.2f)",
                     newest_closed_time.strftime("%H:%M"), equity)
        return

    # Size each leg: risk split across 20
    legs_total = max(1, len(levels))
    risk_pct = risk.current_risk_pct() / legs_total
    # Proxy SL distance = hedge distance for lot math (no real SL sent)
    sl_proxy = hedge_pips * pip
    volume = risk.lot_size(equity, sl_proxy, client.symbol_info(), risk_pct)
    if volume <= 0:
        log_bot.warning("10 PIPS: lot size 0 — check equity / symbol.")
        return

    placed = 0
    for lvl in levels:
        ticket = trader.place_stop_order(
            lvl.direction, volume, lvl.entry, sl=0.0, tp=lvl.take_profit,
            comment="GG 10pips", allow_no_sl=True,
        )
        if ticket:
            placed += 1

    if placed:
        state["phase"] = PHASE_GRID
        state["grid_armed"] = True
        state["mid_price"] = mid
        save_state(config, state)
        risk.on_trade_opened()
        log_bot.info(
            "10 PIPS GRID ARMED: %d/%d stops | mid %.2f | ±%.0f pips | "
            "TP %d pips | NO SL (hedge at %.0f) | eq %.2f",
            placed, len(levels), mid,
            float(config.get("ten_pips_band_pips", 50)),
            int(config.get("ten_pips_tp_pips", 10)),
            hedge_pips, equity)
    else:
        log_bot.warning("10 PIPS: broker accepted 0 pending stops.")
