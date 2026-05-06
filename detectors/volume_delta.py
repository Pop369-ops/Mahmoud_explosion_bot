"""Volume Delta detector — LEADING indicator from aggTrades."""
from core.models import SourceVerdict, MarketSnapshot
from data_sources.binance import binance


async def detect_volume_delta(snap: MarketSnapshot) -> SourceVerdict:
    v = SourceVerdict(name="volume_delta", score=50, confidence=0.0)

    trades = await binance.fetch_agg_trades(snap.symbol, limit=500)
    if len(trades) < 100:
        v.confidence = 0.2
        return v

    sorted_trades = sorted(trades, key=lambda x: x["ts"])
    n_buckets = 10
    bucket_size = len(sorted_trades) // n_buckets

    buckets = []
    for i in range(n_buckets):
        chunk = sorted_trades[i * bucket_size:(i + 1) * bucket_size]
        if not chunk: continue
        buy_qty = sum(t["qty"] * t["price"] for t in chunk if t["is_buy"])
        sell_qty = sum(t["qty"] * t["price"] for t in chunk if not t["is_buy"])
        total = buy_qty + sell_qty
        delta = buy_qty - sell_qty
        ratio = delta / total if total > 0 else 0
        last_p = chunk[-1]["price"]; first_p = chunk[0]["price"]
        buckets.append({
            "delta": delta, "ratio": ratio, "total": total,
            "price_change": (last_p - first_p) / first_p if first_p > 0 else 0,
        })

    if len(buckets) < 6:
        v.confidence = 0.3
        return v

    recent = buckets[-3:]
    earlier = buckets[:-3]
    recent_delta_avg = sum(b["ratio"] for b in recent) / len(recent)
    earlier_delta_avg = sum(b["ratio"] for b in earlier) / len(earlier) if earlier else 0
    recent_volume = sum(b["total"] for b in recent)
    earlier_volume_avg = sum(b["total"] for b in earlier) / len(earlier) if earlier else 1
    vol_surge = recent_volume / (earlier_volume_avg * len(recent)) if earlier_volume_avg else 1
    recent_price_chg = sum(b["price_change"] for b in recent)

    score = 50
    reasons = []
    warnings = []

    if recent_delta_avg > 0.35:
        score += 25
        reasons.append(f"⚡ ضغط شراء قوي (delta +{recent_delta_avg*100:.0f}%)")
    elif recent_delta_avg > 0.15:
        score += 15
        reasons.append(f"📈 ضغط شراء (delta +{recent_delta_avg*100:.0f}%)")
    elif recent_delta_avg < -0.25:
        score -= 25
        warnings.append(f"🔴 ضغط بيع (delta {recent_delta_avg*100:.0f}%)")
    elif recent_delta_avg < -0.10:
        score -= 10

    if vol_surge > 2.0:
        score += 10
        reasons.append(f"💥 حجم x{vol_surge:.1f} في آخر النوافذ")
    elif vol_surge > 1.4:
        score += 5

    # Bullish divergence — flat price, surging buy delta = silent accumulation
    if abs(recent_price_chg) < 0.005 and recent_delta_avg > 0.20 and earlier_delta_avg < 0:
        score += 20
        reasons.append("🌱 تباعد إيجابي: تراكم صامت قبل الحركة")
        v.confidence = 0.9
    # Bearish divergence — price up but delta weakening
    elif recent_price_chg > 0.01 and recent_delta_avg < earlier_delta_avg - 0.15:
        score -= 15
        warnings.append("⚠️ تباعد سلبي: ارتفاع بدون شراء حقيقي")

    v.score = max(0, min(100, score))
    v.reasons = reasons
    v.warnings = warnings
    v.confidence = max(v.confidence, 0.7)
    v.raw = {
        "recent_delta_avg": recent_delta_avg,
        "earlier_delta_avg": earlier_delta_avg,
        "vol_surge": vol_surge,
    }
    return v
