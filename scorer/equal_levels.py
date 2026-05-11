"""
Equal Lows / Equal Highs Detector — ICT Liquidity Pool Identification.

In ICT methodology, market makers HUNT for stops below equal lows
(or above equal highs). These are visible "obvious" levels that
retail traders cluster their stops at.

When 3+ swing lows form at the same price (within tolerance), the
market is almost certain to "sweep" below them to grab liquidity
before reversing. Same for equal highs.

This module identifies these danger zones so SL placement can:
  - Use a MUCH wider buffer below the lowest equal low
  - Or skip the level entirely and go to deeper support
"""
import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from typing import Optional
from core.logger import get_logger

log = get_logger(__name__)


@dataclass
class EqualLevelCluster:
    """A cluster of swing points at approximately the same price."""
    kind: str                          # "equal_lows" or "equal_highs"
    avg_price: float                   # mean price of cluster
    lowest_price: float                # lowest point in cluster (for SL)
    highest_price: float
    count: int                         # how many swings in cluster (3+ = danger)
    timestamps: list = field(default_factory=list)
    strength: int = 0                  # 0-100 = how strong the trap is

    @property
    def is_trap(self) -> bool:
        return self.count >= 3 and self.strength >= 50

    @property
    def danger_zone_price(self) -> float:
        """The price level that needs extra buffer."""
        return self.lowest_price if self.kind == "equal_lows" else self.highest_price


def _find_swing_lows_raw(df: pd.DataFrame, fractal_size: int = 2,
                          lookback: int = 100) -> list[tuple]:
    """Find raw swing lows. Returns list of (index, price, timestamp)."""
    if df is None or len(df) < fractal_size * 2 + 1:
        return []

    work = df.tail(lookback) if len(df) > lookback else df
    lows = work["l"].astype(float).values
    timestamps = work.index if hasattr(work, "index") else list(range(len(work)))

    swings = []
    for i in range(fractal_size, len(lows) - fractal_size):
        is_swing = True
        for j in range(1, fractal_size + 1):
            if lows[i] >= lows[i - j] or lows[i] >= lows[i + j]:
                is_swing = False
                break
        if is_swing:
            ts = timestamps[i] if i < len(timestamps) else i
            swings.append((i, float(lows[i]), ts))
    return swings


def _find_swing_highs_raw(df: pd.DataFrame, fractal_size: int = 2,
                            lookback: int = 100) -> list[tuple]:
    """Find raw swing highs."""
    if df is None or len(df) < fractal_size * 2 + 1:
        return []

    work = df.tail(lookback) if len(df) > lookback else df
    highs = work["h"].astype(float).values
    timestamps = work.index if hasattr(work, "index") else list(range(len(work)))

    swings = []
    for i in range(fractal_size, len(highs) - fractal_size):
        is_swing = True
        for j in range(1, fractal_size + 1):
            if highs[i] <= highs[i - j] or highs[i] <= highs[i + j]:
                is_swing = False
                break
        if is_swing:
            ts = timestamps[i] if i < len(timestamps) else i
            swings.append((i, float(highs[i]), ts))
    return swings


def detect_equal_lows(df: pd.DataFrame, current_price: float,
                       tolerance_pct: float = 0.5,
                       fractal_size: int = 2,
                       lookback: int = 100) -> Optional[EqualLevelCluster]:
    """
    Detect cluster of equal lows below current price.
    Returns the MOST RECENT cluster (closest danger zone).

    tolerance_pct: how close swings must be to count as "equal" (% of price)
    """
    swings = _find_swing_lows_raw(df, fractal_size, lookback)
    if len(swings) < 3:
        return None

    # Filter to swings below current price
    below_swings = [s for s in swings if s[1] < current_price]
    if len(below_swings) < 3:
        return None

    tolerance_abs = current_price * (tolerance_pct / 100)

    # Find clusters using a sliding window approach
    best_cluster = None
    best_score = 0

    for i in range(len(below_swings)):
        ref_price = below_swings[i][1]
        # Find all swings within tolerance of this one
        cluster_members = [s for s in below_swings
                           if abs(s[1] - ref_price) <= tolerance_abs]

        if len(cluster_members) >= 3:
            avg = sum(s[1] for s in cluster_members) / len(cluster_members)
            lowest = min(s[1] for s in cluster_members)
            highest = max(s[1] for s in cluster_members)

            # Strength based on count + how tight the cluster is
            tightness_pct = ((highest - lowest) / avg * 100) if avg > 0 else 1.0
            tightness_score = max(0, 100 - tightness_pct * 50)
            count_score = min(100, len(cluster_members) * 25)
            strength = int((tightness_score + count_score) / 2)

            # Prefer recent clusters (more recent index = more recent in time)
            recency_score = max(s[0] for s in cluster_members)

            combined = strength + recency_score * 0.5
            if combined > best_score:
                best_score = combined
                best_cluster = EqualLevelCluster(
                    kind="equal_lows",
                    avg_price=avg,
                    lowest_price=lowest,
                    highest_price=highest,
                    count=len(cluster_members),
                    timestamps=[s[2] for s in cluster_members],
                    strength=strength,
                )

    return best_cluster


def detect_equal_highs(df: pd.DataFrame, current_price: float,
                        tolerance_pct: float = 0.5,
                        fractal_size: int = 2,
                        lookback: int = 100) -> Optional[EqualLevelCluster]:
    """Mirror of detect_equal_lows for highs above current price."""
    swings = _find_swing_highs_raw(df, fractal_size, lookback)
    if len(swings) < 3:
        return None

    above_swings = [s for s in swings if s[1] > current_price]
    if len(above_swings) < 3:
        return None

    tolerance_abs = current_price * (tolerance_pct / 100)

    best_cluster = None
    best_score = 0

    for i in range(len(above_swings)):
        ref_price = above_swings[i][1]
        cluster_members = [s for s in above_swings
                           if abs(s[1] - ref_price) <= tolerance_abs]

        if len(cluster_members) >= 3:
            avg = sum(s[1] for s in cluster_members) / len(cluster_members)
            lowest = min(s[1] for s in cluster_members)
            highest = max(s[1] for s in cluster_members)

            tightness_pct = ((highest - lowest) / avg * 100) if avg > 0 else 1.0
            tightness_score = max(0, 100 - tightness_pct * 50)
            count_score = min(100, len(cluster_members) * 25)
            strength = int((tightness_score + count_score) / 2)

            recency_score = max(s[0] for s in cluster_members)
            combined = strength + recency_score * 0.5

            if combined > best_score:
                best_score = combined
                best_cluster = EqualLevelCluster(
                    kind="equal_highs",
                    avg_price=avg,
                    lowest_price=lowest,
                    highest_price=highest,
                    count=len(cluster_members),
                    timestamps=[s[2] for s in cluster_members],
                    strength=strength,
                )

    return best_cluster


def is_psychological_level(price: float, tolerance_pct: float = 0.3) -> tuple[bool, float]:
    """
    Check if price is near a round/psychological number.
    Returns (is_round, nearest_round_level).

    Rounds checked:
      - For price < 0.01: nearest 0.001
      - For price < 0.1: nearest 0.01
      - For price < 1: nearest 0.05 or 0.1
      - For price < 10: nearest 0.5 or 1
      - For price < 100: nearest 5 or 10
      - For price < 1000: nearest 50 or 100
      - For price < 10000: nearest 500 or 1000
      - For price >= 10000: nearest 1000 or 5000
    """
    if price <= 0:
        return False, 0.0

    # Determine the round number scale
    if price < 0.01:
        round_levels = [0.001, 0.005]
    elif price < 0.1:
        round_levels = [0.01, 0.05]
    elif price < 1:
        round_levels = [0.05, 0.1]
    elif price < 10:
        round_levels = [0.5, 1.0]
    elif price < 100:
        round_levels = [5.0, 10.0]
    elif price < 1000:
        round_levels = [50.0, 100.0]
    elif price < 10000:
        round_levels = [500.0, 1000.0]
    else:
        round_levels = [1000.0, 5000.0]

    tolerance_abs = price * (tolerance_pct / 100)

    for level_size in round_levels:
        nearest = round(price / level_size) * level_size
        if abs(price - nearest) <= tolerance_abs:
            return True, nearest

    return False, 0.0


def find_safer_sl_below_equal_lows(equal_lows: EqualLevelCluster,
                                     atr: float,
                                     extra_buffer_mult: float = 2.0) -> float:
    """
    Given an equal lows cluster, return a SAFE SL below it.
    Uses 2x the normal ATR buffer to escape the stop hunt zone.
    """
    safe_sl = equal_lows.lowest_price - (atr * extra_buffer_mult)
    return safe_sl


def find_safer_sl_above_equal_highs(equal_highs: EqualLevelCluster,
                                      atr: float,
                                      extra_buffer_mult: float = 2.0) -> float:
    """Mirror for shorts."""
    safe_sl = equal_highs.highest_price + (atr * extra_buffer_mult)
    return safe_sl
