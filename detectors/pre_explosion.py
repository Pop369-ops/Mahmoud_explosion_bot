"""Pre-explosion detector. Refactored from v3's detect_pre_explosion.

CRITICAL: This detector is for PRE-explosion (accumulation BEFORE move).
It must NEVER fire on coins that already had a major move (distribution!).
"""
import numpy as np
from core.models import SourceVerdict, MarketSnapshot


async def detect_pre_explosion(snap: MarketSnapshot) -> SourceVerdict:
    v = SourceVerdict(name="pre_explosion", score=50, confidence=0.0)

    df = snap.klines
    if df is None or len(df) < 30:
        return v

    # ─── Anti-distribution guards (block on already-pumped coins) ───
    # Guard A: 24h change >15% means the explosion already happened
    if abs(snap.change_24h) > 15:
        v.score = 30
        v.confidence = 0.7
        v.reasons = [f"❌ تحرك 24h ضخم ({snap.change_24h:+.1f}%) — الانفجار حصل بالفعل"]
        return v

    # Guard B: distance from 24h high — wick rejection means distribution
    df_1h = snap.klines_1h
    if df_1h is not None and len(df_1h) >= 24:
        try:
            high_24h = float(df_1h["h"].astype(float).iloc[-24:].max())
            if high_24h > 0:
                drop_from_high = (high_24h - snap.price) / high_24h * 100
                if drop_from_high > 12:
                    v.score = 25
                    v.confidence = 0.75
                    v.reasons = [
                        f"❌ السعر منهار -{drop_from_high:.1f}% من قمة 24h "
                        f"(${high_24h:.6g}) — توزيع وليس تراكم"
                    ]
                    return v
        except Exception:
            pass

    # Guard C: today's daily candle has huge upper wick = distribution
    df_1d = snap.klines_1d
    if df_1d is not None and len(df_1d) >= 1:
        try:
            today = df_1d.iloc[-1]
            d_open = float(today["o"])
            d_high = float(today["h"])
            d_low = float(today["l"])
            d_close = float(today["c"])
            d_range = d_high - d_low
            if d_range > 0:
                upper_wick = d_high - max(d_open, d_close)
                upper_wick_ratio = upper_wick / d_range
                # Huge upper wick + range > 8% = distribution candle
                day_range_pct = d_range / d_low * 100 if d_low > 0 else 0
                if upper_wick_ratio > 0.55 and day_range_pct > 8:
                    v.score = 30
                    v.confidence = 0.7
                    v.reasons = [
                        f"❌ شمعة توزيع يومية (فتيل علوي {upper_wick_ratio*100:.0f}% "
                        f"+ مدى {day_range_pct:.1f}%)"
                    ]
                    return v
        except Exception:
            pass

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
