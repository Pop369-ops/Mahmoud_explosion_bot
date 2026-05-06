"""Funding rate divergence detector — sentiment leading indicator."""
from core.models import SourceVerdict, MarketSnapshot
from data_sources.binance import binance


async def detect_funding_divergence(snap: MarketSnapshot) -> SourceVerdict:
    v = SourceVerdict(name="funding_divergence", score=50, confidence=0.0)

    history = await binance.fetch_funding_history(snap.symbol, limit=24)
    current = await binance.fetch_funding_rate(snap.symbol)
    if not current: return v

    rate_now = current.get("funding_rate", 0)
    if not history:
        return _score_from_current(v, rate_now)

    rates = [h["rate"] for h in history]
    if not rates: return _score_from_current(v, rate_now)

    avg_rate = sum(rates) / len(rates)
    last_3_avg = sum(rates[-3:]) / min(3, len(rates))

    score = 50
    reasons = []
    warnings = []

    if rate_now > 0.0005:
        score -= 25
        warnings.append(f"🔥 funding مرتفع جداً ({rate_now*100:.3f}%)")
    elif rate_now > 0.0002:
        score -= 8
        warnings.append(f"⚠️ funding مرتفع ({rate_now*100:.3f}%)")
    elif rate_now < -0.0003:
        score += 25
        reasons.append(f"🟢 funding سالب ({rate_now*100:.3f}%) — short squeeze محتمل")
    elif rate_now < -0.0001:
        score += 12

    delta = last_3_avg - avg_rate
    if abs(delta) > abs(avg_rate) * 0.5 and abs(avg_rate) > 0.00005:
        if delta > 0 and snap.change_24h < 0:
            score -= 15
            warnings.append("🚨 funding مرتفع رغم الهبوط — longs محاصرة")
        elif delta < 0 and snap.change_24h < 0:
            score += 8
            reasons.append("🔄 funding يبرد — قاع محتمل")

    v.score = max(0, min(100, score))
    v.reasons = reasons
    v.warnings = warnings
    v.confidence = 0.75
    v.raw = {"rate_now": rate_now, "avg_24h": avg_rate}
    return v


def _score_from_current(v: SourceVerdict, rate: float) -> SourceVerdict:
    score = 50
    if rate > 0.0005:
        score = 25
        v.warnings.append(f"🔥 funding مرتفع ({rate*100:.3f}%)")
    elif rate < -0.0003:
        score = 70
        v.reasons.append(f"🟢 funding سالب ({rate*100:.3f}%)")
    v.score = score
    v.confidence = 0.5
    v.raw = {"rate_now": rate}
    return v
