"""Liquidity check + critical risk gate."""
from core.models import SourceVerdict, MarketSnapshot
from filters.btc_trend import get_btc_trend


async def filter_liquidity(snap: MarketSnapshot) -> SourceVerdict:
    v = SourceVerdict(name="liquidity", score=50, confidence=0.5)
    score = 50
    warnings = []

    if snap.volume_24h < 1_000_000:
        score = 20
        warnings.append("⚠️ سيولة 24h ضعيفة جداً")
    elif snap.volume_24h < 5_000_000:
        score = 45
    elif snap.volume_24h < 50_000_000:
        score = 65
    else:
        score = 80

    if snap.order_book:
        bids = snap.order_book.get("bids", [])
        asks = snap.order_book.get("asks", [])
        if bids and asks:
            best_bid = bids[0][0]; best_ask = asks[0][0]
            mid = (best_bid + best_ask) / 2
            spread_pct = (best_ask - best_bid) / mid * 100 if mid > 0 else 999
            if spread_pct > 0.5:
                score = min(score, 30)
                warnings.append(f"⚠️ سبريد عريض ({spread_pct:.2f}%)")

    v.score = score
    v.warnings = warnings
    v.confidence = 0.85
    v.raw = {"volume_24h": snap.volume_24h}
    return v


async def critical_risk_check(direction: str = "long") -> dict:
    """Hard rejections — BTC dump kills all longs."""
    btc = await get_btc_trend()
    if not btc.get("valid"):
        return {"reject": False, "reason": ""}

    chg_1h = btc.get("chg_1h", 0)
    chg_4h = btc.get("chg_4h", 0)

    if direction == "long":
        if chg_1h < -1.5:
            return {"reject": True, "reason": f"🚫 BTC ينهار ({chg_1h:+.2f}%/1h) — لا longs"}
        if chg_4h < -3.0:
            return {"reject": True, "reason": f"🚫 BTC هابط بقوة ({chg_4h:+.2f}%/4h)"}
    return {"reject": False, "reason": ""}
