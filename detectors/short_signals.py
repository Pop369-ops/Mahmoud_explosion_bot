"""
Short Signal Detector — mirrors LONG detection for SHORT setups.

Signals SHORT setups when:
1. Sell volume delta dominates (-30% sell pressure)
2. Strong ask wall pressure (sellers absorbing)
3. Funding overheated long (squeeze coming)
4. Whales depositing to exchanges (distribution)
5. Bearish breakout from compression (volatility expansion DOWN)
6. Distribution patterns (sell volume on rallies)

These are run in addition to existing long detectors.
The scanner picks whichever side scores higher.
"""
import pandas as pd
import numpy as np
from core.models import MarketSnapshot, SourceVerdict
from core.logger import get_logger

log = get_logger(__name__)


async def detect_short_volume_delta(snap: MarketSnapshot) -> SourceVerdict:
    """Detect dominant SELL pressure (mirror of buy delta)."""
    v = SourceVerdict(name="volume_delta", score=0, confidence=0.0)
    if snap.klines is None or len(snap.klines) < 30:
        return v

    df = snap.klines.tail(30)
    bqv = df["bqv"].astype(float).sum()
    qv = df["qv"].astype(float).sum()
    sqv = qv - bqv

    if qv <= 0:
        return v

    sell_ratio = sqv / qv
    delta_pct = (sell_ratio - 0.5) * 200  # 0..100 from 50% to 100% sell

    # Recent acceleration
    recent_5 = df.tail(5)
    recent_qv = recent_5["qv"].astype(float).sum()
    recent_bqv = recent_5["bqv"].astype(float).sum()
    recent_sell_ratio = (recent_qv - recent_bqv) / max(recent_qv, 1)

    score = 0
    reasons = []
    if recent_sell_ratio >= 0.62:
        score = min(100, int(50 + (recent_sell_ratio - 0.5) * 250))
        reasons.append(f"📉 ضغط بيع قوي (delta -{(recent_sell_ratio*100-50):.0f}%)")
    elif sell_ratio >= 0.55:
        score = min(100, int(50 + delta_pct))
        reasons.append(f"📉 بيع مهيمن ({sell_ratio*100:.0f}%)")

    v.score = score
    v.confidence = 0.7 if score >= 60 else 0.5
    v.reasons = reasons
    return v


async def detect_short_order_book(snap: MarketSnapshot) -> SourceVerdict:
    """Detect heavy SELL pressure in order book (asks > bids)."""
    v = SourceVerdict(name="order_book", score=0, confidence=0.0)
    if not snap.order_book:
        return v

    asks = snap.order_book.get("asks", [])[:30]
    bids = snap.order_book.get("bids", [])[:30]
    if not asks or not bids:
        return v

    total_ask_usd = sum(float(a[0]) * float(a[1]) for a in asks)
    total_bid_usd = sum(float(b[0]) * float(b[1]) for b in bids)

    if total_ask_usd <= 0 or total_bid_usd <= 0:
        return v

    ratio = total_ask_usd / max(total_bid_usd, 1)
    if ratio < 1.0:
        return v

    score = min(100, int(40 + ratio * 25))
    v.score = score
    v.confidence = 0.7 if score >= 60 else 0.4
    v.reasons.append(f"📕 ضغط بيع في دفتر الأوامر ({ratio:.2f}x bids)")
    return v


async def detect_short_funding_squeeze(snap: MarketSnapshot) -> SourceVerdict:
    """Funding too high → longs crowded → squeeze (good for short)."""
    v = SourceVerdict(name="funding_divergence", score=0, confidence=0.0)
    fr = snap.funding_rate
    if fr is None:
        return v

    fr_pct = fr * 100
    if fr_pct > 0.05:
        v.score = min(100, 70 + int((fr_pct - 0.05) * 200))
        v.reasons.append(f"💸 Funding مرتفع جداً ({fr_pct:.3f}%) — squeeze قادم")
        v.confidence = 0.8
    elif fr_pct > 0.025:
        v.score = 60
        v.reasons.append(f"💸 Funding مرتفع ({fr_pct:.3f}%) — longs مكدسة")
        v.confidence = 0.6
    return v


async def detect_short_whale_distribution(snap: MarketSnapshot) -> SourceVerdict:
    """Whale depositing to exchanges = preparing to sell."""
    v = SourceVerdict(name="whale_flow", score=0, confidence=0.0)
    if snap.whale_flow is None:
        return v

    inflow = snap.whale_flow.get("inflow_to_exchange_usd", 0)
    outflow = snap.whale_flow.get("outflow_from_exchange_usd", 0)
    netflow = inflow - outflow

    if netflow <= 250_000:
        return v

    # Positive netflow = whales sending to exchanges (bearish)
    score = min(100, int(40 + netflow / 50_000))
    v.score = score
    v.confidence = 0.7 if score >= 60 else 0.5
    v.reasons.append(f"🐋 حيتان تودع للبورصة (+${netflow/1000:.0f}K) — توزيع")
    return v


async def detect_short_squeeze_breakdown(snap: MarketSnapshot) -> SourceVerdict:
    """Volatility expansion DOWN after compression."""
    v = SourceVerdict(name="squeeze_breakout", score=0, confidence=0.0)
    if snap.klines is None or len(snap.klines) < 30:
        return v

    df = snap.klines.tail(30)
    highs = df["h"].astype(float).values
    lows = df["l"].astype(float).values
    closes = df["c"].astype(float).values

    early_atr = (highs[:15] - lows[:15]).mean()
    recent_atr = (highs[-5:] - lows[-5:]).mean()

    if early_atr <= 0 or recent_atr <= 0:
        return v

    expansion = recent_atr / early_atr
    last_close = closes[-1]
    range_high = highs[-15:-5].max()
    range_low = lows[-15:-5].min()

    # Bearish breakout = close below range low after compression
    if expansion >= 1.4 and last_close < range_low:
        breakdown_pct = (range_low - last_close) / range_low * 100
        score = min(100, int(60 + expansion * 15 + breakdown_pct * 5))
        v.score = score
        v.confidence = 0.7
        v.reasons.append(
            f"💥 انفجار هابط وشيك ({breakdown_pct:.2f}%) — كسر التماسك"
        )
    return v


async def detect_short_distribution(snap: MarketSnapshot) -> SourceVerdict:
    """Detect distribution: rising prices on declining buy pressure."""
    v = SourceVerdict(name="pre_explosion", score=0, confidence=0.0)
    if snap.klines is None or len(snap.klines) < 30:
        return v

    df = snap.klines.tail(30)
    closes = df["c"].astype(float).values
    bqv = df["bqv"].astype(float).values
    qv = df["qv"].astype(float).values

    # Compute buy ratio per candle
    buy_ratios = bqv / np.maximum(qv, 1)

    early_ratio = float(buy_ratios[:15].mean())
    recent_ratio = float(buy_ratios[-10:].mean())

    price_change = (closes[-1] - closes[-15]) / closes[-15]

    # Distribution: price up but buy ratio dropped significantly
    if price_change > 0.005 and recent_ratio < early_ratio - 0.05:
        ratio_drop = (early_ratio - recent_ratio) * 100
        score = min(100, int(50 + ratio_drop * 8))
        v.score = score
        v.confidence = 0.7
        v.reasons.append(
            f"🌱 توزيع: السعر يرتفع لكن قوة الشراء تنخفض "
            f"(-{ratio_drop:.0f}%)"
        )
    return v
