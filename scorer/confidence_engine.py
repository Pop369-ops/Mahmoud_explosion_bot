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

    # ─── ICT: Multi-Timeframe Bias check (HARD GATE) ──
    from scorer.htf_bias import compute_bias, fetch_htf_klines
    from scorer.killzones import get_current_killzone

    # Fetch & cache HTF klines if not already on snapshot
    if snap.klines_4h is None or snap.klines_1d is None:
        try:
            htf_data = await fetch_htf_klines(snap.symbol)
            snap.klines_4h = htf_data.get("4h")
            snap.klines_1d = htf_data.get("1d")
        except Exception as e:
            log.warning("htf_fetch_error", err=str(e))
            htf_data = {"1d": None, "4h": None, "1h": snap.klines_1h, "15m": snap.klines_15m}
    else:
        htf_data = {
            "1d": snap.klines_1d, "4h": snap.klines_4h,
            "1h": snap.klines_1h, "15m": snap.klines_15m,
        }

    snap.htf_bias = compute_bias(htf_data, snap.price, proposed_direction=direction)
    snap.killzone = get_current_killzone()

    # HARD REJECT if HTF bias forbids this direction
    if snap.htf_bias.rejection_reason:
        sig.phase = Phase.REJECTED
        sig.rejected_reason = snap.htf_bias.rejection_reason
        sig.warnings.append(f"🚫 {snap.htf_bias.rejection_reason}")
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

    # ─── Killzone quality multiplier ──
    if snap.killzone:
        confidence = int(confidence * snap.killzone.quality_mult)
        confidence = max(0, min(100, confidence))

    # ─── HTF bias confluence bonus / penalty ──
    if snap.htf_bias:
        b = snap.htf_bias
        if direction == "long" and b.aligned_long:
            confidence = min(100, confidence + 8)
        elif direction == "short" and b.aligned_short:
            confidence = min(100, confidence + 8)
        elif b.counter_trend_setup:
            confidence = max(0, confidence - 5)

    all_signals: list[str] = []
    all_warnings: list[str] = []
    for verd in verdicts:
        all_signals.extend(verd.reasons)
        all_warnings.extend(verd.warnings)

    # Inject HTF bias summary
    if snap.htf_bias:
        all_signals.append(f"🌐 HTF: {snap.htf_bias.summary}")
        for w in snap.htf_bias.warnings:
            all_warnings.append(w)
    if snap.killzone:
        all_signals.append(f"⏱ {snap.killzone.description_ar}")

    sig.confidence = confidence
    sig.sources_agreed = sources_agreed
    sig.sources_total = sources_total
    sig.verdicts = verdicts
    sig.signals = all_signals
    sig.warnings = all_warnings

    sig.phase = _classify_phase(confidence, sources_agreed, verdicts)
    _attach_tp_sl(sig, snap, mode, direction)
    _attach_quick_metrics(sig, snap)

    # ═══════════════════════════════════════════════════════
    # HARD QUALITY GATE — final filter before signal goes live
    # ═══════════════════════════════════════════════════════
    _apply_hard_gate(sig, snap, direction)

    return sig


def _apply_hard_gate(sig: Signal, snap: MarketSnapshot, direction: str):
    """Apply strict quality filters. Rejects signals that pass scoring
    but have hidden quality issues that lead to losses.

    Rejection criteria (any one triggers rejection):
    1. SL is on ATR fallback AND R:R on TP1 < 1.5 (high stop-hunt risk)
    2. Sources agreed < 5 AND price is in HTF premium/discount opposite to trade
    3. Order Book reading is opposite (score < 40) AND not strongly mitigated
    4. R:R on TP1 < 0.8 (reward not worth the risk)
    5. SL is on ATR fallback AND HTF score is weak (< 20 absolute)
    """
    if sig.phase == Phase.REJECTED:
        return  # already rejected upstream
    if sig.phase == Phase.NONE:
        return  # not actionable anyway

    rejections = []
    sl_logic = ""
    rr_tp1 = 0.0

    # Extract SL logic and R:R from the signal text we already injected
    for s in sig.signals:
        if s.startswith("🛡 SL:"):
            sl_logic = s
        if "R:R =" in s:
            try:
                # Parse "⚖️ R:R = 1.85 / 3.20 / 5.10"
                parts = s.split("=")[1].split("/")
                rr_tp1 = float(parts[0].strip())
            except (IndexError, ValueError):
                pass

    is_atr_fallback = "ATR fallback" in sl_logic or "ATR fallback" in sig.signals.__str__()

    # ─── Gate 1: ATR fallback + weak R:R ─────────────────
    if is_atr_fallback and rr_tp1 > 0 and rr_tp1 < 1.5:
        rejections.append(
            f"SL على ATR + R:R ضعيف ({rr_tp1:.2f} < 1.5) — "
            f"احتمال stop hunt مرتفع جداً"
        )

    # ─── Gate 2: very weak R:R (regardless of SL method) ─
    if rr_tp1 > 0 and rr_tp1 < 0.8:
        rejections.append(
            f"R:R على TP1 ضعيف جداً ({rr_tp1:.2f}) — "
            f"المكافأة أقل من المخاطرة"
        )

    # ─── Gate 3: order book contradicts the trade ────────
    ob_verdict = next((v for v in sig.verdicts if v.name == "order_book"), None)
    if ob_verdict and ob_verdict.score < 35 and ob_verdict.confidence >= 0.5:
        if sig.sources_agreed < 5:
            rejections.append(
                f"دفتر الأوامر يعارض الصفقة ({ob_verdict.score}/100) "
                f"+ المصادر المتفقة قليلة ({sig.sources_agreed}/{sig.sources_total})"
            )

    # ─── Gate 4: premium/discount conflict on HTF ────────
    if snap.htf_bias and snap.htf_bias.daily:
        zone = snap.htf_bias.daily.zone
        if direction == "long" and zone == "premium" and sig.sources_agreed < 5:
            rejections.append(
                "شراء في منطقة premium على اليومي مع مصادر < 5 — مكلف وغير محقق"
            )
        elif direction == "short" and zone == "discount" and sig.sources_agreed < 5:
            rejections.append(
                "بيع في منطقة discount على اليومي مع مصادر < 5 — متأخر"
            )

    # ─── Gate 5: ATR fallback + weak HTF alignment ───────
    if is_atr_fallback and snap.htf_bias:
        htf_score = abs(snap.htf_bias.bullish_score)
        if htf_score < 20:
            rejections.append(
                f"SL على ATR + اتجاه عام ضعيف (HTF score: {snap.htf_bias.bullish_score}) — "
                f"لا يوجد دعم اتجاهي قوي"
            )

    # ─── Gate 6: NO liquidity above/below + premium/discount + extended move
    # Buying near top with no clear TP target = stop hunt magnet
    no_liquidity_warning = any(
        ("لا توجد سيولة فوق" in w or "لا توجد سيولة تحت" in w
         or "لا توجد قمم سيولة" in w or "لا توجد قيعان سيولة" in w)
        for w in sig.warnings
    )
    if no_liquidity_warning:
        # Combined with extended 24h move (>5%) → high reversal risk
        if abs(sig.change_24h) >= 5.0:
            rejections.append(
                f"لا توجد سيولة في اتجاه الصفقة + حركة 24h ممتدة "
                f"({sig.change_24h:+.1f}%) — احتمال انعكاس مرتفع"
            )
        # Or in premium/discount conflict zone
        elif snap.htf_bias and snap.htf_bias.daily:
            zone = snap.htf_bias.daily.zone
            if direction == "long" and zone == "premium":
                rejections.append(
                    "لا توجد سيولة فوق + السعر في premium زون — شراء فوق القمة"
                )
            elif direction == "short" and zone == "discount":
                rejections.append(
                    "لا توجد سيولة تحت + السعر في discount زون — بيع تحت القاع"
                )

    # ─── Gate 7: BTC correlation/trend both very weak (< 50)
    # Means this trade has zero macro support — purely isolated
    btc_trend_v = next((v for v in sig.verdicts if v.name == "btc_trend"), None)
    btc_corr_v = next((v for v in sig.verdicts if v.name == "btc_correlation"), None)
    if btc_trend_v and btc_corr_v:
        if (btc_trend_v.score < 55 and btc_corr_v.score < 55
            and btc_trend_v.confidence >= 0.4 and btc_corr_v.confidence >= 0.4):
            # Only reject if combined with weak overall structure
            if sig.sources_agreed < 6 and abs(sig.change_24h) >= 5.0:
                rejections.append(
                    f"BTC اتجاه/ارتباط ضعيف ({btc_trend_v.score}/{btc_corr_v.score}) "
                    f"+ حركة 24h ممتدة — لا يوجد دعم ماكرو"
                )

    # ─── Apply rejection ─────────────────────────────────
    if rejections:
        sig.phase = Phase.REJECTED
        sig.rejected_reason = " | ".join(rejections[:2])  # show top 2 reasons
        sig.warnings.append(f"🚫 رُفضت: {rejections[0]}")
        log.info("hard_gate_reject", symbol=sig.symbol,
                 confidence=sig.confidence, reasons=rejections)


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
