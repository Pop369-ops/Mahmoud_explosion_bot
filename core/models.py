"""Core domain models — dataclasses + enums."""
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timezone, timedelta
from enum import Enum

_TZ_RIYADH = timezone(timedelta(hours=3))


def now_riyadh() -> datetime:
    return datetime.now(_TZ_RIYADH)


def now_riyadh_str() -> str:
    return now_riyadh().strftime("%H:%M:%S %d/%m/%Y")


class Phase(str, Enum):
    PRE_EXPLOSION_EARLY = "pre_explosion_early"
    PRE_EXPLOSION = "pre_explosion"
    EXPLOSION_START = "explosion_start"
    EXPLOSION_STRONG = "explosion_strong"
    EXPLOSION_NOW = "explosion_now"
    EXPLOSION_PREMIUM = "explosion_premium"
    REJECTED = "rejected"
    NONE = "none"


PHASE_LABELS = {
    Phase.PRE_EXPLOSION_EARLY: "🌱 تراكم مبكر — قبل الانفجار",
    Phase.PRE_EXPLOSION: "⏳ على وشك الانفجار",
    Phase.EXPLOSION_START: "⚡ بدء الانفجار",
    Phase.EXPLOSION_STRONG: "⚡⚡ بداية انفجار قوية",
    Phase.EXPLOSION_NOW: "🚨 انفجار الآن!",
    Phase.EXPLOSION_PREMIUM: "💎 إشارة فاخرة (دقة عالية)",
    Phase.REJECTED: "🚫 مرفوضة",
    Phase.NONE: "—",
}

PHASE_ICONS = {
    Phase.PRE_EXPLOSION_EARLY: "🌱",
    Phase.PRE_EXPLOSION: "⏳",
    Phase.EXPLOSION_START: "⚡",
    Phase.EXPLOSION_STRONG: "⚡⚡",
    Phase.EXPLOSION_NOW: "🚨",
    Phase.EXPLOSION_PREMIUM: "💎",
}


class Mode(str, Enum):
    SCALP = "scalp"
    DAY = "day"
    SWING = "swing"


@dataclass
class SourceVerdict:
    name: str
    score: int
    confidence: float
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @property
    def agrees(self) -> bool:
        return self.score >= 60


@dataclass
class MarketSnapshot:
    symbol: str
    price: float
    volume_24h: float
    change_24h: float
    timestamp: datetime = field(default_factory=now_riyadh)
    klines: Optional[object] = None
    klines_15m: Optional[object] = None
    klines_1h: Optional[object] = None
    order_book: Optional[dict] = None
    funding_rate: Optional[float] = None
    open_interest: Optional[float] = None
    whale_flow: Optional[dict] = None


@dataclass
class Signal:
    symbol: str
    phase: Phase
    confidence: int
    sources_agreed: int
    sources_total: int
    price: float
    change_24h: float
    volume_24h: float
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    sl_pct: float
    atr: float
    mode: Mode
    rsi: Optional[float] = None
    vol_ratio: Optional[float] = None
    timestamp: datetime = field(default_factory=now_riyadh)
    verdicts: list[SourceVerdict] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rejected_reason: Optional[str] = None

    @property
    def quality_label(self) -> str:
        if self.sources_agreed >= 5: return "ممتازة (~75-85%)"
        if self.sources_agreed >= 4: return "جيدة (~65-75%)"
        if self.sources_agreed >= 3: return "متوسطة (~55-65%)"
        return "ضعيفة"


@dataclass
class Trade:
    chat_id: int
    symbol: str
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    confidence: int
    phase: Phase
    mode: Mode
    opened_at: datetime = field(default_factory=now_riyadh)
    peak_price: float = 0.0
    tp1_hit: bool = False
    tp2_hit: bool = False
    closed: bool = False
    close_reason: Optional[str] = None
    close_price: Optional[float] = None

    @property
    def pnl_pct(self) -> float:
        if self.entry <= 0: return 0
        ref = self.close_price if self.closed else self.peak_price or self.entry
        return (ref - self.entry) / self.entry * 100


def fmt(v) -> str:
    if v is None: return "—"
    try: v = float(v)
    except (TypeError, ValueError): return "—"
    if v >= 1e9: return f"{v/1e9:.2f}B"
    if v >= 1e6: return f"{v/1e6:.2f}M"
    if v >= 1e3: return f"{v/1e3:.1f}K"
    if v >= 1: return f"{v:,.4f}"
    return f"{v:.8f}"
