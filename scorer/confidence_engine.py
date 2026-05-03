"""
Confidence Engine — combines verdicts from independent sources via weighted scoring.
Solves v3's biggest flaw (correlated errors from single Binance feed).
"""
import asyncio
import pandas as pd
from core.models import SourceVerdict, MarketSnapshot, Signal, Phase, Mode
from config.settings import settings
from core.logger import get_logger

from detectors.volume_delta import detect_volume_delta
from detectors.order_book import detect_order_book
from detectors.funding_divergence import detect_funding_divergence
from detectors.whale_flow import detect_whale_flow
from detectors.squeeze_breakout import detect_squeeze_breakout
from detectors.pre_explosion import detect_pre_explosion

from filters.multi_timeframe import filter_multi_timeframe
from filters.btc_trend import filter_btc_trend
from filters.btc_correlation import filter_btc_correlation
from filters.risk_gate import filter_liquidity, critical_risk_check

log = get_logger(__name__)


async def score_symbol(snap: MarketSnapshot, mode: Mode = Mode.DAY,
                        direction: str = "long") -> Signal:
    sig = Signal(
        symbol=snap.symbol, phase=Phase.NONE, confidence=0,
        sources_agreed=0, sources_total=0,
        price=snap.price, change_24h=snap.change_24h, volume_24h=snap.volume_24h,
        entry=0, sl=0, tp1=0, tp2=0, tp3=0, sl_pct=0, atr=0, mode=mode,
    )

    risk = await critical_risk_check(direction)
    if risk["reject"]:
        sig.phase = Phase.REJECTED
        sig.rejected_reason = risk["reason"]
        sig.warnings.append(risk["reason"])
        return sig

    if snap.klines is None or len(snap.klines) < 30:
        return sig

    detector_tasks = [
        detect_volume_delta(snap), detect_order_book(snap),
        detect_funding_divergence(snap), detect_whale_flow(snap),
        detect_squeeze_breakout(snap), detect_pre_explosion(snap),
    ]
    filter_tasks = [
        filter_multi_timeframe(snap, direction),
        filter_btc_trend(snap, direction),
        filter_btc_correlation(snap, direction),
        filter_liquidity(snap),
    ]

    all_results = await asyncio.gather(*detector_tasks, *filter_tasks, return_exceptions=True)

    verdicts: list[SourceVerdict] = []
    for r in all_results:
        if isinstance(r, SourceVerdict):
            verdicts.append(r)
        else:
            log.warning("verdict_error", err=str(r))

    weights = settings.source_weights
    weighted_sum = 0.0
    total_weight = 0.0
    sources_agreed = 0
    sources_total = 0

    for verd in verdicts:
        if verd.confidence < 0.2:
            continue
        w = weights.get(verd.name, 0.05) * verd.confidence
        weighted_sum += verd.score * w
        total_weight += w
        sources_total += 1
        if verd.agrees:
            sources_agreed += 1

    confidence = int(weighted_sum / total_weight) if total_weight > 0 else 0

    all_signals: list[str] = []
    all_warnings: list[str] = []
    for verd in verdicts:
        all_signals.extend(verd.reasons)
        all_warnings.extend(verd.warnings)

    sig.confidence = confidence
    sig.sources_agreed = sources_agreed
    sig.sources_total = sources_total
    sig.verdicts = verdicts
    sig.signals = all_signals
    sig.warnings = all_warnings

    sig.phase = _classify_phase(confidence, sources_agreed, verdicts)
    _attach_tp_sl(sig, snap, mode, direction)
    _attach_quick_metrics(sig, snap)
    return sig


def _classify_phase(confidence: int, sources_agreed: int,
                     verdicts: list[SourceVerdict]) -> Phase:
    pre_v = next((v for v in verdicts if v.name == "pre_explosion"), None)

    if pre_v and pre_v.score >= 75 and sources_agreed >= 3:
        return Phase.PRE_EXPLOSION_EARLY
    if confidence >= 80 and sources_agreed >= 5:
        return Phase.EXPLOSION_PREMIUM
    if confidence >= 75 and sources_agreed >= 4:
        return Phase.EXPLOSION_NOW
    if confidence >= 65 and sources_agreed >= 4:
        return Phase.EXPLOSION_STRONG
    if confidence >= 60 and sources_agreed >= 3:
        return Phase.EXPLOSION_START
    if confidence >= 50 and sources_agreed >= 3:
        return Phase.PRE_EXPLOSION
    if pre_v and pre_v.score >= 65 and sources_agreed >= 2:
        return Phase.PRE_EXPLOSION
    return Phase.NONE


def _attach_tp_sl(sig: Signal, snap: MarketSnapshot, mode: Mode, direction: str = "long"):
    """Liquidity-aware SL/TP: hides stops behind real swing/wall liquidity
    so that stop hunts (sweeps) don't trigger before the real move.
    Falls back to ATR-based placement if no clear liquidity is found."""
    from scorer.liquidity_levels import build_plan

    df = snap.klines
    if df is None or len(df) < 15:
        return

    plan = build_plan(
        price=snap.price,
        df=df,
        order_book=snap.order_book or {},
        direction=direction,
        mode=mode.value,
    )

    sig.entry = plan.entry
    sig.sl = plan.sl
    sig.tp1 = plan.tp1
    sig.tp2 = plan.tp2
    sig.tp3 = plan.tp3
    sig.sl_pct = plan.sl_pct
    sig.atr = 0.0  # legacy field, kept for compatibility

    # Inject liquidity rationale into the alert
    sig.signals.append(f"🛡 SL: {plan.sl_logic}")
    sig.signals.append(f"🎯 TP: {plan.tp_logic}")
    sig.signals.append(
        f"⚖️ R:R = {plan.rr_tp1:.2f} / {plan.rr_tp2:.2f} / {plan.rr_tp3:.2f}"
    )
    if plan.method == "atr_fallback":
        sig.warnings.append("⚠️ سيولة غير واضحة — اعتمد ATR (احتمال stop hunt أعلى)")
    sig.warnings.extend(plan.warnings)


def _attach_quick_metrics(sig: Signal, snap: MarketSnapshot):
    df = snap.klines
    if df is None or len(df) < 25:
        return
    c = df["c"].astype(float)
    v = df["v"].astype(float)

    delta = c.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, float("nan"))
    rsi = 100 - 100 / (1 + rs)
    sig.rsi = float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else None

    vol_now = float(v.iloc[-1])
    vol_avg20 = float(v.iloc[-21:-1].mean())
    sig.vol_ratio = vol_now / vol_avg20 if vol_avg20 > 0 else None
