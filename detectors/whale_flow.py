"""Whale flow detector — wraps Whale Alert client."""
from core.models import SourceVerdict, MarketSnapshot
from data_sources.whale_alert import whale_alert


async def detect_whale_flow(snap: MarketSnapshot) -> SourceVerdict:
    v = SourceVerdict(name="whale_flow", score=50, confidence=0.0)
    if not whale_alert.enabled:
        return v
    flow = await whale_alert.analyze_flow(snap.symbol)
    if not flow.get("valid"):
        return v
    v.score = flow["score"]
    v.reasons = flow["details"] if flow["score"] >= 60 else []
    v.warnings = flow["details"] if flow["score"] < 40 else []
    v.confidence = 0.85 if flow["tx_count"] > 0 else 0.4
    v.raw = flow
    return v
