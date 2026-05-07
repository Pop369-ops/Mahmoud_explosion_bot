"""Bollinger squeeze→breakout detector. Ported from v3 (the original 'golden signal')."""
from core.models import SourceVerdict, MarketSnapshot


async def detect_squeeze_breakout(snap: MarketSnapshot) -> SourceVerdict:
    v = SourceVerdict(name="squeeze_breakout", score=50, confidence=0.0)

    df = snap.klines
    if df is None or len(df) < 25:
        return v

    c = df["c"]
    bb_mid = c.rolling(20).mean()
    bb_std = c.rolling(20).std()
    bb_up = bb_mid + 2 * bb_std
    bb_lo = bb_mid - 2 * bb_std

    bb_w_series = ((bb_up - bb_lo) / bb_mid) * 100
    if bb_w_series.iloc[-1] is None:
        return v

    bb_w_now = float(bb_w_series.iloc[-1])
    bb_w_prev_avg = float(bb_w_series.iloc[-15:-2].mean())

    price_now = float(c.iloc[-1])
    price_prev = float(c.iloc[-2])
    bb_up_now = float(bb_up.iloc[-1])
    bb_up_prev = float(bb_up.iloc[-2])

    was_squeeze = bb_w_prev_avg < 1.8

    score = 50
    reasons = []

    if price_now > bb_up_now and price_prev <= bb_up_prev:
        if was_squeeze:
            score = 95
            reasons.append("💎 Squeeze→Breakout — إشارة ذهبية نادرة")
            v.confidence = 0.95
        else:
            score = 75
            reasons.append("🚀 كسر Bollinger العلوي")
            v.confidence = 0.75
    elif price_now > bb_up_now:
        score = 60
        reasons.append("📈 فوق Bollinger العلوي (متأخر قليلاً)")
        v.confidence = 0.6
    elif bb_w_now < 1.5 and was_squeeze:
        score = 70
        reasons.append(f"⏳ Squeeze نشط ({bb_w_now:.2f}%) — انفجار وشيك")
        v.confidence = 0.7
    elif bb_w_now < 2.0:
        score = 55
        v.confidence = 0.5
    elif bb_w_now > 5.0:
        score = 40
        v.confidence = 0.5

    v.score = score
    v.reasons = reasons
    v.raw = {"bb_width": bb_w_now, "bb_width_prev": bb_w_prev_avg, "was_squeeze": was_squeeze}
    return v
