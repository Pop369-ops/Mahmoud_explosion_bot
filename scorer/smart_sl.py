"""
Smart SL Placement — combines multiple protections to avoid stop hunts.

Replaces simple priority-list SL with a comprehensive approach:

  1. Volatility-aware minimum SL distance
     - SL distance must be at least 1.5x the ATR of the higher timeframe
     - Prevents micro-stops on volatile coins

  2. Equal Lows / Equal Highs avoidance
     - If candidate SL is inside an equal-levels cluster, push it deeper
     - Adds 2x ATR buffer below the lowest equal low

  3. Psychological round number avoidance
     - If SL is within 0.3% of a round number ($1.00, $0.50, $0.10, etc.)
     - Move SL slightly past the round number

  4. Higher Timeframe SL context
     - In strong HTF trends, use the HTF swing level not the 5m swing
     - 1h swing low for day trades, 4h swing low for swing trades

  5. Stop hunt buffer (preserved from old logic)
     - max(ATR×buffer_mult, avg_wick × 1.3)

  6. Bounds enforcement
     - sl_min_pct and sl_max_pct guard rails

Returns: SLDecision with final SL price, reasoning, and warnings.
"""
import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from typing import Optional
from core.logger import get_logger
from scorer.equal_levels import (
    detect_equal_lows, detect_equal_highs, is_psychological_level,
    find_safer_sl_below_equal_lows, find_safer_sl_above_equal_highs,
)
from scorer.trend_maturity import calculate_rsi

log = get_logger(__name__)


@dataclass
class SLDecision:
    """Final SL decision with full reasoning."""
    sl_price: float
    sl_distance_pct: float
    method: str                              # how SL was chosen
    logic: str                               # human-readable reasoning
    warnings: list[str] = field(default_factory=list)
    safety_adjustments: list[str] = field(default_factory=list)
    # Diagnostic info
    equal_lows_detected: bool = False
    psychological_level_avoided: bool = False
    htf_context_applied: bool = False
    volatility_floor_applied: bool = False


def _calculate_atr_for_timeframe(df: pd.DataFrame, period: int = 14) -> float:
    """Calculate ATR for a given timeframe dataframe."""
    if df is None or len(df) < period + 1:
        return 0.0
    high = df["h"].astype(float)
    low = df["l"].astype(float)
    close = df["c"].astype(float)
    prev_close = close.shift(1).fillna(close.iloc[0])
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return float(tr.tail(period).mean())


def _calculate_avg_wick(df: pd.DataFrame, direction: str = "long",
                         lookback: int = 15) -> float:
    """Average wick size — used for spike buffer."""
    if df is None or len(df) < lookback:
        return 0.0
    recent = df.tail(lookback)
    opens = recent["o"].astype(float).values
    closes = recent["c"].astype(float).values
    lows = recent["l"].astype(float).values
    highs = recent["h"].astype(float).values
    if direction == "long":
        wicks = [max(0, min(o, c) - l) for o, c, l in zip(opens, closes, lows)]
    else:
        wicks = [max(0, h - max(o, c)) for o, c, h in zip(opens, closes, highs)]
    return sum(wicks) / max(len(wicks), 1)


def calculate_volatility_min_sl(snap, direction: str, mode: str = "day") -> float:
    """
    Returns the MINIMUM SL distance as % of price based on volatility.

    Logic:
      - Take ATR of 1h timeframe
      - Min SL = max(default_min, ATR_1h_pct × 1.5)

    This ensures a $0.50 coin with $0.02 ATR (4%) doesn't get a 1% stop.
    """
    default_min = {"scalp": 0.6, "day": 1.0, "swing": 1.8}[mode]

    # Try 1h first, fall back to 15m, then 5m
    atr_pct = 0.0
    if snap.klines_1h is not None and len(snap.klines_1h) >= 20:
        atr_1h = _calculate_atr_for_timeframe(snap.klines_1h, 14)
        atr_pct = (atr_1h / snap.price * 100) if snap.price > 0 else 0
    elif snap.klines_15m is not None and len(snap.klines_15m) >= 20:
        atr_15m = _calculate_atr_for_timeframe(snap.klines_15m, 14)
        # 15m ATR scaled up to ~hourly
        atr_pct = (atr_15m / snap.price * 100) * 2.5 if snap.price > 0 else 0

    if atr_pct <= 0:
        return default_min

    # Min SL must be 1.5x the hourly volatility, but not less than default
    vol_based_min = atr_pct * 1.5
    return max(default_min, vol_based_min)


def adjust_for_equal_levels(candidate_sl: float, direction: str,
                              df: pd.DataFrame, current_price: float,
                              atr: float) -> tuple[float, Optional[str]]:
    """
    Check if candidate SL falls inside an equal-lows/highs cluster.
    If yes, push SL deeper to escape the danger zone.
    Returns: (adjusted_sl, warning_message_or_None)
    """
    if direction == "long":
        cluster = detect_equal_lows(df, current_price,
                                       tolerance_pct=0.6, lookback=100)
        if not cluster or not cluster.is_trap:
            return candidate_sl, None

        # Check if candidate SL is INSIDE the cluster zone
        # (between cluster_lowest - ATR and cluster_highest)
        cluster_low = cluster.lowest_price
        cluster_high = cluster.highest_price

        # If SL is above the lowest equal low → in danger zone
        if candidate_sl > cluster_low - atr * 0.5:
            safer_sl = find_safer_sl_below_equal_lows(cluster, atr,
                                                         extra_buffer_mult=2.0)
            warning = (f"⚠️ Equal Lows مكتشفة عند ${cluster.avg_price:.6g} "
                        f"({cluster.count} قيعان) — SL تعمّق لتجنب الصيد")
            return safer_sl, warning
        return candidate_sl, None
    else:
        cluster = detect_equal_highs(df, current_price,
                                        tolerance_pct=0.6, lookback=100)
        if not cluster or not cluster.is_trap:
            return candidate_sl, None
        cluster_low = cluster.lowest_price
        cluster_high = cluster.highest_price
        if candidate_sl < cluster_high + atr * 0.5:
            safer_sl = find_safer_sl_above_equal_highs(cluster, atr,
                                                           extra_buffer_mult=2.0)
            warning = (f"⚠️ Equal Highs مكتشفة عند ${cluster.avg_price:.6g} "
                        f"({cluster.count} قمم) — SL تعمّق لتجنب الصيد")
            return safer_sl, warning
        return candidate_sl, None


def adjust_for_psychological_level(candidate_sl: float, direction: str,
                                      atr: float) -> tuple[float, Optional[str]]:
    """
    If SL is very close to a round number (e.g. $1.00, $0.50),
    push it past the round number to avoid stop hunts.
    """
    is_round, round_level = is_psychological_level(candidate_sl, tolerance_pct=0.4)
    if not is_round:
        return candidate_sl, None

    extra_buffer = atr * 0.5
    if direction == "long":
        # SL should be BELOW the round number
        if candidate_sl >= round_level:
            adjusted = round_level - extra_buffer
            return adjusted, f"⚠️ SL كان عند رقم نفسي ${round_level} — تم تعميق"
        else:
            # Already below, but very close
            return candidate_sl, None
    else:
        # SL should be ABOVE the round number
        if candidate_sl <= round_level:
            adjusted = round_level + extra_buffer
            return adjusted, f"⚠️ SL كان عند رقم نفسي ${round_level} — تم تعميق"
        else:
            return candidate_sl, None


def get_htf_swing_sl(snap, direction: str,
                       mode: str = "day") -> Optional[tuple[float, str]]:
    """
    For day/swing modes in strong HTF trend, return the HTF swing level.

    Returns: (sl_price, source_description) or None
    """
    if mode == "scalp":
        return None  # scalpers stay on 5m

    # Decide which HTF to use
    if mode == "day":
        primary_df = snap.klines_1h
        tf_label = "1h"
    else:  # swing
        primary_df = snap.klines_4h
        tf_label = "4h"

    if primary_df is None or len(primary_df) < 30:
        return None

    # Find most recent swing in the right direction
    from scorer.liquidity_levels import find_swing_lows, find_swing_highs
    if direction == "long":
        swings = find_swing_lows(primary_df, lookback=30, fractal_size=2)
        candidates = [s for s in swings if s.price < snap.price]
        if not candidates:
            return None
        candidates.sort(key=lambda s: s.price, reverse=True)
        # Pick a swing that's reasonable (1-7% below price)
        for s in candidates:
            dist_pct = (snap.price - s.price) / snap.price * 100
            if 1.0 <= dist_pct <= 7.0 and s.strength >= 2:
                return s.price, f"HTF swing low ({tf_label}, قوة {s.strength})"
        return None
    else:
        swings = find_swing_highs(primary_df, lookback=30, fractal_size=2)
        candidates = [s for s in swings if s.price > snap.price]
        if not candidates:
            return None
        candidates.sort(key=lambda s: s.price)
        for s in candidates:
            dist_pct = (s.price - snap.price) / snap.price * 100
            if 1.0 <= dist_pct <= 7.0 and s.strength >= 2:
                return s.price, f"HTF swing high ({tf_label}, قوة {s.strength})"
        return None


def apply_smart_sl_adjustments(initial_sl: float, initial_method: str,
                                  initial_logic: str,
                                  snap, direction: str, mode: str,
                                  atr: float, df: pd.DataFrame) -> SLDecision:
    """
    Main entrypoint: takes the initial SL from liquidity_levels.build_plan()
    and applies all 5 layers of smart adjustments.
    """
    decision = SLDecision(
        sl_price=initial_sl,
        sl_distance_pct=0.0,
        method=initial_method,
        logic=initial_logic,
    )

    current_price = snap.price

    # === Layer 1: Volatility-aware minimum SL ===
    min_sl_pct = calculate_volatility_min_sl(snap, direction, mode)
    if direction == "long":
        max_allowed_sl_price = current_price * (1 - min_sl_pct / 100)
        if initial_sl > max_allowed_sl_price:
            old = initial_sl
            decision.sl_price = max_allowed_sl_price
            decision.volatility_floor_applied = True
            decision.safety_adjustments.append(
                f"📏 SL عُمّق من {((current_price-old)/current_price*100):.2f}% "
                f"إلى {min_sl_pct:.2f}% (حماية volatility)"
            )
            decision.method = f"{initial_method}_vol_floor"
    else:
        min_sl_price = current_price * (1 + min_sl_pct / 100)
        if initial_sl < min_sl_price:
            old = initial_sl
            decision.sl_price = min_sl_price
            decision.volatility_floor_applied = True
            decision.safety_adjustments.append(
                f"📏 SL عُمّق من {((old-current_price)/current_price*100):.2f}% "
                f"إلى {min_sl_pct:.2f}% (حماية volatility)"
            )
            decision.method = f"{initial_method}_vol_floor"

    # === Layer 2: Equal lows/highs avoidance ===
    adjusted, warning = adjust_for_equal_levels(
        decision.sl_price, direction, df, current_price, atr,
    )
    if warning:
        decision.sl_price = adjusted
        decision.equal_lows_detected = True
        decision.safety_adjustments.append(warning)
        decision.method = f"{decision.method}_eq_levels"

    # === Layer 3: Psychological round numbers ===
    adjusted, warning = adjust_for_psychological_level(
        decision.sl_price, direction, atr,
    )
    if warning:
        decision.sl_price = adjusted
        decision.psychological_level_avoided = True
        decision.safety_adjustments.append(warning)

    # === Layer 4: HTF swing context (only if initial SL was ATR-fallback) ===
    if "atr_fallback" in initial_method.lower() or "atr_fallback" in initial_logic.lower():
        htf_result = get_htf_swing_sl(snap, direction, mode)
        if htf_result is not None:
            htf_sl_raw, htf_source = htf_result
            # Apply buffer
            buffer = atr * 1.0
            if direction == "long":
                htf_sl = htf_sl_raw - buffer
                if htf_sl < decision.sl_price:
                    decision.sl_price = htf_sl
                    decision.htf_context_applied = True
                    decision.safety_adjustments.append(
                        f"🔭 SL استخدم {htf_source}"
                    )
                    decision.method = f"htf_{decision.method}"
            else:
                htf_sl = htf_sl_raw + buffer
                if htf_sl > decision.sl_price:
                    decision.sl_price = htf_sl
                    decision.htf_context_applied = True
                    decision.safety_adjustments.append(
                        f"🔭 SL استخدم {htf_source}"
                    )
                    decision.method = f"htf_{decision.method}"

    # === Layer 5: Enforce max SL bound ===
    sl_max_pct = {"scalp": 3.0, "day": 6.0, "swing": 10.0}[mode]
    if direction == "long":
        min_allowed_sl = current_price * (1 - sl_max_pct / 100)
        if decision.sl_price < min_allowed_sl:
            decision.sl_price = min_allowed_sl
            decision.warnings.append(f"⚠️ SL تم تقييده عند -{sl_max_pct}%")
    else:
        max_allowed_sl = current_price * (1 + sl_max_pct / 100)
        if decision.sl_price > max_allowed_sl:
            decision.sl_price = max_allowed_sl
            decision.warnings.append(f"⚠️ SL تم تقييده عند +{sl_max_pct}%")

    # Final distance
    if direction == "long":
        decision.sl_distance_pct = (current_price - decision.sl_price) / current_price * 100
    else:
        decision.sl_distance_pct = (decision.sl_price - current_price) / current_price * 100

    return decision
