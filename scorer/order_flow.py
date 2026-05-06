"""
Order Flow Analysis — CVD divergence detector.

CVD (Cumulative Volume Delta) tracks the running difference between
buy-initiated and sell-initiated volume.

Key insight — DIVERGENCE patterns:
- Price makes new HIGH but CVD makes LOWER high → bearish divergence
  (real buying pressure is weakening even though price rises = hidden distribution)
- Price makes new LOW but CVD makes HIGHER low → bullish divergence
  (selling pressure exhausting even though price falls = hidden accumulation)

These divergences precede most major reversals because they reveal
"smart money" footprints invisible in price alone.

Source: Binance gives us aggressive buy volume per candle (bqv = buy quote volume)
which is enough to compute CVD without WebSocket order book.
"""
import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class OrderFlowSnapshot:
    cvd_current: float = 0.0      # current CVD value
    cvd_change_recent: float = 0.0  # recent change (last 10 candles)

    # Divergence detection
    bullish_divergence: bool = False
    bearish_divergence: bool = False
    divergence_strength: int = 0  # 1-3

    # Buy/Sell aggression
    buy_aggression_pct: float = 50.0   # % of recent volume that was buy-initiated
    sell_aggression_pct: float = 50.0

    # Absorption (rare reversal signal)
    absorption_detected: bool = False
    absorption_ar: str = ""

    # Summary
    summary_ar: str = ""
    score: int = 50   # 0..100, higher = more bullish flow


def compute_cvd_series(df: pd.DataFrame) -> np.ndarray:
    """Compute Cumulative Volume Delta series.
    Delta = buy_vol - sell_vol per candle.
    sell_vol = total_qv - bqv
    """
    if df is None or len(df) < 5:
        return np.array([])
    bqv = df["bqv"].astype(float).values   # buy quote volume
    qv = df["qv"].astype(float).values     # total quote volume
    sell_qv = qv - bqv
    delta = bqv - sell_qv
    cvd = np.cumsum(delta)
    return cvd


def detect_divergence(prices: np.ndarray, cvd: np.ndarray,
                       lookback: int = 30, peak_window: int = 5) -> dict:
    """Find divergence between price and CVD.

    Compares the last 2 swing peaks/troughs in price vs CVD.
    """
    if len(prices) < lookback or len(cvd) < lookback:
        return {"bullish": False, "bearish": False, "strength": 0}

    p_recent = prices[-lookback:]
    c_recent = cvd[-lookback:]

    # Find local highs/lows using simple peak detection
    def _peaks(arr, window):
        peaks = []
        for i in range(window, len(arr) - window):
            if all(arr[i] >= arr[i-j] for j in range(1, window+1)) and \
               all(arr[i] >= arr[i+j] for j in range(1, window+1)):
                peaks.append((i, arr[i]))
        return peaks

    def _troughs(arr, window):
        troughs = []
        for i in range(window, len(arr) - window):
            if all(arr[i] <= arr[i-j] for j in range(1, window+1)) and \
               all(arr[i] <= arr[i+j] for j in range(1, window+1)):
                troughs.append((i, arr[i]))
        return troughs

    price_peaks = _peaks(p_recent, peak_window)
    price_troughs = _troughs(p_recent, peak_window)

    bullish_div = False
    bearish_div = False
    strength = 0

    # Bearish divergence: price HH, CVD LH
    if len(price_peaks) >= 2:
        p1_idx, p1_val = price_peaks[-2]
        p2_idx, p2_val = price_peaks[-1]
        if p2_val > p1_val:  # price made HH
            c1, c2 = c_recent[p1_idx], c_recent[p2_idx]
            if c2 < c1:
                bearish_div = True
                # Strength: how big is the divergence?
                price_pct = (p2_val - p1_val) / p1_val * 100
                cvd_pct = abs(c1 - c2) / max(abs(c1), 1) * 100
                strength = 3 if cvd_pct > 30 and price_pct > 1 else \
                           2 if cvd_pct > 15 else 1

    # Bullish divergence: price LL, CVD HL
    if len(price_troughs) >= 2:
        t1_idx, t1_val = price_troughs[-2]
        t2_idx, t2_val = price_troughs[-1]
        if t2_val < t1_val:  # price made LL
            c1, c2 = c_recent[t1_idx], c_recent[t2_idx]
            if c2 > c1:
                bullish_div = True
                price_pct = abs(t2_val - t1_val) / t1_val * 100
                cvd_pct = abs(c2 - c1) / max(abs(c1), 1) * 100
                strength = max(strength,
                                3 if cvd_pct > 30 and price_pct > 1 else
                                2 if cvd_pct > 15 else 1)

    return {"bullish": bullish_div, "bearish": bearish_div, "strength": strength}


def detect_absorption(df: pd.DataFrame, lookback: int = 5) -> dict:
    """Absorption: high volume traded but price barely moves.
    Indicates strong opposing limit orders absorbing aggression.
    Often precedes reversal.
    """
    if df is None or len(df) < lookback + 5:
        return {"detected": False}

    recent = df.tail(lookback)
    avg_vol = df["qv"].astype(float).iloc[-30:-5].mean()
    recent_vol = recent["qv"].astype(float).sum() / lookback

    if recent_vol < avg_vol * 1.8:
        return {"detected": False}

    # Compute price range over the period
    high = recent["h"].astype(float).max()
    low = recent["l"].astype(float).min()
    open_p = float(recent["o"].iloc[0])
    close_p = float(recent["c"].iloc[-1])
    range_pct = (high - low) / max(open_p, 1e-9) * 100
    move_pct = abs(close_p - open_p) / max(open_p, 1e-9) * 100

    # High volume + small move = absorption
    if range_pct < 1.5 and move_pct < 0.7:
        # Determine which side absorbed
        bqv_recent = recent["bqv"].astype(float).sum()
        sqv_recent = recent["qv"].astype(float).sum() - bqv_recent
        if sqv_recent > bqv_recent * 1.3:
            return {
                "detected": True,
                "side": "buyer_absorption",
                "msg_ar": f"🛡 امتصاص شراء قوي ({recent_vol/avg_vol:.1f}x متوسط الحجم)",
            }
        elif bqv_recent > sqv_recent * 1.3:
            return {
                "detected": True,
                "side": "seller_absorption",
                "msg_ar": f"🛡 امتصاص بيع قوي ({recent_vol/avg_vol:.1f}x متوسط الحجم)",
            }
    return {"detected": False}


def analyze_order_flow(df: pd.DataFrame) -> OrderFlowSnapshot:
    """Full order flow analysis."""
    snap = OrderFlowSnapshot()
    if df is None or len(df) < 30:
        return snap

    cvd = compute_cvd_series(df)
    if len(cvd) == 0:
        return snap

    snap.cvd_current = float(cvd[-1])
    if len(cvd) >= 11:
        snap.cvd_change_recent = float(cvd[-1] - cvd[-11])

    # Buy aggression % over last 10 candles
    recent = df.tail(10)
    total_qv = recent["qv"].astype(float).sum()
    buy_qv = recent["bqv"].astype(float).sum()
    if total_qv > 0:
        snap.buy_aggression_pct = round(buy_qv / total_qv * 100, 1)
        snap.sell_aggression_pct = round(100 - snap.buy_aggression_pct, 1)

    # Divergence
    prices = df["c"].astype(float).values
    div = detect_divergence(prices, cvd)
    snap.bullish_divergence = div["bullish"]
    snap.bearish_divergence = div["bearish"]
    snap.divergence_strength = div["strength"]

    # Absorption
    absorp = detect_absorption(df)
    snap.absorption_detected = absorp["detected"]
    if absorp["detected"]:
        snap.absorption_ar = absorp["msg_ar"]

    # Score (0-100, higher = more bullish flow)
    score = 50
    if snap.buy_aggression_pct >= 60:
        score += 15
    elif snap.buy_aggression_pct >= 55:
        score += 7
    elif snap.buy_aggression_pct <= 40:
        score -= 15
    elif snap.buy_aggression_pct <= 45:
        score -= 7

    if snap.bullish_divergence:
        score += 10 + snap.divergence_strength * 5
    if snap.bearish_divergence:
        score -= 10 + snap.divergence_strength * 5

    if snap.absorption_detected:
        if "buyer" in absorp.get("side", ""):
            score -= 8  # buyers absorbed by sellers = bearish
        else:
            score += 8

    snap.score = max(0, min(100, score))

    # Summary
    parts = []
    parts.append(f"شراء {snap.buy_aggression_pct:.0f}% / بيع {snap.sell_aggression_pct:.0f}%")
    if snap.bullish_divergence:
        parts.append(f"⚡ تباعد إيجابي (قوة {snap.divergence_strength})")
    if snap.bearish_divergence:
        parts.append(f"⚠️ تباعد سلبي (قوة {snap.divergence_strength})")
    if snap.absorption_detected:
        parts.append(snap.absorption_ar)
    snap.summary_ar = " | ".join(parts)
    return snap
