"""Order book imbalance detector — LEADING."""
from core.models import SourceVerdict, MarketSnapshot
from data_sources.binance import binance


async def detect_order_book(snap: MarketSnapshot) -> SourceVerdict:
    v = SourceVerdict(name="order_book", score=50, confidence=0.0)

    ob = snap.order_book or await binance.fetch_order_book(snap.symbol, limit=50)
    if not ob or not ob.get("bids") or not ob.get("asks"):
        return v

    bids, asks = ob["bids"], ob["asks"]
    if not bids or not asks: return v

    best_bid = bids[0][0]; best_ask = asks[0][0]
    mid = (best_bid + best_ask) / 2
    if mid <= 0: return v

    spread_pct = (best_ask - best_bid) / mid * 100
    if spread_pct > 0.3:
        v.warnings.append(f"⚠️ سبريد عريض ({spread_pct:.2f}%)")
        v.confidence = 0.4

    near_threshold = mid * 0.005
    near_bid_qty = sum(q for p, q in bids[:25] if mid - p <= near_threshold)
    near_ask_qty = sum(q for p, q in asks[:25] if p - mid <= near_threshold)
    total_near = near_bid_qty + near_ask_qty
    if total_near <= 0: return v

    imbalance_ratio = near_bid_qty / near_ask_qty if near_ask_qty > 0 else 999
    score = 50
    reasons = []
    warnings = []

    if imbalance_ratio > 3.0:
        score += 30
        reasons.append(f"🟢 طلب الشراء x{imbalance_ratio:.1f} (ضغط شراء قوي)")
    elif imbalance_ratio > 2.0:
        score += 20
        reasons.append(f"🟢 طلب الشراء x{imbalance_ratio:.1f} ضعف البيع")
    elif imbalance_ratio > 1.5:
        score += 10
        reasons.append(f"📈 طلب شراء أعلى (x{imbalance_ratio:.1f})")
    elif imbalance_ratio < 0.4:
        score -= 25
        warnings.append(f"🔴 طلب البيع x{1/imbalance_ratio:.1f} ضعف الشراء")
    elif imbalance_ratio < 0.6:
        score -= 10

    avg_bid_qty = sum(q for p, q in bids[:20]) / 20 if bids else 0
    avg_ask_qty = sum(q for p, q in asks[:20]) / 20 if asks else 0
    big_bid_walls = [(p, q) for p, q in bids[:30] if avg_bid_qty > 0 and q > avg_bid_qty * 5]
    big_ask_walls = [(p, q) for p, q in asks[:30] if avg_ask_qty > 0 and q > avg_ask_qty * 5]

    if big_bid_walls and not big_ask_walls:
        score += 12
        reasons.append(f"🏛 جدار شراء قوي عند ${big_bid_walls[0][0]:.4f}")
    elif big_ask_walls and not big_bid_walls:
        score -= 12
        warnings.append(f"🧱 جدار بيع كبير عند ${big_ask_walls[0][0]:.4f}")

    v.score = max(0, min(100, score))
    v.reasons = reasons
    v.warnings = warnings
    v.confidence = 0.85 if total_near > 0 else 0.3
    v.raw = {
        "imbalance_ratio": imbalance_ratio,
        "spread_pct": spread_pct,
        "bid_walls": len(big_bid_walls),
        "ask_walls": len(big_ask_walls),
    }
    return v
