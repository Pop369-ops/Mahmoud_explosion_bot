"""
Trade Lifecycle Manager — auto-trailing SL + partial TP guidance.

This module monitors active trades and sends timely alerts:
- TP1 hit → suggest taking 50% off + move SL to break-even
- TP2 hit → suggest taking 30% more + move SL to TP1
- TP3 hit → close remaining 20% (or let it run with trailing SL)
- SL approaching → warning before it hits

The user still executes manually on Binance — this module just SUGGESTS
the right action at the right time. No auto-execution.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from core.models import Trade, now_riyadh
from core.logger import get_logger

log = get_logger(__name__)


@dataclass
class TrailingState:
    """Per-trade trailing state."""
    chat_id: int
    symbol: str
    direction: str = "long"
    initial_sl: float = 0.0
    current_sl: float = 0.0    # the suggested SL after trailing
    initial_entry: float = 0.0

    # TP levels
    tp1: float = 0.0
    tp2: float = 0.0
    tp3: float = 0.0

    # Lifecycle flags
    tp1_hit: bool = False
    tp2_hit: bool = False
    tp3_hit: bool = False

    # Position scaling (% of position remaining)
    position_remaining_pct: float = 100.0

    # Notifications sent
    sl_warning_sent: bool = False
    tp1_action_sent: bool = False
    tp2_action_sent: bool = False
    tp3_action_sent: bool = False

    # Metadata
    started_at: datetime = field(default_factory=now_riyadh)
    last_check_price: float = 0.0
    peak_price: float = 0.0
    trough_price: float = 0.0


class TrailingManager:
    """In-memory tracker for active trades with trailing logic."""

    def __init__(self):
        self._states: dict[tuple, TrailingState] = {}  # (chat_id, symbol) -> state

    def start_tracking(self, chat_id: int, symbol: str, trade: Trade,
                        direction: str = "long"):
        """Begin tracking a trade."""
        key = (chat_id, symbol)
        self._states[key] = TrailingState(
            chat_id=chat_id, symbol=symbol, direction=direction,
            initial_sl=trade.sl, current_sl=trade.sl,
            initial_entry=trade.entry,
            tp1=trade.tp1, tp2=trade.tp2, tp3=trade.tp3,
            peak_price=trade.entry, trough_price=trade.entry,
        )

    def stop_tracking(self, chat_id: int, symbol: str):
        self._states.pop((chat_id, symbol), None)

    def get(self, chat_id: int, symbol: str) -> Optional[TrailingState]:
        return self._states.get((chat_id, symbol))

    def get_all_for_user(self, chat_id: int) -> list[TrailingState]:
        return [s for (cid, _), s in self._states.items() if cid == chat_id]

    def update(self, chat_id: int, symbol: str, current_price: float) -> dict:
        """Process a price update. Returns action dict if any change."""
        key = (chat_id, symbol)
        state = self._states.get(key)
        if state is None:
            return {"action": "not_tracked"}

        state.last_check_price = current_price
        if current_price > state.peak_price:
            state.peak_price = current_price
        if current_price < state.trough_price or state.trough_price == 0:
            state.trough_price = current_price

        actions = []

        # ─── Long-side logic ───────────────────────────────
        if state.direction == "long":
            # SL hit?
            if current_price <= state.current_sl:
                actions.append({
                    "type": "sl_hit",
                    "price": state.current_sl,
                    "msg": (
                        f"🔴 *{symbol}: SL تم ضربه!*\n\n"
                        f"السعر: ${current_price:.6g}\n"
                        f"SL: ${state.current_sl:.6g}\n\n"
                        f"_أغلق الصفقة على Binance لو لم تنغلق تلقائياً._"
                    ),
                    "close": True,
                })
                return {"actions": actions, "should_stop": True}

            # SL warning (price within 0.3% of SL)
            sl_distance_pct = (current_price - state.current_sl) / current_price * 100
            if 0 < sl_distance_pct < 0.3 and not state.sl_warning_sent:
                state.sl_warning_sent = True
                actions.append({
                    "type": "sl_warning",
                    "msg": (
                        f"⚠️ *{symbol}: السعر قريب من SL!*\n\n"
                        f"السعر: ${current_price:.6g}\n"
                        f"SL: ${state.current_sl:.6g}\n"
                        f"المسافة: {sl_distance_pct:.2f}%\n\n"
                        f"_راقب الصفقة بحذر._"
                    ),
                })

            # TP1 hit
            if not state.tp1_hit and current_price >= state.tp1 and state.tp1 > 0:
                state.tp1_hit = True
                # Auto-move SL to break-even
                old_sl = state.current_sl
                state.current_sl = state.initial_entry
                state.position_remaining_pct = 50.0
                actions.append({
                    "type": "tp1_action",
                    "old_sl": old_sl,
                    "new_sl": state.current_sl,
                    "msg": (
                        f"🎯 *{symbol}: TP1 تحقق!*\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"💰 السعر: ${current_price:.6g}\n"
                        f"🎯 TP1: ${state.tp1:.6g}\n\n"
                        f"*✅ الإجراء الموصى به:*\n"
                        f"  1️⃣ أخرج *50%* من المركز\n"
                        f"  2️⃣ حرّك SL إلى Break-Even\n"
                        f"     `${old_sl:.6g}` → `${state.current_sl:.6g}`\n\n"
                        f"_بعد هذي اللحظة، أسوأ سيناريو: ربح صفر (مستحيل تخسر)._"
                    ),
                })

            # TP2 hit
            if (not state.tp2_hit and state.tp1_hit
                and current_price >= state.tp2 and state.tp2 > 0):
                state.tp2_hit = True
                old_sl = state.current_sl
                state.current_sl = state.tp1   # trail to TP1
                state.position_remaining_pct = 20.0
                actions.append({
                    "type": "tp2_action",
                    "old_sl": old_sl,
                    "new_sl": state.current_sl,
                    "msg": (
                        f"🎯 *{symbol}: TP2 تحقق!*\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"💰 السعر: ${current_price:.6g}\n"
                        f"🎯 TP2: ${state.tp2:.6g}\n\n"
                        f"*✅ الإجراء الموصى به:*\n"
                        f"  1️⃣ أخرج *30%* إضافية (الباقي 20%)\n"
                        f"  2️⃣ حرّك SL إلى TP1\n"
                        f"     `${old_sl:.6g}` → `${state.current_sl:.6g}`\n\n"
                        f"_الربح مضمون بنسبة TP1. الـ 20% المتبقية تجري نحو TP3._"
                    ),
                })

            # TP3 hit
            if (not state.tp3_hit and state.tp2_hit
                and current_price >= state.tp3 and state.tp3 > 0):
                state.tp3_hit = True
                state.position_remaining_pct = 0.0
                actions.append({
                    "type": "tp3_action",
                    "msg": (
                        f"🏆 *{symbol}: TP3 تحقق! إغلاق كامل*\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"💰 السعر: ${current_price:.6g}\n"
                        f"🏆 TP3: ${state.tp3:.6g}\n\n"
                        f"*✅ الإجراء:*\n"
                        f"  أخرج آخر *20%* — الصفقة كاملة 🎉\n\n"
                        f"_ممتاز! الصفقة استفادت من كامل الحركة._"
                    ),
                    "close": True,
                })
                return {"actions": actions, "should_stop": True}

            # Trailing SL (after TP2, trail aggressively)
            if state.tp2_hit and not state.tp3_hit:
                # Trail SL by 1.5% below current peak
                trail_sl = state.peak_price * 0.985
                if trail_sl > state.current_sl:
                    old_sl = state.current_sl
                    state.current_sl = trail_sl
                    # Notify only on significant moves (every 1% peak rise)
                    if (state.peak_price - state.tp2) / state.tp2 > 0.01:
                        actions.append({
                            "type": "trail_update",
                            "old_sl": old_sl,
                            "new_sl": state.current_sl,
                            "msg": (
                                f"📈 *{symbol}: SL متحرك تم تحديثه*\n\n"
                                f"السعر الحالي: ${current_price:.6g}\n"
                                f"القمة: ${state.peak_price:.6g}\n"
                                f"SL: `${old_sl:.6g}` → `${state.current_sl:.6g}`"
                            ),
                        })

        # ─── Short-side logic ─────────────────────────────
        else:  # short
            # SL hit
            if current_price >= state.current_sl:
                actions.append({
                    "type": "sl_hit",
                    "price": state.current_sl,
                    "msg": (f"🔴 *{symbol}: SL تم ضربه!*\n\nالسعر: ${current_price:.6g}"),
                    "close": True,
                })
                return {"actions": actions, "should_stop": True}

            # TP1 hit (short — price goes DOWN)
            if not state.tp1_hit and current_price <= state.tp1 and state.tp1 > 0:
                state.tp1_hit = True
                old_sl = state.current_sl
                state.current_sl = state.initial_entry
                state.position_remaining_pct = 50.0
                actions.append({
                    "type": "tp1_action",
                    "old_sl": old_sl,
                    "new_sl": state.current_sl,
                    "msg": (
                        f"🎯 *{symbol} SHORT: TP1 تحقق!*\n\n"
                        f"أخرج 50% + حرّك SL إلى Break-Even"
                    ),
                })

            # TP2 hit
            if (not state.tp2_hit and state.tp1_hit
                and current_price <= state.tp2 and state.tp2 > 0):
                state.tp2_hit = True
                state.current_sl = state.tp1
                state.position_remaining_pct = 20.0
                actions.append({
                    "type": "tp2_action",
                    "msg": f"🎯 *{symbol} SHORT: TP2 تحقق!*\n\nأخرج 30% + حرّك SL إلى TP1",
                })

            # TP3 hit
            if (not state.tp3_hit and state.tp2_hit
                and current_price <= state.tp3 and state.tp3 > 0):
                state.tp3_hit = True
                actions.append({
                    "type": "tp3_action",
                    "msg": f"🏆 *{symbol} SHORT: TP3 تحقق!* أغلق الباقي.",
                    "close": True,
                })
                return {"actions": actions, "should_stop": True}

        return {"actions": actions, "should_stop": False}


# Singleton
trail_mgr = TrailingManager()
