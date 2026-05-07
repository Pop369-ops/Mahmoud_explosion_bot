"""
Daily Risk Limits — protects capital from revenge trading and bad days.

Industry rules (used by prop firms, hedge funds):
- Daily loss limit: 3% of capital → stop for the day
- Weekly loss limit: 8% → stop for the week
- Consecutive losses: 3 in a row → cooldown 4 hours
- Max concurrent positions: 3-5 (avoid over-exposure)

These rules look "limiting" but they're what separates pros from amateurs.
The pros who survived 10+ years all use some form of daily limits.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from core.models import now_riyadh


_TZ_RIYADH = timezone(timedelta(hours=3))


@dataclass
class RiskState:
    """Per-user daily risk tracking."""
    chat_id: int
    capital: float                    # user's reported capital
    daily_pnl_pct: float = 0.0       # cumulative daily P&L %
    weekly_pnl_pct: float = 0.0      # cumulative weekly P&L %
    consecutive_losses: int = 0
    open_trades_count: int = 0

    last_loss_time: Optional[datetime] = None
    daily_reset_at: Optional[datetime] = None  # next midnight Riyadh
    weekly_reset_at: Optional[datetime] = None # next Monday 00:00 Riyadh

    paused_until: Optional[datetime] = None
    pause_reason: Optional[str] = None

    # Limits (configurable per user)
    daily_loss_limit_pct: float = 3.0
    weekly_loss_limit_pct: float = 8.0
    consecutive_loss_limit: int = 3
    cooldown_after_streak_hours: int = 4
    max_concurrent_trades: int = 3


@dataclass
class RiskCheckResult:
    """Result of a 'can I take this trade?' check."""
    allowed: bool
    reason: Optional[str] = None
    paused_until: Optional[datetime] = None
    breakdown: dict = field(default_factory=dict)


class DailyRiskManager:
    """In-memory risk state manager (persisted to DB by storage layer)."""

    def __init__(self):
        self._states: dict[int, RiskState] = {}

    def get(self, chat_id: int) -> RiskState:
        if chat_id not in self._states:
            self._states[chat_id] = RiskState(
                chat_id=chat_id, capital=0.0,
                daily_reset_at=self._next_midnight(),
                weekly_reset_at=self._next_monday(),
            )
        st = self._states[chat_id]
        self._auto_reset(st)
        return st

    def set_capital(self, chat_id: int, capital: float):
        st = self.get(chat_id)
        st.capital = capital

    def can_take_trade(self, chat_id: int) -> RiskCheckResult:
        """Check ALL gates before allowing a new trade."""
        st = self.get(chat_id)
        now = now_riyadh()

        # Sync open_trades_count from actual state (safety net)
        try:
            from core.state import state as user_state
            actual_trades = user_state.get_trades(chat_id)
            st.open_trades_count = len(actual_trades)
        except Exception:
            pass

        if st.capital <= 0:
            return RiskCheckResult(
                allowed=False,
                reason="لم تحدد رأس المال بعد. أرسل /capital 1000 أولاً",
            )

        # Manual pause active?
        if st.paused_until and now < st.paused_until:
            remaining = st.paused_until - now
            mins = int(remaining.total_seconds() / 60)
            return RiskCheckResult(
                allowed=False,
                paused_until=st.paused_until,
                reason=f"البوت متوقف ({st.pause_reason}). متبقي ~{mins} دقيقة.",
            )

        # Daily loss limit
        if st.daily_pnl_pct <= -st.daily_loss_limit_pct:
            return RiskCheckResult(
                allowed=False,
                paused_until=st.daily_reset_at,
                reason=(
                    f"🛑 وصلت لحد الخسارة اليومية "
                    f"({st.daily_pnl_pct:.2f}% / -{st.daily_loss_limit_pct}%). "
                    f"التداول يستأنف عند منتصف الليل بتوقيت السعودية."
                ),
            )

        # Weekly loss limit
        if st.weekly_pnl_pct <= -st.weekly_loss_limit_pct:
            return RiskCheckResult(
                allowed=False,
                paused_until=st.weekly_reset_at,
                reason=(
                    f"🛑 وصلت لحد الخسارة الأسبوعية "
                    f"({st.weekly_pnl_pct:.2f}% / -{st.weekly_loss_limit_pct}%). "
                    f"خذ استراحة وراجع استراتيجيتك."
                ),
            )

        # Consecutive losses → cooldown
        if st.consecutive_losses >= st.consecutive_loss_limit and st.last_loss_time:
            cooldown_end = st.last_loss_time + timedelta(hours=st.cooldown_after_streak_hours)
            if now < cooldown_end:
                remaining = cooldown_end - now
                mins = int(remaining.total_seconds() / 60)
                return RiskCheckResult(
                    allowed=False,
                    paused_until=cooldown_end,
                    reason=(
                        f"⏸ {st.consecutive_losses} خسائر متتالية. "
                        f"استراحة إجبارية {st.cooldown_after_streak_hours} ساعات. "
                        f"متبقي {mins} دقيقة."
                    ),
                )

        # Max concurrent trades
        if st.open_trades_count >= st.max_concurrent_trades:
            return RiskCheckResult(
                allowed=False,
                reason=(
                    f"🚫 لديك {st.open_trades_count} صفقات مفتوحة "
                    f"(الحد الأقصى {st.max_concurrent_trades}).\n\n"
                    f"💡 *الخيارات:*\n"
                    f"• `/trades` - عرض الصفقات المفتوحة\n"
                    f"• `/clear_trades` - مسح كل الصفقات من البوت"
                ),
            )

        return RiskCheckResult(
            allowed=True,
            breakdown={
                "daily_pnl": st.daily_pnl_pct,
                "weekly_pnl": st.weekly_pnl_pct,
                "consecutive_losses": st.consecutive_losses,
                "open_trades": st.open_trades_count,
                "remaining_daily_room_pct": st.daily_loss_limit_pct + st.daily_pnl_pct,
            },
        )

    def record_trade_result(self, chat_id: int, pnl_pct: float):
        """Call when a trade is closed."""
        st = self.get(chat_id)
        st.daily_pnl_pct += pnl_pct
        st.weekly_pnl_pct += pnl_pct
        if pnl_pct < 0:
            st.consecutive_losses += 1
            st.last_loss_time = now_riyadh()
        else:
            st.consecutive_losses = 0
        st.open_trades_count = max(0, st.open_trades_count - 1)

    def record_trade_opened(self, chat_id: int):
        st = self.get(chat_id)
        st.open_trades_count += 1

    def manual_pause(self, chat_id: int, hours: int, reason: str = "manual"):
        st = self.get(chat_id)
        st.paused_until = now_riyadh() + timedelta(hours=hours)
        st.pause_reason = reason

    def manual_resume(self, chat_id: int):
        st = self.get(chat_id)
        st.paused_until = None
        st.pause_reason = None

    def _auto_reset(self, st: RiskState):
        now = now_riyadh()
        if st.daily_reset_at and now >= st.daily_reset_at:
            st.daily_pnl_pct = 0.0
            st.daily_reset_at = self._next_midnight()
        if st.weekly_reset_at and now >= st.weekly_reset_at:
            st.weekly_pnl_pct = 0.0
            st.weekly_reset_at = self._next_monday()

    @staticmethod
    def _next_midnight() -> datetime:
        now = now_riyadh()
        tomorrow = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return tomorrow

    @staticmethod
    def _next_monday() -> datetime:
        now = now_riyadh()
        days_ahead = 7 - now.weekday()
        next_monday = (now + timedelta(days=days_ahead)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return next_monday


# Singleton
risk_manager = DailyRiskManager()
