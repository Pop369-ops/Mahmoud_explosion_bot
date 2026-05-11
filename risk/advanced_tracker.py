"""
Advanced Performance Analytics — the killer feature for self-improvement.

Goes beyond basic win/loss stats. Tracks:

  1. PER-GATE statistics: which Gates 1-23 are rejecting profitable
     signals vs catching losing ones?

  2. PER-DETECTOR contributions: which of the 8 confidence sources
     (volume_delta, order_book, funding, etc.) are most accurate?

  3. PER-METHOD SL performance: ict_premium vs atr_fallback vs
     equal_levels — which SL placement method actually survives?

  4. PER-CONDITION analysis: setups in killzone vs outside, scalp vs
     swing mode, low_volume vs high_volume coins — what's working?

  5. SIGNAL QUALITY DEGRADATION: track if accuracy is dropping over
     time → market regime may have changed.

Outputs actionable recommendations:
  • "Gate 18 (consecutive bars) rejected 7 winners last week — consider lowering threshold"
  • "Setup type 'ict_premium' has 78% win rate, 'atr_fallback' has 41% — auto-reject ATR fallback?"
  • "Killzone London signals: 65% win, NY: 45% win — disable NY?"
"""
import json
import aiosqlite
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
from core.models import now_riyadh
from core.logger import get_logger

log = get_logger(__name__)


# ────────────────────────────────────────────────────────────
# SCHEMA: extended performance log with detailed metadata
# ────────────────────────────────────────────────────────────
ADVANCED_SCHEMA = """
-- Track every signal sent (not just closed trades)
-- This lets us measure rejection accuracy too
CREATE TABLE IF NOT EXISTS signal_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    phase TEXT,                       -- "PRE_EXPLOSION_EARLY" / "EXPLOSION_NOW" / etc
    confidence INTEGER,
    sources_agreed INTEGER,
    sources_total INTEGER,

    -- Entry plan
    entry REAL, sl REAL, tp1 REAL, tp2 REAL, tp3 REAL,
    sl_pct REAL,
    sl_method TEXT,                   -- ict_premium / atr_fallback / etc
    sl_adjustments TEXT,              -- "vol_floor; eq_levels"

    -- Per-source verdicts (JSON)
    verdicts_json TEXT,               -- {"volume_delta": 75, "order_book": 60, ...}

    -- Filter activity
    rejected INTEGER DEFAULT 0,
    rejection_reason TEXT,
    gates_passed TEXT,                -- "1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23"
    gates_failed TEXT,                -- ""

    -- Context at signal time
    change_24h REAL,
    rsi_1h REAL, rsi_4h REAL, rsi_1d REAL,
    in_killzone TEXT,                 -- "london" / "ny" / "asian" / "none"
    btc_trend_score INTEGER,
    market_regime TEXT,               -- "uptrend" / "downtrend" / "ranging"

    sent_at TEXT NOT NULL,

    -- Outcome (filled when trade closes)
    outcome TEXT,                     -- "win_tp1" / "win_tp2" / "win_tp3" / "loss_sl" / "manual" / null
    actual_pnl_pct REAL,
    actual_pnl_r REAL,
    duration_minutes INTEGER,
    closed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_sig_chat_time ON signal_log(chat_id, sent_at);
CREATE INDEX IF NOT EXISTS idx_sig_outcome ON signal_log(outcome);
CREATE INDEX IF NOT EXISTS idx_sig_method ON signal_log(sl_method);

-- Tier B alerts log (awakening + reversal)
CREATE TABLE IF NOT EXISTS tier_b_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    alert_type TEXT NOT NULL,         -- "awakening" / "reversal"
    direction TEXT,
    score INTEGER,
    signals_fired INTEGER,
    price_at_alert REAL,
    sent_at TEXT NOT NULL,

    -- Check what happened in next 1-4 hours
    price_1h_later REAL,
    price_4h_later REAL,
    accurate INTEGER,                 -- 1 if move matched direction, 0 if not
    move_pct_1h REAL,
    move_pct_4h REAL,
    validated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_tb_chat_time ON tier_b_log(chat_id, sent_at);
"""


@dataclass
class SignalRecord:
    """Full record of a signal — sent OR rejected."""
    chat_id: int
    symbol: str
    direction: str
    phase: str
    confidence: int
    sources_agreed: int
    sources_total: int
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    sl_pct: float
    sl_method: str
    sl_adjustments: str
    verdicts_json: str
    rejected: bool
    rejection_reason: str
    gates_passed: str
    gates_failed: str
    change_24h: float
    rsi_1h: float
    rsi_4h: float
    rsi_1d: float
    in_killzone: str
    btc_trend_score: int
    market_regime: str
    sent_at: datetime = field(default_factory=now_riyadh)


@dataclass
class TierBRecord:
    chat_id: int
    symbol: str
    alert_type: str        # "awakening" or "reversal"
    direction: str
    score: int
    signals_fired: int
    price_at_alert: float
    sent_at: datetime = field(default_factory=now_riyadh)


@dataclass
class GateStats:
    """Statistics for a single Gate."""
    gate_id: int
    name_ar: str
    times_triggered: int = 0          # how many times this gate rejected a signal
    rejected_winners: int = 0          # rejected signal that WOULD have won
    rejected_losers: int = 0           # rejected signal that WOULD have lost
    passed_winners: int = 0
    passed_losers: int = 0

    @property
    def precision(self) -> float:
        """Of all rejections, % that were correct (would have lost)."""
        total_rejected = self.rejected_winners + self.rejected_losers
        if total_rejected == 0:
            return 0.0
        return self.rejected_losers / total_rejected * 100

    @property
    def recommendation(self) -> str:
        if self.times_triggered < 5:
            return "بيانات قليلة"
        if self.precision >= 70:
            return "✅ يعمل بكفاءة"
        elif self.precision >= 50:
            return "⚠️ متوسط — راقب"
        else:
            return "❌ يرفض رابحين كثيراً — قلّل الحساسية"


@dataclass
class MethodStats:
    """Statistics for an SL method."""
    method: str
    total: int = 0
    winners: int = 0
    losers: int = 0
    avg_pnl_pct: float = 0.0
    avg_duration_min: float = 0.0

    @property
    def win_rate(self) -> float:
        return (self.winners / self.total * 100) if self.total > 0 else 0


@dataclass
class DetectorStats:
    """Per-detector contribution analysis."""
    name: str
    total_signals: int = 0
    when_high_score: dict = field(default_factory=dict)  # {"winners": N, "losers": N}
    when_low_score: dict = field(default_factory=dict)

    @property
    def high_score_win_rate(self) -> float:
        h = self.when_high_score
        total = h.get("winners", 0) + h.get("losers", 0)
        return (h.get("winners", 0) / total * 100) if total > 0 else 0


class AdvancedTracker:
    """Logs every signal + computes deep analytics."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(ADVANCED_SCHEMA)
            await db.commit()

    # ════════════════════════════════════════════════════════
    # RECORDING
    # ════════════════════════════════════════════════════════
    async def log_signal(self, rec: SignalRecord) -> int:
        """Record a signal (sent or rejected). Returns insert id."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("""
                    INSERT INTO signal_log
                    (chat_id, symbol, direction, phase, confidence, sources_agreed,
                     sources_total, entry, sl, tp1, tp2, tp3, sl_pct, sl_method,
                     sl_adjustments, verdicts_json, rejected, rejection_reason,
                     gates_passed, gates_failed, change_24h, rsi_1h, rsi_4h,
                     rsi_1d, in_killzone, btc_trend_score, market_regime, sent_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    rec.chat_id, rec.symbol, rec.direction, rec.phase,
                    rec.confidence, rec.sources_agreed, rec.sources_total,
                    rec.entry, rec.sl, rec.tp1, rec.tp2, rec.tp3, rec.sl_pct,
                    rec.sl_method, rec.sl_adjustments, rec.verdicts_json,
                    1 if rec.rejected else 0, rec.rejection_reason,
                    rec.gates_passed, rec.gates_failed, rec.change_24h,
                    rec.rsi_1h, rec.rsi_4h, rec.rsi_1d, rec.in_killzone,
                    rec.btc_trend_score, rec.market_regime,
                    rec.sent_at.isoformat(),
                ))
                await db.commit()
                return cursor.lastrowid or 0
        except Exception as e:
            log.warning("log_signal_err", err=str(e))
            return 0

    async def update_outcome(self, signal_id: int, outcome: str,
                                actual_pnl_pct: float, actual_pnl_r: float,
                                duration_minutes: int):
        """Update a signal record when its trade closes."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    UPDATE signal_log SET
                        outcome = ?, actual_pnl_pct = ?, actual_pnl_r = ?,
                        duration_minutes = ?, closed_at = ?
                    WHERE id = ?
                """, (outcome, actual_pnl_pct, actual_pnl_r, duration_minutes,
                       now_riyadh().isoformat(), signal_id))
                await db.commit()
        except Exception as e:
            log.warning("update_outcome_err", err=str(e))

    async def find_open_signal(self, chat_id: int, symbol: str) -> Optional[int]:
        """Find the most recent signal_log id for an open trade (no outcome yet)."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("""
                    SELECT id FROM signal_log
                    WHERE chat_id = ? AND symbol = ? AND rejected = 0
                          AND outcome IS NULL
                    ORDER BY sent_at DESC LIMIT 1
                """, (chat_id, symbol))
                row = await cursor.fetchone()
                return row[0] if row else None
        except Exception as e:
            log.warning("find_open_err", err=str(e))
            return None

    async def log_tier_b(self, rec: TierBRecord) -> int:
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("""
                    INSERT INTO tier_b_log
                    (chat_id, symbol, alert_type, direction, score, signals_fired,
                     price_at_alert, sent_at)
                    VALUES (?,?,?,?,?,?,?,?)
                """, (rec.chat_id, rec.symbol, rec.alert_type, rec.direction,
                       rec.score, rec.signals_fired, rec.price_at_alert,
                       rec.sent_at.isoformat()))
                await db.commit()
                return cursor.lastrowid or 0
        except Exception as e:
            log.warning("log_tier_b_err", err=str(e))
            return 0

    async def validate_tier_b(self, alert_id: int, current_price: float,
                                hours_passed: float):
        """Update tier_b record with actual outcome."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                # Get the alert
                cursor = await db.execute("""
                    SELECT direction, price_at_alert FROM tier_b_log WHERE id = ?
                """, (alert_id,))
                row = await cursor.fetchone()
                if not row:
                    return
                direction, price_alert = row
                if price_alert <= 0:
                    return

                move_pct = (current_price - price_alert) / price_alert * 100

                # Determine accuracy
                accurate = 0
                if direction in ("long_likely", "bullish_reversal"):
                    accurate = 1 if move_pct > 0.5 else 0
                elif direction in ("short_likely", "bearish_reversal"):
                    accurate = 1 if move_pct < -0.5 else 0

                # Update which column?
                if hours_passed <= 1.5:
                    await db.execute("""
                        UPDATE tier_b_log SET price_1h_later = ?, move_pct_1h = ?
                        WHERE id = ?
                    """, (current_price, move_pct, alert_id))
                if hours_passed >= 3.5:
                    await db.execute("""
                        UPDATE tier_b_log SET price_4h_later = ?, move_pct_4h = ?,
                                              accurate = ?, validated_at = ?
                        WHERE id = ?
                    """, (current_price, move_pct, accurate,
                           now_riyadh().isoformat(), alert_id))
                await db.commit()
        except Exception as e:
            log.warning("validate_tier_b_err", err=str(e))

    # ════════════════════════════════════════════════════════
    # ANALYTICS
    # ════════════════════════════════════════════════════════
    async def get_method_stats(self, chat_id: int,
                                 days: int = 30) -> list[MethodStats]:
        """Performance breakdown by SL method."""
        cutoff = (now_riyadh() - timedelta(days=days)).isoformat()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("""
                    SELECT sl_method,
                           COUNT(*) as total,
                           SUM(CASE WHEN actual_pnl_pct > 0 THEN 1 ELSE 0 END) as winners,
                           SUM(CASE WHEN actual_pnl_pct <= 0 THEN 1 ELSE 0 END) as losers,
                           AVG(actual_pnl_pct) as avg_pnl,
                           AVG(duration_minutes) as avg_dur
                    FROM signal_log
                    WHERE chat_id = ? AND rejected = 0
                          AND outcome IS NOT NULL
                          AND sent_at >= ?
                    GROUP BY sl_method
                    ORDER BY total DESC
                """, (chat_id, cutoff))
                rows = await cursor.fetchall()
                return [
                    MethodStats(
                        method=r[0] or "unknown",
                        total=r[1] or 0,
                        winners=r[2] or 0,
                        losers=r[3] or 0,
                        avg_pnl_pct=r[4] or 0.0,
                        avg_duration_min=r[5] or 0.0,
                    ) for r in rows
                ]
        except Exception as e:
            log.warning("method_stats_err", err=str(e))
            return []

    async def get_killzone_stats(self, chat_id: int, days: int = 30) -> dict:
        """Win rate by killzone."""
        cutoff = (now_riyadh() - timedelta(days=days)).isoformat()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("""
                    SELECT in_killzone,
                           COUNT(*) as total,
                           SUM(CASE WHEN actual_pnl_pct > 0 THEN 1 ELSE 0 END) as winners,
                           AVG(actual_pnl_pct) as avg_pnl
                    FROM signal_log
                    WHERE chat_id = ? AND rejected = 0
                          AND outcome IS NOT NULL
                          AND sent_at >= ?
                    GROUP BY in_killzone
                """, (chat_id, cutoff))
                rows = await cursor.fetchall()
                return {
                    (r[0] or "none"): {
                        "total": r[1], "winners": r[2],
                        "win_rate": (r[2] / r[1] * 100) if r[1] > 0 else 0,
                        "avg_pnl": r[3] or 0.0,
                    } for r in rows
                }
        except Exception as e:
            log.warning("killzone_stats_err", err=str(e))
            return {}

    async def get_direction_stats(self, chat_id: int, days: int = 30) -> dict:
        """Long vs Short performance."""
        cutoff = (now_riyadh() - timedelta(days=days)).isoformat()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("""
                    SELECT direction,
                           COUNT(*) as total,
                           SUM(CASE WHEN actual_pnl_pct > 0 THEN 1 ELSE 0 END) as winners,
                           AVG(actual_pnl_pct) as avg_pnl,
                           MAX(actual_pnl_pct) as best,
                           MIN(actual_pnl_pct) as worst
                    FROM signal_log
                    WHERE chat_id = ? AND rejected = 0
                          AND outcome IS NOT NULL
                          AND sent_at >= ?
                    GROUP BY direction
                """, (chat_id, cutoff))
                rows = await cursor.fetchall()
                return {r[0]: {
                    "total": r[1], "winners": r[2],
                    "win_rate": (r[2]/r[1]*100) if r[1] > 0 else 0,
                    "avg_pnl": r[3] or 0.0,
                    "best": r[4] or 0.0, "worst": r[5] or 0.0,
                } for r in rows}
        except Exception as e:
            return {}

    async def get_tier_b_accuracy(self, chat_id: int, days: int = 30) -> dict:
        """How accurate are Tier B alerts?"""
        cutoff = (now_riyadh() - timedelta(days=days)).isoformat()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("""
                    SELECT alert_type,
                           COUNT(*) as total,
                           SUM(CASE WHEN accurate = 1 THEN 1 ELSE 0 END) as accurate,
                           AVG(move_pct_4h) as avg_move
                    FROM tier_b_log
                    WHERE chat_id = ? AND validated_at IS NOT NULL
                          AND sent_at >= ?
                    GROUP BY alert_type
                """, (chat_id, cutoff))
                rows = await cursor.fetchall()
                return {r[0]: {
                    "total": r[1],
                    "accurate": r[2],
                    "accuracy_pct": (r[2]/r[1]*100) if r[1] > 0 else 0,
                    "avg_move_pct": r[3] or 0.0,
                } for r in rows}
        except Exception as e:
            return {}

    async def get_confidence_buckets(self, chat_id: int, days: int = 30) -> dict:
        """Win rate by confidence bucket."""
        cutoff = (now_riyadh() - timedelta(days=days)).isoformat()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("""
                    SELECT
                        CASE
                            WHEN confidence >= 85 THEN '85-100'
                            WHEN confidence >= 75 THEN '75-84'
                            WHEN confidence >= 65 THEN '65-74'
                            ELSE 'below_65'
                        END as bucket,
                        COUNT(*) as total,
                        SUM(CASE WHEN actual_pnl_pct > 0 THEN 1 ELSE 0 END) as winners,
                        AVG(actual_pnl_pct) as avg_pnl
                    FROM signal_log
                    WHERE chat_id = ? AND rejected = 0
                          AND outcome IS NOT NULL
                          AND sent_at >= ?
                    GROUP BY bucket
                    ORDER BY bucket DESC
                """, (chat_id, cutoff))
                rows = await cursor.fetchall()
                return {r[0]: {
                    "total": r[1], "winners": r[2],
                    "win_rate": (r[2]/r[1]*100) if r[1] > 0 else 0,
                    "avg_pnl": r[3] or 0.0,
                } for r in rows}
        except Exception as e:
            return {}

    async def get_rejection_summary(self, chat_id: int, days: int = 7) -> dict:
        """How many signals were rejected, by which Gates?"""
        cutoff = (now_riyadh() - timedelta(days=days)).isoformat()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("""
                    SELECT rejection_reason, COUNT(*) as cnt
                    FROM signal_log
                    WHERE chat_id = ? AND rejected = 1 AND sent_at >= ?
                    GROUP BY rejection_reason
                    ORDER BY cnt DESC LIMIT 15
                """, (chat_id, cutoff))
                rows = await cursor.fetchall()
                return {r[0]: r[1] for r in rows if r[0]}
        except Exception as e:
            return {}

    async def get_overall_summary(self, chat_id: int, days: int = 30) -> dict:
        """Overall stats: total signals, accepted, rejected, win rate."""
        cutoff = (now_riyadh() - timedelta(days=days)).isoformat()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("""
                    SELECT
                        COUNT(*) as total_signals,
                        SUM(CASE WHEN rejected = 1 THEN 1 ELSE 0 END) as rejected,
                        SUM(CASE WHEN rejected = 0 AND outcome IS NOT NULL THEN 1 ELSE 0 END) as closed,
                        SUM(CASE WHEN actual_pnl_pct > 0 THEN 1 ELSE 0 END) as winners,
                        AVG(CASE WHEN actual_pnl_pct IS NOT NULL THEN actual_pnl_pct END) as avg_pnl,
                        SUM(CASE WHEN actual_pnl_pct IS NOT NULL THEN actual_pnl_pct END) as total_pnl
                    FROM signal_log
                    WHERE chat_id = ? AND sent_at >= ?
                """, (chat_id, cutoff))
                row = await cursor.fetchone()
                if not row:
                    return {}
                total = row[0] or 0
                rejected = row[1] or 0
                closed = row[2] or 0
                winners = row[3] or 0
                return {
                    "total_signals": total,
                    "rejected_count": rejected,
                    "accepted_count": total - rejected,
                    "closed_count": closed,
                    "winners": winners,
                    "losers": closed - winners,
                    "win_rate": (winners / closed * 100) if closed > 0 else 0,
                    "avg_pnl_pct": row[4] or 0.0,
                    "total_pnl_pct": row[5] or 0.0,
                }
        except Exception as e:
            log.warning("overall_summary_err", err=str(e))
            return {}


# Singleton — initialized at app startup
advanced_tracker: Optional[AdvancedTracker] = None


def init_advanced_tracker(db_path: str):
    global advanced_tracker
    advanced_tracker = AdvancedTracker(db_path)
    return advanced_tracker
