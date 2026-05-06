"""
Diagnostic Scanner — explains exactly why the live scanner is producing no signals.

Runs the FULL live scoring engine (not the lightweight retroactive one) on top
symbols and reports back:
- For each symbol: confidence, sources_agreed, phase, rejection reason
- Aggregate: which gates are rejecting most
- Identify if the issue is HARD GATE too strict OR confidence engine too low
"""
import asyncio
from dataclasses import dataclass, field
from typing import Optional

from data_sources.binance import binance
from core.models import MarketSnapshot, Signal, Phase
from scorer.confidence_engine import score_symbol
from core.logger import get_logger

log = get_logger(__name__)


@dataclass
class DiagnosticEntry:
    symbol: str
    confidence: int
    sources_agreed: int
    sources_total: int
    phase: str
    rejection_reason: Optional[str]
    change_24h: float
    volume_24h: float
    failed_threshold: bool = False  # confidence/sources below threshold
    failed_gate: bool = False        # rejected by HARD GATE
    no_phase: bool = False           # phase=NONE (no signal at all)


async def diagnose_live_scan(top_n: int = 20) -> dict:
    """Run the full live scoring on top N coins and report findings."""
    try:
        pairs = await binance.fetch_top_pairs(top_n=top_n, min_vol_usd=10_000_000)
    except Exception as e:
        return {"error": f"خطأ في جلب العملات: {type(e).__name__}"}

    if not pairs:
        return {"error": "لا توجد عملات"}

    semaphore = asyncio.Semaphore(4)

    async def _diagnose(p: dict) -> Optional[DiagnosticEntry]:
        async with semaphore:
            try:
                # Fetch all data the live scanner uses
                results = await asyncio.gather(
                    binance.fetch_klines(p["sym"], "5m", 80),
                    binance.fetch_klines(p["sym"], "15m", 50),
                    binance.fetch_klines(p["sym"], "1h", 60),
                    binance.fetch_klines(p["sym"], "4h", 60),
                    binance.fetch_klines(p["sym"], "1d", 50),
                    binance.fetch_order_book(p["sym"], 50),
                    return_exceptions=True,
                )
                klines = results[0] if not isinstance(results[0], Exception) else None
                if klines is None or len(klines) < 30:
                    return None

                snap = MarketSnapshot(
                    symbol=p["sym"], price=p["price"],
                    volume_24h=p["vol_24h"], change_24h=p["chg_24h"],
                    klines=klines,
                    klines_15m=results[1] if not isinstance(results[1], Exception) else None,
                    klines_1h=results[2] if not isinstance(results[2], Exception) else None,
                    klines_4h=results[3] if not isinstance(results[3], Exception) else None,
                    klines_1d=results[4] if not isinstance(results[4], Exception) else None,
                    order_book=results[5] if not isinstance(results[5], Exception) else None,
                )
                sig = await score_symbol(snap)

                entry = DiagnosticEntry(
                    symbol=p["sym"],
                    confidence=sig.confidence,
                    sources_agreed=sig.sources_agreed,
                    sources_total=sig.sources_total,
                    phase=sig.phase.value,
                    rejection_reason=sig.rejected_reason,
                    change_24h=p["chg_24h"],
                    volume_24h=p["vol_24h"],
                )

                if sig.phase == Phase.REJECTED:
                    entry.failed_gate = True
                elif sig.phase == Phase.NONE:
                    entry.no_phase = True
                elif sig.confidence < 65 or sig.sources_agreed < 4:
                    entry.failed_threshold = True
                return entry
            except Exception as e:
                log.warning("diag_error", sym=p["sym"], err=str(e))
                return None

    results = await asyncio.gather(*[_diagnose(p) for p in pairs])
    entries = [r for r in results if r is not None]

    # Aggregate
    total = len(entries)
    would_pass = sum(
        1 for e in entries
        if not e.failed_gate and not e.failed_threshold and not e.no_phase
    )
    rejected_by_gate = [e for e in entries if e.failed_gate]
    below_threshold = [e for e in entries if e.failed_threshold]
    no_signal = [e for e in entries if e.no_phase]

    # Top close calls
    rejected_by_gate.sort(key=lambda e: -e.confidence)
    below_threshold.sort(key=lambda e: -e.confidence)

    # Rejection reason histogram
    rejection_reasons = {}
    for e in rejected_by_gate:
        if e.rejection_reason:
            for r in e.rejection_reason.split(" | "):
                key = r.split("(")[0].strip()[:80]
                rejection_reasons[key] = rejection_reasons.get(key, 0) + 1

    return {
        "total": total,
        "would_pass": would_pass,
        "rejected_by_gate": len(rejected_by_gate),
        "below_threshold": len(below_threshold),
        "no_signal": len(no_signal),
        "top_rejected": rejected_by_gate[:5],
        "top_below_threshold": below_threshold[:5],
        "rejection_summary": rejection_reasons,
    }


def render_diagnosis(report: dict) -> str:
    if "error" in report:
        return f"❌ {report['error']}"

    m = "🔬 تشخيص المسح الحي\n"
    m += "━━━━━━━━━━━━━━━━━━━━\n\n"
    m += f"عدد العملات المفحوصة: {report['total']}\n\n"

    m += "📊 النتائج:\n"
    m += f"  ✅ ستمر للإرسال: {report['would_pass']}\n"
    m += f"  🚫 رفض HARD GATE: {report['rejected_by_gate']}\n"
    m += f"  ⚪ تحت العتبة (ثقة < 65 أو مصادر < 4): {report['below_threshold']}\n"
    m += f"  ⚫ لا إشارة أساساً: {report['no_signal']}\n\n"

    if report["top_rejected"]:
        m += "🚫 أعلى 5 إشارات مرفوضة من HARD GATE:\n"
        for e in report["top_rejected"]:
            m += f"\n• {e.symbol} (ثقة {e.confidence}, مصادر {e.sources_agreed}/{e.sources_total})\n"
            m += f"  24h: {e.change_24h:+.2f}% | حجم: ${e.volume_24h/1_000_000:.1f}M\n"
            if e.rejection_reason:
                reasons = e.rejection_reason.split(" | ")
                for r in reasons[:2]:
                    m += f"  ✗ {r[:90]}\n"
        m += "\n"

    if report["top_below_threshold"]:
        m += "⚪ أعلى 5 إشارات تحت العتبة (HARD GATE ما وصل لها):\n"
        for e in report["top_below_threshold"]:
            m += f"\n• {e.symbol} (ثقة {e.confidence}, مصادر {e.sources_agreed}/{e.sources_total})\n"
            m += f"  24h: {e.change_24h:+.2f}%\n"
        m += "\n"

    if report["rejection_summary"]:
        m += "📊 أسباب رفض HARD GATE الأكثر شيوعاً:\n"
        sorted_reasons = sorted(report["rejection_summary"].items(),
                                  key=lambda x: -x[1])
        for reason, count in sorted_reasons[:6]:
            m += f"  • {reason}: {count} مرة\n"
        m += "\n"

    # Verdict
    m += "🎯 التشخيص:\n"
    if report["would_pass"] > 0:
        m += f"✅ هناك {report['would_pass']} إشارة ستصلك في المسح القادم.\n"
        m += "إذا لم تصل، المشكلة في cooldown أو risk limits."
    elif report["rejected_by_gate"] >= report["total"] * 0.3:
        m += "🚫 HARD GATE صارم جداً — يرفض كثير من الإشارات.\n"
        m += "هذا الصح للحماية، لكن قد نخفف فلتر معين بناءً على البيانات أعلاه."
    elif report["below_threshold"] >= report["total"] * 0.5:
        m += "⚪ معظم العملات لا تصل لعتبة الثقة (65/100).\n"
        m += "السوق هادئ — لا توجد setups واضحة."
    else:
        m += "⚫ السوق هادئ تماماً — لا توجد إشارات بأي مستوى.\n"
        m += "انتظر volatility أعلى."

    return m
