"""
Performance Tracking — measures what actually works for YOUR trading.

Records every closed trade and computes:
- Win rate (overall + per setup type)
- Profit factor (gross profit / gross loss)
- Average R-multiple (R = SL distance — universal trade unit)
- Expectancy: avg($ won per trade)
- Max drawdown
- Best/worst setups (which ICT pattern wins most)

The killer feature: identifies WHAT WORKS for the user specifically.
"""
import json
import aiosqlite
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
from core.models import now_riyadh
from core.logger import get_logger

log = get_logger(__name__)


@dataclass
class TradeRecord:
    chat_id: int
    symbol: str
    direction: str             # "long" or "short"
    setup_type: str            # "ict_premium" | "ict_standard" | "liquidity" | "atr_fallback"
    entry: float
    sl: float
    tp1: float
    exit_price: float
    pnl_pct: float
    pnl_r: float              # how many R units (R = SL distance)
    confidence: int
    sources_agreed: int
    duration_minutes: int
    closed_at: datetime
    close_reason: str         # "TP1", "TP2", "TP3", "SL", "manual", "trailing"


@dataclass
class PerformanceStats:
    period_label: str          # "Today" / "This Week" / "All Time"
    total_trades: int = 0
    winners: int = 0
    losers: int = 0
    win_rate: float = 0.0
    total_pnl_pct: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    profit_factor: float = 0.0     # gross_profit / abs(gross_loss)
    expectancy: float = 0.0        # avg pnl per trade
    avg_r_multiple: float = 0.0
    best_trade_pct: float = 0.0
    worst_trade_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    avg_duration_min: float = 0.0
    by_setup: dict = field(default_factory=dict)   # setup_type → mini stats
    by_direction: dict = field(default_factory=dict)


SCHEMA = """
CREATE TABLE IF NOT EXISTS performance_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    setup_type TEXT,
    entry REAL, sl REAL, tp1 REAL,
    exit_price REAL,
    pnl_pct REAL,
    pnl_r REAL,
    confidence INTEGER,
    sources_agreed INTEGER,
    duration_minutes INTEGER,
    closed_at TEXT NOT NULL,
    close_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_perf_chat_time
    ON performance_log(chat_id, closed_at);
"""


class PerformanceTracker:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    async def record(self, rec: TradeRecord):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO performance_log
                (chat_id, symbol, direction, setup_type, entry, sl, tp1,
                 exit_price, pnl_pct, pnl_r, confidence, sources_agreed,
                 duration_minutes, closed_at, close_reason)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rec.chat_id, rec.symbol, rec.direction, rec.setup_type,
                 rec.entry, rec.sl, rec.tp1, rec.exit_price, rec.pnl_pct,
                 rec.pnl_r, rec.confidence, rec.sources_agreed,
                 rec.duration_minutes, rec.closed_at.isoformat(), rec.close_reason),
            )
            await db.commit()

    async def get_stats(self, chat_id: int, period: str = "all") -> PerformanceStats:
        """period: 'today' | 'week' | 'month' | 'all'"""
        now = now_riyadh()
        if period == "today":
            since = now.replace(hour=0, minute=0, second=0, microsecond=0)
            label = "اليوم"
        elif period == "week":
            since = now - timedelta(days=7)
            label = "آخر 7 أيام"
        elif period == "month":
            since = now - timedelta(days=30)
            label = "آخر 30 يوم"
        else:
            since = None
            label = "كل الأوقات"

        query = "SELECT * FROM performance_log WHERE chat_id = ?"
        params = [chat_id]
        if since:
            query += " AND closed_at >= ?"
            params.append(since.isoformat())
        query += " ORDER BY closed_at ASC"

        rows = []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, params) as cur:
                async for row in cur:
                    rows.append(dict(row))

        return self._compute_stats(rows, label)

    def _compute_stats(self, rows: list, label: str) -> PerformanceStats:
        s = PerformanceStats(period_label=label, total_trades=len(rows))
        if not rows:
            return s

        wins = [r for r in rows if r["pnl_pct"] > 0]
        losses = [r for r in rows if r["pnl_pct"] <= 0]

        s.winners = len(wins)
        s.losers = len(losses)
        s.win_rate = round(s.winners / s.total_trades * 100, 1)

        gross_profit = sum(r["pnl_pct"] for r in wins)
        gross_loss = sum(r["pnl_pct"] for r in losses)
        s.total_pnl_pct = round(gross_profit + gross_loss, 2)

        s.avg_win_pct = round(gross_profit / max(1, len(wins)), 2)
        s.avg_loss_pct = round(gross_loss / max(1, len(losses)), 2)

        if gross_loss < 0:
            s.profit_factor = round(gross_profit / abs(gross_loss), 2)

        s.expectancy = round(s.total_pnl_pct / s.total_trades, 2)

        s.avg_r_multiple = round(
            sum(r["pnl_r"] or 0 for r in rows) / len(rows), 2
        )

        s.best_trade_pct = round(max(r["pnl_pct"] for r in rows), 2)
        s.worst_trade_pct = round(min(r["pnl_pct"] for r in rows), 2)

        # Max drawdown — running peak-to-trough on cumulative P&L
        cum = 0.0
        peak = 0.0
        max_dd = 0.0
        for r in rows:
            cum += r["pnl_pct"]
            if cum > peak:
                peak = cum
            dd = peak - cum
            if dd > max_dd:
                max_dd = dd
        s.max_drawdown_pct = round(max_dd, 2)

        durations = [r["duration_minutes"] for r in rows if r["duration_minutes"]]
        if durations:
            s.avg_duration_min = round(sum(durations) / len(durations), 0)

        # By setup type
        setups = {}
        for r in rows:
            setup = r.get("setup_type") or "unknown"
            if setup not in setups:
                setups[setup] = {"total": 0, "wins": 0, "pnl": 0.0}
            setups[setup]["total"] += 1
            if r["pnl_pct"] > 0:
                setups[setup]["wins"] += 1
            setups[setup]["pnl"] += r["pnl_pct"]
        for setup, d in setups.items():
            d["win_rate"] = round(d["wins"] / d["total"] * 100, 1)
            d["pnl"] = round(d["pnl"], 2)
        s.by_setup = setups

        # By direction
        for direction in ("long", "short"):
            sub = [r for r in rows if r["direction"] == direction]
            if sub:
                w = len([r for r in sub if r["pnl_pct"] > 0])
                s.by_direction[direction] = {
                    "total": len(sub),
                    "wins": w,
                    "win_rate": round(w / len(sub) * 100, 1),
                    "pnl": round(sum(r["pnl_pct"] for r in sub), 2),
                }
        return s


def render_stats_message(stats: PerformanceStats) -> str:
    """Format stats for Telegram display."""
    if stats.total_trades == 0:
        return f"📊 *الأداء — {stats.period_label}*\n\n_لا توجد صفقات مغلقة بعد._"

    overall_emoji = "🟢" if stats.total_pnl_pct >= 0 else "🔴"
    pf_emoji = "🟢" if stats.profit_factor >= 1.5 else "🟡" if stats.profit_factor >= 1 else "🔴"

    m = f"📊 *الأداء — {stats.period_label}*\n"
    m += "━━━━━━━━━━━━━━━━━━━━\n\n"
    m += f"{overall_emoji} *المجموع:* `{stats.total_pnl_pct:+.2f}%`\n"
    m += f"📈 *الصفقات:* {stats.total_trades} ({stats.winners}✅ / {stats.losers}❌)\n"
    m += f"🎯 *Win Rate:* `{stats.win_rate}%`\n\n"

    m += "*📐 المؤشرات:*\n"
    m += f"  • Avg Win: `+{stats.avg_win_pct:.2f}%`\n"
    m += f"  • Avg Loss: `{stats.avg_loss_pct:.2f}%`\n"
    m += f"  • {pf_emoji} Profit Factor: `{stats.profit_factor:.2f}`\n"
    m += f"  • Expectancy: `{stats.expectancy:+.2f}% per trade`\n"
    m += f"  • Avg R-multiple: `{stats.avg_r_multiple:+.2f}R`\n"
    m += f"  • Max Drawdown: `{stats.max_drawdown_pct:.2f}%`\n"
    m += f"  • Best: `+{stats.best_trade_pct:.2f}%` | Worst: `{stats.worst_trade_pct:.2f}%`\n\n"

    if stats.by_setup:
        m += "*🎯 حسب نوع الـ Setup:*\n"
        for setup, d in sorted(stats.by_setup.items(),
                                key=lambda x: x[1]["pnl"], reverse=True):
            emoji = "🟢" if d["pnl"] >= 0 else "🔴"
            label_ar = {
                "ict_premium": "ICT Premium (Sweep/OB+BOS)",
                "ict_standard": "ICT Standard (OB/FVG)",
                "liquidity": "Swing/Wall liquidity",
                "atr_fallback": "ATR fallback",
            }.get(setup, setup)
            m += f"  {emoji} {label_ar}: {d['total']} | {d['win_rate']}% | `{d['pnl']:+.2f}%`\n"
        m += "\n"

    if stats.by_direction:
        m += "*↕️ حسب الاتجاه:*\n"
        for direction, d in stats.by_direction.items():
            arrow = "🟢⬆️" if direction == "long" else "🔴⬇️"
            m += f"  {arrow} {direction}: {d['total']} | {d['win_rate']}% | `{d['pnl']:+.2f}%`\n"

    if stats.avg_duration_min:
        hours = int(stats.avg_duration_min // 60)
        mins = int(stats.avg_duration_min % 60)
        m += f"\n⏱ متوسط مدة الصفقة: {hours}h {mins}m"

    return m
