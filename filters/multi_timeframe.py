"""Multi-timeframe alignment filter."""
import pandas as pd
from core.models import SourceVerdict, MarketSnapshot
from data_sources.binance import binance


async def filter_multi_timeframe(snap: MarketSnapshot, direction: str = "long") -> SourceVerdict:
    v = SourceVerdict(name="multi_timeframe", score=50, confidence=0.0)

    df_15m = snap.klines_15m or await binance.fetch_klines(snap.symbol, "15m", 50)
    df_1h = snap.klines_1h or await binance.fetch_klines(snap.symbol, "1h", 50)

    if df_15m is None or len(df_15m) < 25 or df_1h is None or len(df_1h) < 25:
        v.confidence = 0.2
        return v

    aligned_15m = _trend_aligned(df_15m, direction)
    aligned_1h = _trend_aligned(df_1h, direction)

    if aligned_15m and aligned_1h:
        v.score = 85
        v.reasons.append("✅ 15m و 1h متوافقان مع الإشارة")
        v.confidence = 0.9
    elif aligned_15m or aligned_1h:
        v.score = 60
        v.reasons.append("✅ 1h متوافق" if aligned_1h else "✅ 15m متوافق")
        v.confidence = 0.7
    else:
        v.score = 25
        v.warnings.append("❌ 15m و 1h ضد الإشارة")
        v.confidence = 0.85

    v.raw = {"15m_aligned": aligned_15m, "1h_aligned": aligned_1h}
    return v


def _trend_aligned(df: pd.DataFrame, direction: str) -> bool:
    c = df["c"].astype(float)
    ema8 = c.ewm(span=8, adjust=False).mean()
    ema21 = c.ewm(span=21, adjust=False).mean()
    last_price = float(c.iloc[-1])
    e8 = float(ema8.iloc[-1]); e21 = float(ema21.iloc[-1])
    short_change = (last_price - float(c.iloc[-5])) / float(c.iloc[-5]) if c.iloc[-5] > 0 else 0
    if direction == "long":
        return last_price > e8 and e8 > e21 and short_change > -0.005
    return last_price < e8 and e8 < e21 and short_change < 0.005
