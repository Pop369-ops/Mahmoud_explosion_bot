"""
Reversal Detection Engine — catches trend reversals BEFORE confirmation.

Detects 4 critical reversal patterns:

  1. MACD Divergence (most reliable)
     - Bearish: price HH + MACD histogram LH
     - Bullish: price LL + MACD histogram HL

  2. RSI Divergence (multi-TF)
     - Same logic, on RSI

  3. Bollinger Band Walking + Reversal
     - Price walks the upper band for N bars, then closes back inside
     - = exhaustion of the trend, reversal incoming

  4. Volume Climax + Wick Reversal
     - Big volume + huge upper wick + weak close = top
     - Mirror for bottom

Each detector returns a ReversalSignal with strength 0-100.
Aggregator returns combined ReversalReport.

CRITICAL DIFFERENCE from exhaustion.py:
  - exhaustion.py = mostly volume/funding-based (broad signal)
  - reversal_engine.py = price-action + indicator divergences (sharper)
  - Both can fire together for highest confidence
"""
import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from typing import Optional
from core.logger import get_logger
from scorer.trend_maturity import (
    calculate_rsi, calculate_macd, calculate_bollinger,
)

log = get_logger(__name__)


@dataclass
class ReversalSignal:
    name: str
    triggered: bool
    direction: str = "neutral"     # "bearish_reversal" / "bullish_reversal"
    strength: int = 0              # 0-100
    timeframe: str = "1h"
    reason_ar: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class ReversalReport:
    direction: str = "neutral"     # "bearish_reversal" / "bullish_reversal" / "neutral"
    total_strength: int = 0        # 0-100, capped
    signals: list[ReversalSignal] = field(default_factory=list)
    confirms_count: int = 0        # how many timeframes/types confirm

    @property
    def is_strong_reversal(self) -> bool:
        return self.confirms_count >= 2 and self.total_strength >= 50

    @property
    def is_extreme_reversal(self) -> bool:
        return self.confirms_count >= 3 and self.total_strength >= 75


# ============================================================================
# 1. MACD DIVERGENCE
# ============================================================================
def detect_macd_divergence(df: pd.DataFrame, timeframe: str,
                             lookback: int = 30) -> ReversalSignal:
    """
    Bearish: price makes higher high, MACD histogram makes lower high.
    Bullish: price makes lower low, MACD histogram makes higher low.

    We use peaks/troughs in the MACD histogram to find divergences.
    """
    sig = ReversalSignal(name=f"macd_div_{timeframe}", triggered=False,
                          timeframe=timeframe)
    if df is None or len(df) < 50:
        return sig

    closes = df["c"].astype(float)
    macd = calculate_macd(closes)
    hist = macd["histogram_series"]
    if len(hist) < lookback:
        return sig

    work = df.tail(lookback).copy()
    h = work["h"].astype(float).values
    l = work["l"].astype(float).values
    work_hist = hist.tail(lookback).values

    # Find local peaks (for bearish div) and troughs (for bullish div)
    def _peaks(arr, w=3):
        peaks = []
        for i in range(w, len(arr) - w):
            if all(arr[i] > arr[i-j] for j in range(1, w+1)) and \
                all(arr[i] > arr[i+j] for j in range(1, w+1)):
                peaks.append((i, arr[i]))
        return peaks

    def _troughs(arr, w=3):
        troughs = []
        for i in range(w, len(arr) - w):
            if all(arr[i] < arr[i-j] for j in range(1, w+1)) and \
                all(arr[i] < arr[i+j] for j in range(1, w+1)):
                troughs.append((i, arr[i]))
        return troughs

    price_peaks = _peaks(h)
    price_troughs = _troughs(l)

    # Bearish divergence — last 2 price peaks
    if len(price_peaks) >= 2:
        p1_i, p1_v = price_peaks[-2]
        p2_i, p2_v = price_peaks[-1]
        if p2_v > p1_v and p1_i < len(work_hist) and p2_i < len(work_hist):
            h1 = work_hist[p1_i]
            h2 = work_hist[p2_i]
            # Price HH but MACD histogram LH (and second peak weaker)
            if h2 < h1 and h1 > 0:
                price_diff_pct = (p2_v - p1_v) / p1_v * 100
                hist_diff_pct = abs(h1 - h2) / abs(h1) * 100
                strength = min(100, int(40 + price_diff_pct * 2 + hist_diff_pct * 0.5))
                sig.triggered = True
                sig.direction = "bearish_reversal"
                sig.strength = strength
                sig.reason_ar = (f"⚠️ MACD divergence هابط ({timeframe}) — "
                                  f"السعر HH ({p2_v:.4f} > {p1_v:.4f}) لكن MACD أضعف")
                sig.raw = {"price_diff_pct": price_diff_pct,
                           "hist_diff_pct": hist_diff_pct}
                return sig

    # Bullish divergence — last 2 price troughs
    if len(price_troughs) >= 2:
        t1_i, t1_v = price_troughs[-2]
        t2_i, t2_v = price_troughs[-1]
        if t2_v < t1_v and t1_i < len(work_hist) and t2_i < len(work_hist):
            h1 = work_hist[t1_i]
            h2 = work_hist[t2_i]
            # Price LL but MACD histogram HL (less negative)
            if h2 > h1 and h1 < 0:
                price_diff_pct = abs(t2_v - t1_v) / t1_v * 100
                hist_diff_pct = abs(h1 - h2) / abs(h1) * 100
                strength = min(100, int(40 + price_diff_pct * 2 + hist_diff_pct * 0.5))
                sig.triggered = True
                sig.direction = "bullish_reversal"
                sig.strength = strength
                sig.reason_ar = (f"⚠️ MACD divergence صاعد ({timeframe}) — "
                                  f"السعر LL ({t2_v:.4f} < {t1_v:.4f}) لكن MACD أقوى")
                sig.raw = {"price_diff_pct": price_diff_pct,
                           "hist_diff_pct": hist_diff_pct}
                return sig

    return sig


# ============================================================================
# 2. RSI DIVERGENCE
# ============================================================================
def detect_rsi_divergence(df: pd.DataFrame, timeframe: str,
                            lookback: int = 30) -> ReversalSignal:
    """RSI divergence — same logic as MACD div."""
    sig = ReversalSignal(name=f"rsi_div_{timeframe}", triggered=False,
                          timeframe=timeframe)
    if df is None or len(df) < 30:
        return sig

    closes = df["c"].astype(float)
    if len(closes) < 30:
        return sig

    # Compute full RSI series (Wilder)
    delta = closes.diff().dropna()
    gain = delta.where(delta > 0, 0).ewm(alpha=1/14, adjust=False).mean()
    loss = -delta.where(delta < 0, 0).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss.replace(0, 1e-9)
    rsi_series = 100 - (100 / (1 + rs))

    work = df.tail(lookback).copy()
    h = work["h"].astype(float).values
    l = work["l"].astype(float).values
    rsi_work = rsi_series.tail(lookback).values if len(rsi_series) >= lookback else None

    if rsi_work is None or len(rsi_work) < lookback:
        return sig

    def _peaks(arr, w=3):
        peaks = []
        for i in range(w, len(arr) - w):
            if all(arr[i] > arr[i-j] for j in range(1, w+1)) and \
                all(arr[i] > arr[i+j] for j in range(1, w+1)):
                peaks.append((i, arr[i]))
        return peaks

    def _troughs(arr, w=3):
        troughs = []
        for i in range(w, len(arr) - w):
            if all(arr[i] < arr[i-j] for j in range(1, w+1)) and \
                all(arr[i] < arr[i+j] for j in range(1, w+1)):
                troughs.append((i, arr[i]))
        return troughs

    price_peaks = _peaks(h)
    price_troughs = _troughs(l)

    # Bearish: price HH, RSI LH (especially when RSI was overbought)
    if len(price_peaks) >= 2:
        p1_i, p1_v = price_peaks[-2]
        p2_i, p2_v = price_peaks[-1]
        if p2_v > p1_v and p1_i < len(rsi_work) and p2_i < len(rsi_work):
            r1 = rsi_work[p1_i]
            r2 = rsi_work[p2_i]
            if r2 < r1 and r1 >= 65:  # only meaningful if RSI was high
                price_diff = (p2_v - p1_v) / p1_v * 100
                rsi_diff = r1 - r2
                strength = min(100, int(40 + price_diff * 2 + rsi_diff * 1.5))
                sig.triggered = True
                sig.direction = "bearish_reversal"
                sig.strength = strength
                sig.reason_ar = (f"⚠️ RSI divergence هابط ({timeframe}) — "
                                  f"السعر HH لكن RSI ({r2:.0f} < {r1:.0f})")
                sig.raw = {"r1": r1, "r2": r2}
                return sig

    # Bullish: price LL, RSI HL (especially when RSI was oversold)
    if len(price_troughs) >= 2:
        t1_i, t1_v = price_troughs[-2]
        t2_i, t2_v = price_troughs[-1]
        if t2_v < t1_v and t1_i < len(rsi_work) and t2_i < len(rsi_work):
            r1 = rsi_work[t1_i]
            r2 = rsi_work[t2_i]
            if r2 > r1 and r1 <= 35:
                price_diff = abs(t2_v - t1_v) / t1_v * 100
                rsi_diff = r2 - r1
                strength = min(100, int(40 + price_diff * 2 + rsi_diff * 1.5))
                sig.triggered = True
                sig.direction = "bullish_reversal"
                sig.strength = strength
                sig.reason_ar = (f"⚠️ RSI divergence صاعد ({timeframe}) — "
                                  f"السعر LL لكن RSI ({r2:.0f} > {r1:.0f})")
                sig.raw = {"r1": r1, "r2": r2}
                return sig

    return sig


# ============================================================================
# 3. BOLLINGER BAND WALKING + REVERSAL
# ============================================================================
def detect_bb_walking_reversal(df: pd.DataFrame, timeframe: str,
                                 walk_bars: int = 5) -> ReversalSignal:
    """
    BB walking = price closing >= upper band for N consecutive bars.
    Reversal trigger = then the next bar closes back inside the band.
    """
    sig = ReversalSignal(name=f"bb_walk_{timeframe}", triggered=False,
                          timeframe=timeframe)
    if df is None or len(df) < 25:
        return sig

    closes = df["c"].astype(float)
    bb = calculate_bollinger(closes, period=20, std_mult=2.0)
    upper_series = bb["upper_series"]
    lower_series = bb["lower_series"]
    middle_series = bb["middle_series"]

    if len(upper_series) < walk_bars + 2:
        return sig

    recent_closes = closes.tail(walk_bars + 1).values
    recent_upper = upper_series.tail(walk_bars + 1).values
    recent_lower = lower_series.tail(walk_bars + 1).values

    # Check for upper band walking (bearish reversal setup)
    walk_count_upper = 0
    for i in range(walk_bars):
        if recent_closes[i] >= recent_upper[i] * 0.995:  # within 0.5%
            walk_count_upper += 1
    # Last bar (current) closes back inside?
    last_close = recent_closes[-1]
    last_upper = recent_upper[-1]
    last_middle = float(middle_series.iloc[-1])

    if walk_count_upper >= 3 and last_close < last_upper * 0.985:
        # Reversal confirmed
        distance_back = (last_upper - last_close) / last_upper * 100
        strength = min(100, int(45 + walk_count_upper * 5 + distance_back * 3))
        sig.triggered = True
        sig.direction = "bearish_reversal"
        sig.strength = strength
        sig.reason_ar = (f"⚠️ BB walking هابط ({timeframe}) — "
                          f"{walk_count_upper} شموع لمست الحد العلوي ثم انعكست")
        sig.raw = {"walk_bars": walk_count_upper, "distance_back": distance_back}
        return sig

    # Check for lower band walking (bullish reversal setup)
    walk_count_lower = 0
    for i in range(walk_bars):
        if recent_closes[i] <= recent_lower[i] * 1.005:
            walk_count_lower += 1

    last_lower = recent_lower[-1]
    if walk_count_lower >= 3 and last_close > last_lower * 1.015:
        distance_back = (last_close - last_lower) / last_lower * 100
        strength = min(100, int(45 + walk_count_lower * 5 + distance_back * 3))
        sig.triggered = True
        sig.direction = "bullish_reversal"
        sig.strength = strength
        sig.reason_ar = (f"⚠️ BB walking صاعد ({timeframe}) — "
                          f"{walk_count_lower} شموع لمست الحد السفلي ثم انعكست")
        sig.raw = {"walk_bars": walk_count_lower, "distance_back": distance_back}
        return sig

    return sig


# ============================================================================
# 4. VOLUME CLIMAX + WICK REVERSAL
# ============================================================================
def detect_volume_climax_wick(df: pd.DataFrame, timeframe: str,
                                lookback: int = 20) -> ReversalSignal:
    """
    Climax candle = volume > 2x avg, big wick, weak close.
    Bearish: huge upper wick + weak close near low = top.
    Bullish: huge lower wick + strong close near high = bottom.
    """
    sig = ReversalSignal(name=f"vol_climax_{timeframe}", triggered=False,
                          timeframe=timeframe)
    if df is None or len(df) < lookback + 1:
        return sig

    last = df.iloc[-1]
    o = float(last["o"])
    h = float(last["h"])
    l = float(last["l"])
    c = float(last["c"])
    v = float(last["qv"]) if "qv" in last else float(last["v"])

    # Average volume
    historical = df.iloc[-(lookback + 1):-1]
    avg_v = float(historical["qv"].astype(float).mean()
                   if "qv" in historical.columns
                   else historical["v"].astype(float).mean())

    if avg_v <= 0 or v < avg_v * 1.8:
        return sig  # not climactic enough

    candle_range = h - l
    if candle_range <= 0:
        return sig

    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    body = abs(c - o)

    upper_wick_pct = upper_wick / candle_range
    lower_wick_pct = lower_wick / candle_range

    vol_mult = v / avg_v

    # Bearish climax: upper wick > 50%, close in lower half
    if upper_wick_pct > 0.50 and (c - l) < candle_range * 0.4:
        strength = min(100, int(50 + upper_wick_pct * 30 + min(20, vol_mult * 3)))
        sig.triggered = True
        sig.direction = "bearish_reversal"
        sig.strength = strength
        sig.reason_ar = (f"🛑 ذروة بيع/wick ({timeframe}) — حجم {vol_mult:.1f}x "
                          f"+ فتيل علوي {upper_wick_pct*100:.0f}%")
        sig.raw = {"upper_wick_pct": upper_wick_pct, "vol_mult": vol_mult}
        return sig

    # Bullish climax: lower wick > 50%, close in upper half
    if lower_wick_pct > 0.50 and (h - c) < candle_range * 0.4:
        strength = min(100, int(50 + lower_wick_pct * 30 + min(20, vol_mult * 3)))
        sig.triggered = True
        sig.direction = "bullish_reversal"
        sig.strength = strength
        sig.reason_ar = (f"🛑 ذروة شراء/wick ({timeframe}) — حجم {vol_mult:.1f}x "
                          f"+ فتيل سفلي {lower_wick_pct*100:.0f}%")
        sig.raw = {"lower_wick_pct": lower_wick_pct, "vol_mult": vol_mult}
        return sig

    return sig


# ============================================================================
# AGGREGATOR
# ============================================================================
def analyze_reversal(snap) -> ReversalReport:
    """
    Run all reversal detectors on multiple timeframes.
    Returns combined report.
    """
    signals: list[ReversalSignal] = []

    # MACD divergence on 1h, 4h
    if snap.klines_1h is not None and len(snap.klines_1h) >= 50:
        signals.append(detect_macd_divergence(snap.klines_1h, "1h"))
        signals.append(detect_rsi_divergence(snap.klines_1h, "1h"))
        signals.append(detect_bb_walking_reversal(snap.klines_1h, "1h"))
        signals.append(detect_volume_climax_wick(snap.klines_1h, "1h"))

    if snap.klines_4h is not None and len(snap.klines_4h) >= 50:
        signals.append(detect_macd_divergence(snap.klines_4h, "4h"))
        signals.append(detect_rsi_divergence(snap.klines_4h, "4h"))

    if snap.klines_1d is not None and len(snap.klines_1d) >= 30:
        signals.append(detect_volume_climax_wick(snap.klines_1d, "1d"))

    triggered = [s for s in signals if s.triggered]

    if not triggered:
        return ReversalReport(direction="neutral", total_strength=0,
                                signals=signals, confirms_count=0)

    # Group by direction
    bearish = [s for s in triggered if s.direction == "bearish_reversal"]
    bullish = [s for s in triggered if s.direction == "bullish_reversal"]

    if len(bearish) > len(bullish):
        direction = "bearish_reversal"
        confirms = len(bearish)
        # Cap total at 100, weighted average favoring stronger signals
        total = min(100, int(sum(s.strength for s in bearish) / max(len(bearish), 1)))
        if confirms >= 2:
            total = min(100, total + (confirms - 1) * 10)  # bonus for multiple confirms
    elif len(bullish) > len(bearish):
        direction = "bullish_reversal"
        confirms = len(bullish)
        total = min(100, int(sum(s.strength for s in bullish) / max(len(bullish), 1)))
        if confirms >= 2:
            total = min(100, total + (confirms - 1) * 10)
    else:
        # Mixed signals — neutral
        return ReversalReport(direction="neutral", total_strength=0,
                                signals=signals, confirms_count=0)

    return ReversalReport(
        direction=direction, total_strength=total,
        signals=signals, confirms_count=confirms,
    )


def render_reversal_summary(report: ReversalReport) -> str:
    """Build a short Arabic summary for warnings/alerts."""
    if report.direction == "neutral":
        return ""

    if report.direction == "bearish_reversal":
        title = "🔻 إشارات انعكاس هابط"
    else:
        title = "🔺 إشارات انعكاس صاعد"

    msg = f"{title} (قوة {report.total_strength}/100, {report.confirms_count} تأكيدات)\n"
    triggered = [s for s in report.signals if s.triggered]
    for s in triggered[:4]:
        msg += f"  • {s.reason_ar}\n"
    return msg
