"""BTC trend filter — cached 60s to avoid redundant calls during full scan."""
import time
from core.models import SourceVerdict, MarketSnapshot
from data_sources.binance import binance

_btc_cache: dict = {"ts": 0, "data": None}
_CACHE_TTL = 60


async def get_btc_trend() -> dict:
    if _btc_cache["data"] and (time.time() - _btc_cache["ts"]) < _CACHE_TTL:
        return _btc_cache["data"]

    df_1h = await binance.fetch_klines("BTCUSDT", "1h", 50)
    result = {"valid": False, "trend": "neutral", "score": 50, "details": [],
              "chg_1h": 0, "chg_4h": 0, "chg_24h": 0}
    if df_1h is None or len(df_1h) < 25:
        return result

    c1h = df_1h["c"].astype(float)
    ema21 = c1h.ewm(span=21, adjust=False).mean()
    ema50 = c1h.ewm(span=50, adjust=False).mean()
    price = float(c1h.iloc[-1])
    e21 = float(ema21.iloc[-1])
    e50 = float(ema50.iloc[-1])

    chg_1h = (price - float(c1h.iloc[-2])) / float(c1h.iloc[-2]) * 100
    chg_4h = (price - float(c1h.iloc[-5])) / float(c1h.iloc[-5]) * 100 if len(c1h) >= 5 else 0
    chg_24h = (price - float(c1h.iloc[-24])) / float(c1h.iloc[-24]) * 100 if len(c1h) >= 24 else 0

    if price > e21 > e50 and chg_4h > 0.5:
        result["trend"] = "bullish"; result["score"] = 80
        result["details"].append(f"BTC صاعد +{chg_4h:.2f}%/4h")
    elif price < e21 < e50 and chg_4h < -0.5:
        result["trend"] = "bearish"; result["score"] = 20
        result["details"].append(f"BTC هابط {chg_4h:.2f}%/4h")
    else:
        result["details"].append(f"BTC متذبذب ({chg_4h:+.2f}%/4h)")

    result["valid"] = True
    result["chg_1h"] = chg_1h
    result["chg_4h"] = chg_4h
    result["chg_24h"] = chg_24h

    _btc_cache["ts"] = time.time()
    _btc_cache["data"] = result
    return result


async def filter_btc_trend(snap: MarketSnapshot, direction: str = "long") -> SourceVerdict:
    v = SourceVerdict(name="btc_trend", score=50, confidence=0.0)
    btc = await get_btc_trend()
    if not btc.get("valid"): return v

    if direction == "long":
        if btc["trend"] == "bullish":
            v.score = 85
            v.reasons.append(f"✅ BTC صاعد ({btc['chg_4h']:+.1f}%/4h)")
        elif btc["trend"] == "bearish":
            v.score = 20
            v.warnings.append(f"⚠️ BTC هابط ({btc['chg_4h']:+.1f}%/4h)")
        else:
            v.score = 55
    v.confidence = 0.9
    v.raw = btc
    return v
