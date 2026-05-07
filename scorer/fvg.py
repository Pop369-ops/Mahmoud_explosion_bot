"""
Fair Value Gaps (FVG) — price imbalances per ICT methodology.

Definition:
A 3-candle pattern where the middle candle's range creates a "gap" between
candle 1 and candle 3 that wasn't traded through. Price tends to return to
fill these gaps because they represent inefficient price discovery.

Bullish FVG: candle1.high < candle3.low → gap = (candle1.high, candle3.low)
Bearish FVG: candle1.low > candle3.high → gap = (candle3.high, candle1.low)

Use cases:
1. Entry zones (price returns to FVG, then bounces)
2. TP targets (opposing FVGs that haven't been filled)
3. Confluence with Order Blocks for highest-probability zones
"""
import pandas as pd
from dataclasses import dataclass
from typing import Optional


@dataclass
class FairValueGap:
    kind: str           # "bullish_fvg" or "bearish_fvg"
    top: float          # upper boundary
    bottom: float       # lower boundary
    candle_idx: int     # index of middle candle
    size_pct: float     # gap size as % of price
    filled: bool = False
    fill_pct: float = 0.0  # how much has been filled

    @property
    def midpoint(self) -> float:
        return (self.top + self.bottom) / 2

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top


def find_fvgs(df: pd.DataFrame, lookback: int = 60,
               min_size_pct: float = 0.15, max_gaps: int = 6) -> list[FairValueGap]:
    """Identify FVGs in the recent N candles."""
    if df is None or len(df) < 5:
        return []

    work_df = df.tail(lookback).reset_index(drop=True)
    h = work_df["h"].astype(float).values
    l = work_df["l"].astype(float).values
    c = work_df["c"].astype(float).values

    gaps: list[FairValueGap] = []

    # Need 3 consecutive candles to form FVG
    for i in range(1, len(work_df) - 1):
        c1_high = h[i - 1]
        c1_low = l[i - 1]
        c3_high = h[i + 1]
        c3_low = l[i + 1]
        mid_close = c[i]

        # ─── Bullish FVG: gap between c1.high and c3.low ──
        if c1_high < c3_low:
            gap_size = c3_low - c1_high
            mid_price = (c3_low + c1_high) / 2
            size_pct = (gap_size / mid_price) * 100 if mid_price > 0 else 0
            if size_pct >= min_size_pct:
                # Check if filled
                filled, fill_pct = _check_fill(
                    work_df, start_idx=i + 1,
                    gap_top=c3_low, gap_bottom=c1_high,
                    direction="bullish",
                )
                gaps.append(FairValueGap(
                    kind="bullish_fvg",
                    top=c3_low, bottom=c1_high,
                    candle_idx=i,
                    size_pct=round(size_pct, 3),
                    filled=filled,
                    fill_pct=round(fill_pct, 2),
                ))

        # ─── Bearish FVG: gap between c1.low and c3.high ──
        elif c1_low > c3_high:
            gap_size = c1_low - c3_high
            mid_price = (c3_high + c1_low) / 2
            size_pct = (gap_size / mid_price) * 100 if mid_price > 0 else 0
            if size_pct >= min_size_pct:
                filled, fill_pct = _check_fill(
                    work_df, start_idx=i + 1,
                    gap_top=c1_low, gap_bottom=c3_high,
                    direction="bearish",
                )
                gaps.append(FairValueGap(
                    kind="bearish_fvg",
                    top=c1_low, bottom=c3_high,
                    candle_idx=i,
                    size_pct=round(size_pct, 3),
                    filled=filled,
                    fill_pct=round(fill_pct, 2),
                ))

    # Recent unfilled first
    gaps.sort(key=lambda g: (g.filled, -g.candle_idx))
    return gaps[:max_gaps]


def _check_fill(df: pd.DataFrame, start_idx: int,
                 gap_top: float, gap_bottom: float, direction: str) -> tuple[bool, float]:
    if start_idx >= len(df):
        return False, 0.0
    h = df["h"].astype(float).values
    l = df["l"].astype(float).values
    gap_size = gap_top - gap_bottom
    if gap_size <= 0:
        return False, 0.0
    max_fill = 0.0
    for j in range(start_idx, len(df)):
        if direction == "bullish":
            # Bullish FVG fills when price comes DOWN into it
            if l[j] <= gap_bottom:
                return True, 1.0
            if l[j] <= gap_top:
                fill = (gap_top - l[j]) / gap_size
                max_fill = max(max_fill, min(1.0, fill))
        else:
            if h[j] >= gap_top:
                return True, 1.0
            if h[j] >= gap_bottom:
                fill = (h[j] - gap_bottom) / gap_size
                max_fill = max(max_fill, min(1.0, fill))
    return False, max_fill


def find_unfilled_below(gaps: list[FairValueGap], current_price: float) -> list[FairValueGap]:
    """Bullish FVGs below current price = potential support / entry zones."""
    return [g for g in gaps
            if g.kind == "bullish_fvg"
            and g.top < current_price
            and not g.filled]


def find_unfilled_above(gaps: list[FairValueGap], current_price: float) -> list[FairValueGap]:
    """Bearish FVGs above current price = potential resistance / TP targets."""
    return [g for g in gaps
            if g.kind == "bearish_fvg"
            and g.bottom > current_price
            and not g.filled]
