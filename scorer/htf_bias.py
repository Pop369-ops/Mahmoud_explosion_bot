"""
Multi-Timeframe Bias Analyzer

Philosophy: A high-probability trade requires alignment across timeframes:
- Higher TF (Daily, 4H) = the BIAS — defines the dominant direction
- Mid TF (1H) = the SETUP — where we look for entry zones
- Lower TF (15m, 5m) = the TIMING — exact entry trigger

Rules enforced:
- LONG: requires Daily+4H to be bullish OR neutral. Bearish higher TF = REJECT.
- SHORT: requires Daily+4H bearish OR neutral. Bullish higher TF = REJECT.
- Counter-trend trades allowed only on confirmed CHoCH on 1H + sweep on lower TF.

Returns a "MultiTFBias" object with full breakdown.
"""
import asyncio
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional
from data_sources.binance import binance
from scorer.structure import analyze_structure, StructureState
from core.logger import get_logger

log = get_logger(__name__)


@dataclass
class MultiTFBias:
    daily: Optional[StructureState] = None
    h4: Optional[StructureState] = None
    h1: Optional[StructureState] = None
    m15: Optional[StructureState] = None

    bullish_score: int = 0     # weighted -100..+100
    aligned_long: bool = False
    aligned_short: bool = False
    counter_trend_setup: bool = False  # CHoCH on 1H against HTF
    summary: str = ""
    warnings: list = field(default_factory=list)
    rejection_reason: Optional[str] = None  # if signal must be rejected

    def trend_emoji(self, state: Optional[StructureState]) -> str:
        if state is None: return "❓"
        return {"bullish": "🟢", "bearish": "🔴", "ranging": "⚪"}.get(state.trend, "⚪")

    def render_breakdown(self) -> str:
        rows = []
        for label, st in [("اليومي", self.daily), ("4 ساعات", self.h4),
                          ("1 ساعة", self.h1), ("15 دقيقة", self.m15)]:
            if st is None:
                rows.append(f"  {self.trend_emoji(None)} {label}: غير متوفر")
            else:
                trend_ar = {"bullish": "صاعد", "bearish": "هابط", "ranging": "عرضي"}.get(st.trend, "—")
                zone_ar = {"premium": "premium 🔴", "discount": "discount 🟢",
                            "equilibrium": "equilibrium ⚪"}.get(st.zone, "")
                rows.append(f"  {self.trend_emoji(st)} {label}: {trend_ar} ({zone_ar})")
        return "\n".join(rows)


async def fetch_htf_klines(symbol: str) -> dict:
    """Fetch all higher timeframes in parallel."""
    tasks = {
        "1d": binance.fetch_klines(symbol, "1d", 60),
        "4h": binance.fetch_klines(symbol, "4h", 80),
        "1h": binance.fetch_klines(symbol, "1h", 80),
        "15m": binance.fetch_klines(symbol, "15m", 80),
    }
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    return {
        tf: (r if not isinstance(r, Exception) else None)
        for tf, r in zip(tasks.keys(), results)
    }


def compute_bias(htf_data: dict, current_price: float,
                  proposed_direction: str = "long") -> MultiTFBias:
    """Compute the full multi-TF bias from pre-fetched klines.
    Caller responsibility: pre-fetch via fetch_htf_klines().
    """
    bias = MultiTFBias()

    # Analyze each TF
    if htf_data.get("1d") is not None:
        bias.daily = analyze_structure(htf_data["1d"], lookback=40)
    if htf_data.get("4h") is not None:
        bias.h4 = analyze_structure(htf_data["4h"], lookback=50)
    if htf_data.get("1h") is not None:
        bias.h1 = analyze_structure(htf_data["1h"], lookback=60)
    if htf_data.get("15m") is not None:
        bias.m15 = analyze_structure(htf_data["15m"], lookback=60)

    # Weighted scoring (Daily heaviest)
    weights = {"daily": 0.40, "h4": 0.30, "h1": 0.20, "m15": 0.10}
    score_sum = 0.0
    weight_sum = 0.0
    for name, w in weights.items():
        st = getattr(bias, name)
        if st:
            score_sum += st.bullish_score * w
            weight_sum += w
    bias.bullish_score = int(score_sum / weight_sum) if weight_sum > 0 else 0

    # ─── Alignment check ────────────────────────────────
    daily_ok_long = bias.daily is None or bias.daily.trend in ("bullish", "ranging")
    h4_ok_long = bias.h4 is None or bias.h4.trend in ("bullish", "ranging")
    daily_ok_short = bias.daily is None or bias.daily.trend in ("bearish", "ranging")
    h4_ok_short = bias.h4 is None or bias.h4.trend in ("bearish", "ranging")

    bias.aligned_long = daily_ok_long and h4_ok_long and bias.bullish_score > -10
    bias.aligned_short = daily_ok_short and h4_ok_short and bias.bullish_score < 10

    # ─── Counter-trend (CHoCH on 1H against HTF) ────────
    if bias.h1 and bias.h1.last_event == "CHoCH_bullish" and not bias.aligned_long:
        bias.counter_trend_setup = True
    if bias.h1 and bias.h1.last_event == "CHoCH_bearish" and not bias.aligned_short:
        bias.counter_trend_setup = True

    # ─── Rejection logic ────────────────────────────────
    if proposed_direction == "long":
        if bias.daily and bias.daily.trend == "bearish" and bias.bullish_score < -30:
            if not (bias.h1 and bias.h1.last_event == "CHoCH_bullish"):
                bias.rejection_reason = (
                    f"اتجاه يومي هابط (score: {bias.bullish_score}) — "
                    f"الشراء يخالف الاتجاه الكبير"
                )
        elif bias.h4 and bias.h4.trend == "bearish" and bias.h4.bullish_score < -40:
            bias.warnings.append(
                f"⚠️ اتجاه 4H هابط — صفقة شراء contrarian (احذر)"
            )
        if bias.daily and bias.daily.is_premium and bias.bullish_score < 30:
            bias.warnings.append(
                "⚠️ السعر في منطقة premium على اليومي — شراء مكلف"
            )

    elif proposed_direction == "short":
        if bias.daily and bias.daily.trend == "bullish" and bias.bullish_score > 30:
            if not (bias.h1 and bias.h1.last_event == "CHoCH_bearish"):
                bias.rejection_reason = (
                    f"اتجاه يومي صاعد (score: {bias.bullish_score}) — "
                    f"البيع يخالف الاتجاه الكبير"
                )
        elif bias.h4 and bias.h4.trend == "bullish" and bias.h4.bullish_score > 40:
            bias.warnings.append("⚠️ اتجاه 4H صاعد — صفقة بيع contrarian")
        if bias.daily and bias.daily.is_discount and bias.bullish_score > -30:
            bias.warnings.append("⚠️ السعر في منطقة discount على اليومي — بيع متأخر")

    # ─── Summary ────────────────────────────────────────
    if bias.aligned_long:
        bias.summary = f"✅ توافق صاعد كامل (score: +{bias.bullish_score})"
    elif bias.aligned_short:
        bias.summary = f"✅ توافق هابط كامل (score: {bias.bullish_score})"
    elif bias.counter_trend_setup:
        bias.summary = f"⚡ صفقة عكسية (CHoCH على 1H) — احذر"
    else:
        bias.summary = f"⚠️ عدم توافق بين الفريمات (score: {bias.bullish_score})"

    return bias
