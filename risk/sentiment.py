"""
Sentiment Analyzer — crowd psychology read.

Key insight: extreme sentiment = contrarian opportunity.
- Fear & Greed = 90+ (extreme greed) → market top is near
- Fear & Greed = 10- (extreme fear) → market bottom is near

Uses free APIs only:
- alternative.me Fear & Greed (no key needed)
- Binance funding rate aggregate
- 24h liquidations from Coinglass-style heuristics
"""
import asyncio
import aiohttp
from dataclasses import dataclass, field
from typing import Optional
from core.logger import get_logger

log = get_logger(__name__)


@dataclass
class SentimentSnapshot:
    fear_greed: Optional[int] = None        # 0-100
    fear_greed_label: str = "unknown"       # extreme_fear | fear | neutral | greed | extreme_greed
    btc_funding_aggregate: float = 0.0      # avg funding across exchanges (Binance only here)
    market_mood: str = "neutral"            # contrarian_bullish | contrarian_bearish | aligned
    advisory: str = ""                      # human-readable advisory
    score_adjustment: int = 0               # -15..+15 to add to confidence
    warnings: list = field(default_factory=list)


_FNG_CACHE = {"value": None, "ts": 0.0, "label": "unknown"}
_CACHE_TTL = 3600  # 1 hour


async def fetch_fear_greed(session: Optional[aiohttp.ClientSession] = None) -> Optional[dict]:
    """Free Fear & Greed Index from alternative.me. Cached 1 hour."""
    import time
    now = time.time()
    if _FNG_CACHE["value"] is not None and now - _FNG_CACHE["ts"] < _CACHE_TTL:
        return {"value": _FNG_CACHE["value"], "label": _FNG_CACHE["label"]}

    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
    try:
        async with session.get("https://api.alternative.me/fng/?limit=1") as r:
            if r.status != 200:
                return None
            data = await r.json()
            if not data.get("data"):
                return None
            entry = data["data"][0]
            value = int(entry["value"])
            classification = entry.get("value_classification", "unknown").lower().replace(" ", "_")
            _FNG_CACHE["value"] = value
            _FNG_CACHE["ts"] = now
            _FNG_CACHE["label"] = classification
            return {"value": value, "label": classification}
    except Exception as e:
        log.warning("fng_fetch_error", err=str(e))
        return None
    finally:
        if own_session:
            await session.close()


async def fetch_btc_funding_aggregate() -> Optional[float]:
    """BTC funding rate from Binance as proxy for market sentiment."""
    try:
        from data_sources.binance import binance
        data = await binance.fetch_funding_rate("BTCUSDT")
        if data:
            return data.get("funding_rate", 0)
    except Exception as e:
        log.warning("btc_funding_error", err=str(e))
    return None


async def analyze_sentiment(direction: str = "long") -> SentimentSnapshot:
    """Compute sentiment snapshot. Direction = the trade we're considering."""
    snap = SentimentSnapshot()

    fng_data, btc_funding = await asyncio.gather(
        fetch_fear_greed(),
        fetch_btc_funding_aggregate(),
        return_exceptions=True,
    )

    if isinstance(fng_data, dict):
        snap.fear_greed = fng_data["value"]
        snap.fear_greed_label = fng_data["label"]

    if isinstance(btc_funding, (int, float)):
        snap.btc_funding_aggregate = btc_funding

    # ─── Mood logic ──────────────────────────────────────
    fg = snap.fear_greed
    funding = snap.btc_funding_aggregate

    if fg is not None:
        if fg >= 80:
            snap.fear_greed_label = "extreme_greed"
            if direction == "long":
                snap.market_mood = "contrarian_bearish"
                snap.score_adjustment = -10
                snap.advisory = (
                    f"🚨 جشع شديد ({fg}/100) — السوق مكشوف لتصحيح. "
                    f"احذر من شراء جديد، فكر في تثبيت أرباح."
                )
                snap.warnings.append(snap.advisory)
            else:
                snap.market_mood = "aligned"
                snap.score_adjustment = +5
                snap.advisory = f"🎯 جشع شديد ({fg}/100) يدعم البيع contrarian"
        elif fg >= 65:
            if direction == "long":
                snap.score_adjustment = -3
                snap.advisory = f"⚠️ جشع ({fg}/100) — كن انتقائياً في الشراء"
        elif fg <= 20:
            snap.fear_greed_label = "extreme_fear"
            if direction == "short":
                snap.market_mood = "contrarian_bullish"
                snap.score_adjustment = -10
                snap.advisory = (
                    f"🚨 خوف شديد ({fg}/100) — السوق على وشك ارتداد. "
                    f"احذر من بيع جديد."
                )
                snap.warnings.append(snap.advisory)
            else:
                snap.market_mood = "aligned"
                snap.score_adjustment = +5
                snap.advisory = f"🎯 خوف شديد ({fg}/100) — فرصة شراء contrarian"
        elif fg <= 35:
            if direction == "short":
                snap.score_adjustment = -3
                snap.advisory = f"⚠️ خوف ({fg}/100) — كن انتقائياً في البيع"
        else:
            snap.advisory = f"😐 معنويات متعادلة ({fg}/100)"

    # Funding rate also contributes
    if funding > 0.0005:
        if direction == "long":
            snap.score_adjustment -= 3
        else:
            snap.score_adjustment += 3
    elif funding < -0.0005:
        if direction == "long":
            snap.score_adjustment += 3
        else:
            snap.score_adjustment -= 3

    snap.score_adjustment = max(-15, min(15, snap.score_adjustment))
    return snap
