"""
Liquidity Sweep Detector — detects when smart money has just swept stops.

A "sweep" happens when:
1. Price spikes BELOW a recent swing low (taking out long stops)
2. Then immediately CLOSES BACK ABOVE the swing low
3. This creates the classic "stop hunt + reversal" pattern

When a sweep is detected, going LONG is high-probability because:
- Liquidity has been collected (long stops are gone)
- Smart money typically reverses after sweeps
- The sweep candle shows clear rejection

Mirror image for shorts after sweep of swing high.

This is one of the highest-edge setups in ICT methodology.
"""
import pandas as pd
from dataclasses import dataclass
from typing import Optional
from core.logger import get_logger

log = get_logger(__name__)


@dataclass
class LiquiditySweep:
    kind: str            # "low_sweep" (bullish) or "high_sweep" (bearish)
    swept_level: float   # the swing low/high that got swept
    sweep_extreme: float # the wick low/high on the sweep candle
    close_price: float   # close of the sweep candle
    candles_ago: int     # how recent (1 = current candle, 2 = previous, etc.)
    rejection_pct: float # how much the wick was rejected (% of full range)
    strength: int        # 1-3, based on rejection size and proximity

    @property
    def is_recent(self) -> bool:
        return self.candles_ago <= 3


def _find_recent_swing_lows(df: pd.DataFrame, lookback: int = 30,
                             fractal_size: int = 2) -> list[tuple[int, float]]:
    """Returns [(idx_in_full_df, price), ...]"""
    if len(df) < fractal_size * 2 + 1:
        return []
    lows = df["l"].astype(float).values
    swings = []
    for i in range(fractal_size, len(lows) - fractal_size):
        is_swing = (
            all(lows[i] < lows[i - j] for j in range(1, fractal_size + 1))
            and all(lows[i] < lows[i + j] for j in range(1, fractal_size + 1))
        )
        if is_swing:
            swings.append((i, float(lows[i])))
    # Last `lookback` candles only — but exclude very recent (last 3) to allow sweep room
    cutoff = len(lows) - 3
    swings = [s for s in swings if s[0] <= cutoff and s[0] >= len(lows) - lookback]
    return swings


def _find_recent_swing_highs(df: pd.DataFrame, lookback: int = 30,
                              fractal_size: int = 2) -> list[tuple[int, float]]:
    if len(df) < fractal_size * 2 + 1:
        return []
    highs = df["h"].astype(float).values
    swings = []
    for i in range(fractal_size, len(highs) - fractal_size):
        is_swing = (
            all(highs[i] > highs[i - j] for j in range(1, fractal_size + 1))
            and all(highs[i] > highs[i + j] for j in range(1, fractal_size + 1))
        )
        if is_swing:
            swings.append((i, float(highs[i])))
    cutoff = len(highs) - 3
    swings = [s for s in swings if s[0] <= cutoff and s[0] >= len(highs) - lookback]
    return swings


def detect_sweeps(df: pd.DataFrame, max_candles_back: int = 5,
                   lookback_for_swings: int = 30) -> list[LiquiditySweep]:
    """Look at the last N candles to find a recent sweep that closed back."""
    if df is None or len(df) < 15:
        return []

    o = df["o"].astype(float).values
    h = df["h"].astype(float).values
    l = df["l"].astype(float).values
    c = df["c"].astype(float).values

    swing_lows = _find_recent_swing_lows(df, lookback=lookback_for_swings)
    swing_highs = _find_recent_swing_highs(df, lookback=lookback_for_swings)

    sweeps: list[LiquiditySweep] = []
    n = len(df)

    # Check the last max_candles_back candles for a sweep event
    for offset in range(1, min(max_candles_back + 1, n)):
        idx = n - offset
        candle_open = o[idx]
        candle_high = h[idx]
        candle_low = l[idx]
        candle_close = c[idx]
        full_range = max(candle_high - candle_low, 1e-9)

        # ─── Bullish sweep: wick below swing low, close back above ──
        for swing_idx, swing_price in swing_lows:
            if swing_idx >= idx: continue  # swing must be before sweep
            # Did this candle's LOW pierce the swing low?
            if candle_low < swing_price and candle_close > swing_price:
                # Compute rejection size (how much of the wick rejected back up)
                wick_below = swing_price - candle_low
                rejection = (candle_close - candle_low) / full_range
                if rejection >= 0.4:  # must be meaningful rejection
                    # Strength scoring
                    pierce_pct = wick_below / candle_low * 100
                    if rejection >= 0.7 and pierce_pct >= 0.15:
                        strength = 3
                    elif rejection >= 0.55:
                        strength = 2
                    else:
                        strength = 1
                    sweeps.append(LiquiditySweep(
                        kind="low_sweep",
                        swept_level=swing_price,
                        sweep_extreme=candle_low,
                        close_price=candle_close,
                        candles_ago=offset,
                        rejection_pct=round(rejection, 2),
                        strength=strength,
                    ))
                    break  # one sweep per candle is enough

        # ─── Bearish sweep: wick above swing high, close back below ──
        for swing_idx, swing_price in swing_highs:
            if swing_idx >= idx: continue
            if candle_high > swing_price and candle_close < swing_price:
                wick_above = candle_high - swing_price
                rejection = (candle_high - candle_close) / full_range
                if rejection >= 0.4:
                    pierce_pct = wick_above / candle_high * 100
                    if rejection >= 0.7 and pierce_pct >= 0.15:
                        strength = 3
                    elif rejection >= 0.55:
                        strength = 2
                    else:
                        strength = 1
                    sweeps.append(LiquiditySweep(
                        kind="high_sweep",
                        swept_level=swing_price,
                        sweep_extreme=candle_high,
                        close_price=candle_close,
                        candles_ago=offset,
                        rejection_pct=round(rejection, 2),
                        strength=strength,
                    ))
                    break

    # Most recent + strongest first
    sweeps.sort(key=lambda s: (s.candles_ago, -s.strength))
    return sweeps


def has_recent_bullish_sweep(sweeps: list[LiquiditySweep]) -> Optional[LiquiditySweep]:
    """Quick helper: does a recent low sweep favor LONG entries?"""
    for s in sweeps:
        if s.kind == "low_sweep" and s.is_recent and s.strength >= 2:
            return s
    return None


def has_recent_bearish_sweep(sweeps: list[LiquiditySweep]) -> Optional[LiquiditySweep]:
    for s in sweeps:
        if s.kind == "high_sweep" and s.is_recent and s.strength >= 2:
            return s
    return None
