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
    klines: Optional[object] = None       # 5m base
    klines_15m: Optional[object] = None
    klines_1h: Optional[object] = None
    klines_4h: Optional[object] = None    # NEW: ICT bias
    klines_1d: Optional[object] = None    # NEW: ICT bias
    order_book: Optional[dict] = None
    funding_rate: Optional[float] = None
    open_interest: Optional[float] = None
    whale_flow: Optional[dict] = None
    htf_bias: Optional[object] = None     # MultiTFBias (lazy fill)
    killzone: Optional[object] = None     # KillzoneInfo (lazy fill)
    volume_profile: Optional[object] = None
    order_flow: Optional[object] = None
    liquidation_zones: Optional[list] = None


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
    direction: str = "long"   # "long" or "short"
    exhaustion_summary: Optional[str] = None

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
    direction: str = "long"
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


@dataclass
class LiquiditySignal:
    """Single awakening signal output from a liquidity buildup detector."""
    name: str
    triggered: bool
    score: int           # 0..100 contribution if triggered
    reason_ar: str       # short Arabic explanation
    raw: dict = field(default_factory=dict)


@dataclass
class WatchListEntry:
    """A coin currently in 'coiled' state — ripe for awakening."""
    symbol: str
    added_at: datetime = field(default_factory=now_riyadh)
    last_price: float = 0.0
    atr_1h_pct: float = 0.0
    vol_ratio_1h: float = 0.0   # current vs avg
    quiet_hours: float = 0.0    # how long it's been quiet
    reason: str = ""

    def age_minutes(self) -> float:
        return (now_riyadh() - self.added_at).total_seconds() / 60


@dataclass
class AwakeningAlert:
    """Tier-B early warning — awakening detected, NOT a trade signal."""
    symbol: str
    direction: str                     # "long_likely", "short_likely", "unknown"
    awakening_score: int               # 0..100
    signals_fired: int                 # how many of the 6 detectors triggered
    price: float
    change_15m: float                  # short-term move
    signals: list[LiquiditySignal] = field(default_factory=list)
    timestamp: datetime = field(default_factory=now_riyadh)
    expected_quality: str = "تنبيه مبكر — تأكيد مطلوب"

    @property
    def signal_names(self) -> list[str]:
        return [s.name for s in self.signals if s.triggered]


def fmt(v) -> str:
    if v is None: return "—"
    try: v = float(v)
    except (TypeError, ValueError): return "—"
    if v >= 1e9: return f"{v/1e9:.2f}B"
    if v >= 1e6: return f"{v/1e6:.2f}M"
    if v >= 1e3: return f"{v/1e3:.1f}K"
    if v >= 1: return f"{v:,.4f}"
    return f"{v:.8f}"
