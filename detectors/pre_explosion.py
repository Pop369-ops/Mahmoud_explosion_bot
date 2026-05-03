"""Pre-explosion detector. Refactored from v3's detect_pre_explosion."""
import numpy as np
from core.models import SourceVerdict, MarketSnapshot


async def detect_pre_explosion(snap: MarketSnapshot) -> SourceVerdict:
    v = SourceVerdict(name="pre_explosion", score=50, confidence=0.0)

    df = snap.klines
    if df is None or len(df) < 30:
        return v

    c = df["c"].astype(float)
    h = df["h"].astype(float)
    l = df["l"].astype(float)
    o = df["o"].astype(float)
    vol = df["v"].astype(float)
    bq = df["bq"].astype(float)
    qv = df["qv"].astype(float)

    score = 50
    reasons = []

    # Pattern 1: rising volume on flat price
    last10_price_range = (h.iloc[-10:].max() - l.iloc[-10:].min()) / c.iloc[-10] * 100
    last10_vol_ratio = vol.iloc[-10:].mean() / max(vol.iloc[-30:-10].mean(), 1e-9)
    if last10_price_range < 2.5 and last10_vol_ratio > 1.5:
        score += 25
        reasons.append(f"🌱 حجم x{last10_vol_ratio:.1f} مع سعر ضيق ({last10_price_range:.1f}%)")

    # Pattern 2: higher lows
    last8_lows = l.iloc[-8:].values
    rising_lows = sum(1 for i in range(1, len(last8_lows)) if last8_lows[i] >= last8_lows[i-1] * 0.998)
    if rising_lows >= 5:
        score += 12
        reasons.append("📈 قيعان صاعدة")

    # Pattern 3: buy-side dominance
    if qv.iloc[-10:].sum() > 0:
        buy_pct = bq.iloc[-10:].sum() / qv.iloc[-10:].sum()
        if buy_pct > 0.62:
            score += 18
            reasons.append(f"🟢 {buy_pct*100:.0f}% من الحجم شراء")
        elif buy_pct > 0.55:
            score += 8

    # Pattern 4: lower wick rejection
    body_size = (c - o).abs()
    lower_wick = (np.minimum(c, o) - l).clip(lower=0)
    long_lower_wicks = sum(1 for i in range(-5, 0)
                            if lower_wick.iloc[i] > body_size.iloc[i] * 1.5
                            and body_size.iloc[i] > 0)
    if long_lower_wicks >= 3:
        score += 10
        reasons.append(f"⬆️ {long_lower_wicks}/5 شموع رفض من الأسفل")

    # Pattern 5: volatility contraction
    atr_recent = (h.iloc[-5:] - l.iloc[-5:]).mean()
    atr_prior = (h.iloc[-25:-5] - l.iloc[-25:-5]).mean()
    if atr_prior > 0:
        contraction = atr_recent / atr_prior
        if contraction < 0.6:
            score += 12
            reasons.append(f"⏳ تقلب منخفض (x{contraction:.2f})")

    v.score = max(0, min(100, score))
    v.reasons = reasons
    v.confidence = 0.7
    v.raw = {"price_range_pct": last10_price_range, "vol_ratio": last10_vol_ratio,
             "rising_lows": rising_lows}
    return v
