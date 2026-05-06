"""
Market Structure analysis (ICT methodology):

- BOS (Break of Structure): price breaks last swing high (bullish BOS) or
  swing low (bearish BOS) → continuation of trend
- CHoCH (Change of Character): price breaks against the prevailing structure
  → potential trend reversal
- Premium / Discount zones: when price is in upper 50% of recent range,
  it's "premium" (bad to buy, good to sell). Lower 50% = "discount" (good to buy).

This module gives you the current "structure state" so the bot only takes
trades that align with the higher-timeframe structure.
"""
import pandas as pd
from dataclasses import dataclass
from typing import Optional


@dataclass
class StructureState:
    trend: str            # "bullish", "bearish", "ranging"
    last_event: str       # "BOS_bullish", "BOS_bearish", "CHoCH_bullish", "CHoCH_bearish", "none"
    last_swing_high: float
    last_swing_low: float
    range_high: float
    range_low: float
    current_price: float
    zone: str             # "premium" (upper 50%), "discount" (lower 50%), "equilibrium"
    zone_pct: float       # 0..100, where in the range the price is
    bullish_score: int    # -100..+100, higher = more bullish bias

    @property
    def is_premium(self) -> bool:
        return self.zone == "premium"

    @property
    def is_discount(self) -> bool:
        return self.zone == "discount"


def _find_swings(df: pd.DataFrame, fractal: int = 2) -> tuple[list[tuple], list[tuple]]:
    """Returns (highs, lows) where each is [(idx, price), ...]"""
    h = df["h"].astype(float).values
    l = df["l"].astype(float).values
    highs, lows = [], []
    for i in range(fractal, len(df) - fractal):
        if all(h[i] > h[i - j] for j in range(1, fractal + 1)) and \
           all(h[i] > h[i + j] for j in range(1, fractal + 1)):
            highs.append((i, float(h[i])))
        if all(l[i] < l[i - j] for j in range(1, fractal + 1)) and \
           all(l[i] < l[i + j] for j in range(1, fractal + 1)):
            lows.append((i, float(l[i])))
    return highs, lows


def analyze_structure(df: pd.DataFrame, lookback: int = 50) -> Optional[StructureState]:
    if df is None or len(df) < 20:
        return None

    work = df.tail(lookback).reset_index(drop=True)
    highs, lows = _find_swings(work)
    if len(highs) < 2 or len(lows) < 2:
        return None

    closes = work["c"].astype(float).values
    current_price = float(closes[-1])

    last_swing_high = highs[-1][1]
    prev_swing_high = highs[-2][1] if len(highs) >= 2 else last_swing_high
    last_swing_low = lows[-1][1]
    prev_swing_low = lows[-2][1] if len(lows) >= 2 else last_swing_low

    # ─── Detect BOS / CHoCH ──────────────────────────────
    last_event = "none"
    # Get most recent swing — high or low? whichever came last
    last_high_idx = highs[-1][0]
    last_low_idx = lows[-1][0]

    # Was the prior trend bullish (HH after HL) or bearish (LL after LH)?
    prior_trend = "ranging"
    if last_swing_high > prev_swing_high and last_swing_low > prev_swing_low:
        prior_trend = "bullish"
    elif last_swing_high < prev_swing_high and last_swing_low < prev_swing_low:
        prior_trend = "bearish"

    # Did current_price break the last swing high or low?
    if current_price > last_swing_high:
        if prior_trend == "bullish":
            last_event = "BOS_bullish"      # continuation
        else:
            last_event = "CHoCH_bullish"    # reversal
    elif current_price < last_swing_low:
        if prior_trend == "bearish":
            last_event = "BOS_bearish"      # continuation
        else:
            last_event = "CHoCH_bearish"    # reversal

    # ─── Determine current trend ─────────────────────────
    if last_event in ("BOS_bullish", "CHoCH_bullish"):
        trend = "bullish"
    elif last_event in ("BOS_bearish", "CHoCH_bearish"):
        trend = "bearish"
    else:
        trend = prior_trend

    # ─── Premium / Discount zones ────────────────────────
    range_high = float(work["h"].max())
    range_low = float(work["l"].min())
    range_size = max(range_high - range_low, 1e-9)
    zone_pct = (current_price - range_low) / range_size * 100

    if zone_pct >= 60:
        zone = "premium"
    elif zone_pct <= 40:
        zone = "discount"
    else:
        zone = "equilibrium"

    # ─── Bullish score (-100..+100) ──────────────────────
    score = 0
    if trend == "bullish": score += 40
    elif trend == "bearish": score -= 40

    if last_event == "BOS_bullish": score += 25
    elif last_event == "CHoCH_bullish": score += 30
    elif last_event == "BOS_bearish": score -= 25
    elif last_event == "CHoCH_bearish": score -= 30

    if zone == "discount": score += 15
    elif zone == "premium": score -= 15

    if last_swing_low > prev_swing_low: score += 10
    if last_swing_high > prev_swing_high: score += 10
    if last_swing_low < prev_swing_low: score -= 10
    if last_swing_high < prev_swing_high: score -= 10

    score = max(-100, min(100, score))

    return StructureState(
        trend=trend,
        last_event=last_event,
        last_swing_high=last_swing_high,
        last_swing_low=last_swing_low,
        range_high=range_high,
        range_low=range_low,
        current_price=current_price,
        zone=zone,
        zone_pct=round(zone_pct, 1),
        bullish_score=score,
    )
