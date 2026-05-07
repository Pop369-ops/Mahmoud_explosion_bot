"""
ICT-Enhanced Liquidity-aware SL/TP placement.

Combines all ICT concepts for SL/TP placement:
1. Order Blocks (institutional reference candles)
2. Fair Value Gaps (price imbalance zones)
3. Liquidity Sweeps (recent stop hunts)
4. Swing levels (basic structure)
5. Order Book walls (real-time passive liquidity)

Priority for SL placement:
  1. Behind the most recent unmitigated Order Block + buffer
  2. Behind the swept liquidity level (if recent sweep) + buffer
  3. Behind nearest swing low/high + buffer
  4. Behind a strong order book wall + buffer
  5. ATR fallback (with warning)

Priority for TP placement:
  1. Unfilled FVG in the trade direction (price tends to fill these)
  2. Opposing Order Blocks (counter-side institutional levels)
  3. Opposing swing highs/lows
  4. Order book walls
  5. ATR multiples fallback
"""
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional
from core.logger import get_logger

from scorer.order_blocks import (
    find_order_blocks, find_active_obs_for_long, find_active_obs_for_short, OrderBlock
)
from scorer.fvg import find_fvgs, find_unfilled_above, find_unfilled_below, FairValueGap
from scorer.sweeps import detect_sweeps, has_recent_bullish_sweep, has_recent_bearish_sweep

log = get_logger(__name__)


@dataclass
class LiquidityLevel:
    price: float
    kind: str
    strength: int
    distance_pct: float = 0.0


@dataclass
class LiquidityPlan:
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    sl_pct: float
    rr_tp1: float
    rr_tp2: float
    rr_tp3: float
    sl_logic: str
    tp_logic: str
    method: str
    sweep_detected: bool = False
    ob_count: int = 0
    fvg_count: int = 0
    warnings: list = field(default_factory=list)


def find_swing_lows(df: pd.DataFrame, lookback: int = 50, fractal_size: int = 2) -> list[LiquidityLevel]:
    if len(df) < lookback: lookback = len(df)
    lows = df["l"].astype(float).values[-lookback:]
    swings = []
    for i in range(fractal_size, len(lows) - fractal_size):
        is_swing = all(lows[i] < lows[i - j] for j in range(1, fractal_size + 1)) and \
                   all(lows[i] < lows[i + j] for j in range(1, fractal_size + 1))
        if is_swing:
            level = lows[i]
            tolerance = level * 0.0015
            touches = sum(1 for x in lows if abs(x - level) <= tolerance)
            swings.append(LiquidityLevel(
                price=level, kind="swing_low",
                strength=min(3, max(1, touches // 2 + 1)),
            ))
    swings.sort(key=lambda s: s.price)
    deduped = []
    for s in swings:
        if deduped and (s.price - deduped[-1].price) / deduped[-1].price < 0.003:
            if s.strength > deduped[-1].strength:
                deduped[-1] = s
        else:
            deduped.append(s)
    return deduped


def find_swing_highs(df: pd.DataFrame, lookback: int = 50, fractal_size: int = 2) -> list[LiquidityLevel]:
    if len(df) < lookback: lookback = len(df)
    highs = df["h"].astype(float).values[-lookback:]
    swings = []
    for i in range(fractal_size, len(highs) - fractal_size):
        is_swing = all(highs[i] > highs[i - j] for j in range(1, fractal_size + 1)) and \
                   all(highs[i] > highs[i + j] for j in range(1, fractal_size + 1))
        if is_swing:
            level = highs[i]
            tolerance = level * 0.0015
            touches = sum(1 for x in highs if abs(x - level) <= tolerance)
            swings.append(LiquidityLevel(
                price=level, kind="swing_high",
                strength=min(3, max(1, touches // 2 + 1)),
            ))
    swings.sort(key=lambda s: s.price, reverse=True)
    deduped = []
    for s in swings:
        if deduped and (deduped[-1].price - s.price) / deduped[-1].price < 0.003:
            if s.strength > deduped[-1].strength:
                deduped[-1] = s
        else:
            deduped.append(s)
    return deduped


def find_orderbook_walls(order_book: dict, current_price: float,
                          min_wall_usd: float = 250_000) -> tuple[list[LiquidityLevel], list[LiquidityLevel]]:
    bid_walls, ask_walls = [], []
    if not order_book:
        return bid_walls, ask_walls
    for entry in order_book.get("bids", [])[:50]:
        try:
            price = float(entry[0])
            usd = price * float(entry[1])
            if usd >= min_wall_usd:
                strength = min(3, int(usd / min_wall_usd))
                bid_walls.append(LiquidityLevel(
                    price=price, kind="ob_wall", strength=strength,
                    distance_pct=(current_price - price) / current_price * 100,
                ))
        except (ValueError, IndexError, TypeError):
            continue
    for entry in order_book.get("asks", [])[:50]:
        try:
            price = float(entry[0])
            usd = price * float(entry[1])
            if usd >= min_wall_usd:
                strength = min(3, int(usd / min_wall_usd))
                ask_walls.append(LiquidityLevel(
                    price=price, kind="ob_wall", strength=strength,
                    distance_pct=(price - current_price) / current_price * 100,
                ))
        except (ValueError, IndexError, TypeError):
            continue
    return bid_walls, ask_walls


def compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    if len(df) < period + 1: return 0.0
    h = df["h"].astype(float).values
    l = df["l"].astype(float).values
    c = df["c"].astype(float).values
    tr = [max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1])) for i in range(1, len(c))]
    return float(pd.Series(tr[-period:]).mean()) if len(tr) >= period else 0.0


# ═══════════════════════════════════════════════════════════════
# LONG PLAN with full ICT integration
# ═══════════════════════════════════════════════════════════════

def build_long_plan(price: float, df: pd.DataFrame, order_book: dict,
                     mode: str = "day") -> LiquidityPlan:
    atr = compute_atr(df) or price * 0.005

    # ─── Increased buffers to protect from stop hunts ──
    # Old buffers were too tight — market makers easily wicked through
    if mode == "scalp":
        lookback, buffer_atr_mult = 30, 0.6   # was 0.4
    elif mode == "swing":
        lookback, buffer_atr_mult = 100, 1.0  # was 0.7
    else:
        lookback, buffer_atr_mult = 50, 0.8   # was 0.5

    sl_min_pct = {"scalp": 0.6, "day": 1.0, "swing": 1.8}[mode]   # was 0.5/0.8/1.5
    sl_max_pct = {"scalp": 2.5, "day": 5.0, "swing": 8.0}[mode]   # was 2.0/4.0/7.0

    obs = find_order_blocks(df, lookback=lookback)
    bullish_obs = find_active_obs_for_long(obs, price)
    fvgs = find_fvgs(df, lookback=lookback)
    fvgs_below = find_unfilled_below(fvgs, price)
    fvgs_above = find_unfilled_above(fvgs, price)
    sweeps = detect_sweeps(df, max_candles_back=5)
    bullish_sweep = has_recent_bullish_sweep(sweeps)

    swing_lows = find_swing_lows(df, lookback=lookback)
    swing_highs = find_swing_highs(df, lookback=lookback)
    bid_walls, ask_walls = find_orderbook_walls(order_book or {}, price)

    warnings = []
    sl = 0.0
    sl_logic = ""
    method = "atr_fallback"

    # ─── Stop Hunt Protection ─────────────────────────
    # Look at recent candle wicks to estimate "wick spike" size
    # If recent low has long wicks below, we need wider buffer
    recent_lows = df["l"].astype(float).values[-15:]
    recent_closes = df["c"].astype(float).values[-15:]
    recent_opens = df["o"].astype(float).values[-15:]
    avg_lower_wick = sum(
        max(0, min(o, c) - l) for o, c, l in zip(recent_opens, recent_closes, recent_lows)
    ) / max(len(recent_lows), 1)
    # Spike protection: SL must be below typical wick range
    spike_buffer = avg_lower_wick * 1.3  # 30% extra to be safe

    # P1: Sweep
    if bullish_sweep and bullish_sweep.is_recent:
        sweep_low = bullish_sweep.sweep_extreme
        dist_pct = (price - sweep_low) / price * 100
        if sl_min_pct <= dist_pct <= sl_max_pct:
            buffer = max(atr * buffer_atr_mult * 0.8, spike_buffer)  # was 0.6, no spike
            sl = sweep_low - buffer
            sl_logic = (f"تحت Sweep low @ ${sweep_low:.6g} "
                        f"(rejection {bullish_sweep.rejection_pct*100:.0f}%, قوة {bullish_sweep.strength})")
            method = "ict_premium"

    # P2: Order Block
    if sl == 0 and bullish_obs:
        for ob in bullish_obs:
            dist_pct = (price - ob.low) / price * 100
            if sl_min_pct <= dist_pct <= sl_max_pct and ob.bos_confirmed:
                buffer = max(atr * buffer_atr_mult, spike_buffer)
                sl = ob.low - buffer
                sl_logic = (f"تحت Order Block @ ${ob.low:.6g}-${ob.high:.6g} "
                            f"(displacement {ob.displacement:.1f}× ATR, BOS ✓)")
                method = "ict_premium"
                break
        if sl == 0:
            for ob in bullish_obs:
                dist_pct = (price - ob.low) / price * 100
                if sl_min_pct <= dist_pct <= sl_max_pct:
                    buffer = max(atr * buffer_atr_mult, spike_buffer)
                    sl = ob.low - buffer
                    sl_logic = f"تحت Order Block @ ${ob.low:.6g}-${ob.high:.6g}"
                    method = "ict_standard"
                    break

    # P3: FVG
    if sl == 0 and fvgs_below:
        nearest_fvg = fvgs_below[0]
        dist_pct = (price - nearest_fvg.bottom) / price * 100
        if sl_min_pct <= dist_pct <= sl_max_pct:
            buffer = max(atr * buffer_atr_mult * 0.9, spike_buffer)
            sl = nearest_fvg.bottom - buffer
            sl_logic = f"تحت FVG @ ${nearest_fvg.bottom:.6g}-${nearest_fvg.top:.6g}"
            method = "ict_standard"

    # P4: Swing low
    if sl == 0:
        candidates = sorted([s for s in swing_lows if s.price < price],
                            key=lambda s: s.price, reverse=True)
        for s in candidates:
            dist_pct = (price - s.price) / price * 100
            if sl_min_pct <= dist_pct <= sl_max_pct and s.strength >= 1:
                buffer = max(atr * buffer_atr_mult, spike_buffer)
                sl = s.price - buffer
                sl_logic = f"خلف swing low @ ${s.price:.6g} (قوة {s.strength})"
                method = "liquidity"
                break

    # P5: Bid wall
    if sl == 0:
        strong_walls = sorted([w for w in bid_walls
                                if sl_min_pct <= w.distance_pct <= sl_max_pct
                                and w.strength >= 2], key=lambda w: w.distance_pct)
        if strong_walls:
            wall = strong_walls[0]
            buffer = max(atr * buffer_atr_mult, spike_buffer)
            sl = wall.price - buffer
            sl_logic = f"خلف جدار شراء ${wall.price:.6g}"
            method = "liquidity"

    # P6: ATR fallback (now wider for protection)
    if sl == 0:
        sl_atr_mult = {"scalp": 2.0, "day": 2.5, "swing": 3.0}[mode]  # was 1.5/2.0/2.5
        sl = price - (atr * sl_atr_mult)
        sl_logic = f"ATR fallback ({sl_atr_mult}× ATR) — لا توجد سيولة واضحة"
        warnings.append("⚠️ SL على ATR — احتمال stop hunt أعلى")
        method = "atr_fallback"

    sl_dist = price - sl
    if sl_dist <= 0 or sl_dist / price > sl_max_pct / 100:
        sl = price * (1 - sl_max_pct / 100)
        sl_dist = price - sl
        warnings.append(f"⚠️ SL تم تقييده عند -{sl_max_pct}%")

    # ═══ TP ═══
    tp_targets: list = []
    for fvg in fvgs_above:
        tp_price = fvg.bottom + (fvg.top - fvg.bottom) * 0.3
        tp_targets.append((tp_price, "FVG"))
    bearish_obs_above = [ob for ob in obs
                          if ob.kind == "bearish_ob" and ob.low > price and not ob.mitigated]
    bearish_obs_above.sort(key=lambda b: b.low)
    for ob in bearish_obs_above[:3]:
        tp_targets.append((ob.low - (atr * 0.30), "OB"))
    for s in sorted([s for s in swing_highs if s.price > price], key=lambda s: s.price):
        tp_targets.append((s.price - (atr * 0.30), "swing"))
    for w in sorted([w for w in ask_walls if w.price > price], key=lambda w: w.price):
        tp_price = w.price - (atr * 0.35)
        if not any(abs(tp_price - t[0]) / max(tp_price, 1e-9) < 0.003 for t in tp_targets):
            tp_targets.append((tp_price, "wall"))

    tp_targets = [(p, lbl) for p, lbl in tp_targets if p > price]
    tp_targets.sort(key=lambda x: x[0])

    min_tp1 = price + sl_dist * 1.0
    valid = [t for t in tp_targets if t[0] >= min_tp1]

    tp1 = tp2 = tp3 = 0.0
    tp_sources = []

    if len(valid) >= 1: tp1, l1 = valid[0]; tp_sources.append(l1)
    if len(valid) >= 2: tp2, l2 = valid[1]; tp_sources.append(l2)
    elif tp1 > 0: tp2 = price + sl_dist * 3.0; tp_sources.append("ATR×3")
    if len(valid) >= 3: tp3, l3 = valid[2]; tp_sources.append(l3)
    elif tp2 > 0: tp3 = price + sl_dist * 5.0; tp_sources.append("ATR×5")

    if tp1 == 0:
        tp1 = price + sl_dist * 1.5
        tp2 = price + sl_dist * 3.0
        tp3 = price + sl_dist * 5.0
        tp_logic = "TP بنسب RR ثابتة (لا توجد قمم سيولة)"
        warnings.append("⚠️ لا توجد سيولة فوق")
    else:
        unique_sources = list(dict.fromkeys(tp_sources))
        tp_logic = f"TP عند {' → '.join(unique_sources)}"

    rr1 = (tp1 - price) / sl_dist if sl_dist > 0 else 0
    rr2 = (tp2 - price) / sl_dist if sl_dist > 0 else 0
    rr3 = (tp3 - price) / sl_dist if sl_dist > 0 else 0

    if rr1 < 0.8:
        warnings.append(f"⚠️ R:R ضعيف على TP1 ({rr1:.2f})")

    return LiquidityPlan(
        entry=round(price, 8), sl=round(sl, 8),
        tp1=round(tp1, 8), tp2=round(tp2, 8), tp3=round(tp3, 8),
        sl_pct=round(sl_dist / price * 100, 2),
        rr_tp1=round(rr1, 2), rr_tp2=round(rr2, 2), rr_tp3=round(rr3, 2),
        sl_logic=sl_logic, tp_logic=tp_logic, method=method,
        sweep_detected=bool(bullish_sweep),
        ob_count=len(bullish_obs),
        fvg_count=len(fvgs_above),
        warnings=warnings,
    )


# ═══════════════════════════════════════════════════════════════
# SHORT PLAN
# ═══════════════════════════════════════════════════════════════

def build_short_plan(price: float, df: pd.DataFrame, order_book: dict,
                      mode: str = "day") -> LiquidityPlan:
    atr = compute_atr(df) or price * 0.005

    # ─── Increased buffers to protect from stop hunts ──
    if mode == "scalp":
        lookback, buffer_atr_mult = 30, 0.6   # was 0.4
    elif mode == "swing":
        lookback, buffer_atr_mult = 100, 1.0  # was 0.7
    else:
        lookback, buffer_atr_mult = 50, 0.8   # was 0.5

    sl_min_pct = {"scalp": 0.6, "day": 1.0, "swing": 1.8}[mode]   # was 0.5/0.8/1.5
    sl_max_pct = {"scalp": 2.5, "day": 5.0, "swing": 8.0}[mode]   # was 2.0/4.0/7.0

    obs = find_order_blocks(df, lookback=lookback)
    bearish_obs = find_active_obs_for_short(obs, price)
    fvgs = find_fvgs(df, lookback=lookback)
    fvgs_below = find_unfilled_below(fvgs, price)
    fvgs_above = find_unfilled_above(fvgs, price)
    sweeps = detect_sweeps(df, max_candles_back=5)
    bearish_sweep = has_recent_bearish_sweep(sweeps)

    swing_lows = find_swing_lows(df, lookback=lookback)
    swing_highs = find_swing_highs(df, lookback=lookback)
    bid_walls, ask_walls = find_orderbook_walls(order_book or {}, price)

    warnings = []
    sl = 0.0
    sl_logic = ""
    method = "atr_fallback"

    # ─── Stop Hunt Protection (upper wick) ──
    recent_highs = df["h"].astype(float).values[-15:]
    recent_closes = df["c"].astype(float).values[-15:]
    recent_opens = df["o"].astype(float).values[-15:]
    avg_upper_wick = sum(
        max(0, h - max(o, c)) for o, c, h in zip(recent_opens, recent_closes, recent_highs)
    ) / max(len(recent_highs), 1)
    spike_buffer = avg_upper_wick * 1.3

    if bearish_sweep and bearish_sweep.is_recent:
        sweep_high = bearish_sweep.sweep_extreme
        dist_pct = (sweep_high - price) / price * 100
        if sl_min_pct <= dist_pct <= sl_max_pct:
            buffer = max(atr * buffer_atr_mult * 0.8, spike_buffer)
            sl = sweep_high + buffer
            sl_logic = (f"فوق Sweep high @ ${sweep_high:.6g} "
                        f"(rejection {bearish_sweep.rejection_pct*100:.0f}%)")
            method = "ict_premium"

    if sl == 0 and bearish_obs:
        for ob in bearish_obs:
            dist_pct = (ob.high - price) / price * 100
            if sl_min_pct <= dist_pct <= sl_max_pct and ob.bos_confirmed:
                buffer = max(atr * buffer_atr_mult, spike_buffer)
                sl = ob.high + buffer
                sl_logic = (f"فوق Order Block @ ${ob.low:.6g}-${ob.high:.6g} "
                            f"(displacement {ob.displacement:.1f}× ATR, BOS ✓)")
                method = "ict_premium"
                break
        if sl == 0:
            for ob in bearish_obs:
                dist_pct = (ob.high - price) / price * 100
                if sl_min_pct <= dist_pct <= sl_max_pct:
                    buffer = max(atr * buffer_atr_mult, spike_buffer)
                    sl = ob.high + buffer
                    sl_logic = f"فوق Order Block @ ${ob.low:.6g}-${ob.high:.6g}"
                    method = "ict_standard"
                    break

    if sl == 0 and fvgs_above:
        nearest = fvgs_above[0]
        dist_pct = (nearest.top - price) / price * 100
        if sl_min_pct <= dist_pct <= sl_max_pct:
            buffer = max(atr * buffer_atr_mult * 0.9, spike_buffer)
            sl = nearest.top + buffer
            sl_logic = f"فوق FVG @ ${nearest.bottom:.6g}-${nearest.top:.6g}"
            method = "ict_standard"

    if sl == 0:
        candidates = sorted([s for s in swing_highs if s.price > price], key=lambda s: s.price)
        for s in candidates:
            dist_pct = (s.price - price) / price * 100
            if sl_min_pct <= dist_pct <= sl_max_pct and s.strength >= 1:
                buffer = max(atr * buffer_atr_mult, spike_buffer)
                sl = s.price + buffer
                sl_logic = f"فوق swing high @ ${s.price:.6g}"
                method = "liquidity"
                break

    if sl == 0:
        strong_walls = sorted([w for w in ask_walls
                                if sl_min_pct <= w.distance_pct <= sl_max_pct
                                and w.strength >= 2], key=lambda w: w.distance_pct)
        if strong_walls:
            wall = strong_walls[0]
            buffer = max(atr * buffer_atr_mult, spike_buffer)
            sl = wall.price + buffer
            sl_logic = f"فوق جدار بيع ${wall.price:.6g}"
            method = "liquidity"

    if sl == 0:
        sl_atr_mult = {"scalp": 2.0, "day": 2.5, "swing": 3.0}[mode]  # was 1.5/2.0/2.5
        sl = price + (atr * sl_atr_mult)
        sl_logic = f"ATR fallback ({sl_atr_mult}× ATR)"
        warnings.append("⚠️ SL على ATR — احتمال stop hunt أعلى")
        method = "atr_fallback"

    sl_dist = sl - price
    if sl_dist <= 0 or sl_dist / price > sl_max_pct / 100:
        sl = price * (1 + sl_max_pct / 100)
        sl_dist = sl - price
        warnings.append(f"⚠️ SL تم تقييده عند +{sl_max_pct}%")

    tp_targets: list = []
    for fvg in fvgs_below:
        tp_price = fvg.top - (fvg.top - fvg.bottom) * 0.3
        tp_targets.append((tp_price, "FVG"))
    bullish_obs_below = [ob for ob in obs
                          if ob.kind == "bullish_ob" and ob.high < price and not ob.mitigated]
    bullish_obs_below.sort(key=lambda b: b.high, reverse=True)
    for ob in bullish_obs_below[:3]:
        tp_targets.append((ob.high + (atr * 0.30), "OB"))
    for s in sorted([s for s in swing_lows if s.price < price], key=lambda s: s.price, reverse=True):
        tp_targets.append((s.price + (atr * 0.30), "swing"))
    for w in sorted([w for w in bid_walls if w.price < price], key=lambda w: w.price, reverse=True):
        tp_price = w.price + (atr * 0.35)
        if not any(abs(tp_price - t[0]) / max(tp_price, 1e-9) < 0.003 for t in tp_targets):
            tp_targets.append((tp_price, "wall"))

    tp_targets = [(p, lbl) for p, lbl in tp_targets if p < price]
    tp_targets.sort(key=lambda x: x[0], reverse=True)

    max_tp1 = price - sl_dist * 1.0
    valid = [t for t in tp_targets if t[0] <= max_tp1]

    tp1 = tp2 = tp3 = 0.0
    tp_sources = []

    if len(valid) >= 1: tp1, l1 = valid[0]; tp_sources.append(l1)
    if len(valid) >= 2: tp2, l2 = valid[1]; tp_sources.append(l2)
    elif tp1 > 0: tp2 = price - sl_dist * 3.0; tp_sources.append("ATR×3")
    if len(valid) >= 3: tp3, l3 = valid[2]; tp_sources.append(l3)
    elif tp2 > 0: tp3 = price - sl_dist * 5.0; tp_sources.append("ATR×5")

    if tp1 == 0:
        tp1 = price - sl_dist * 1.5
        tp2 = price - sl_dist * 3.0
        tp3 = price - sl_dist * 5.0
        tp_logic = "TP بنسب RR ثابتة (لا توجد قيعان سيولة)"
        warnings.append("⚠️ لا توجد سيولة تحت")
    else:
        unique = list(dict.fromkeys(tp_sources))
        tp_logic = f"TP عند {' → '.join(unique)}"

    rr1 = (price - tp1) / sl_dist if sl_dist > 0 else 0
    rr2 = (price - tp2) / sl_dist if sl_dist > 0 else 0
    rr3 = (price - tp3) / sl_dist if sl_dist > 0 else 0

    if rr1 < 0.8:
        warnings.append(f"⚠️ R:R ضعيف على TP1 ({rr1:.2f})")

    return LiquidityPlan(
        entry=round(price, 8), sl=round(sl, 8),
        tp1=round(tp1, 8), tp2=round(tp2, 8), tp3=round(tp3, 8),
        sl_pct=round(sl_dist / price * 100, 2),
        rr_tp1=round(rr1, 2), rr_tp2=round(rr2, 2), rr_tp3=round(rr3, 2),
        sl_logic=sl_logic, tp_logic=tp_logic, method=method,
        sweep_detected=bool(bearish_sweep),
        ob_count=len(bearish_obs),
        fvg_count=len(fvgs_below),
        warnings=warnings,
    )


def build_plan(price: float, df: pd.DataFrame, order_book: dict,
                direction: str = "long", mode: str = "day") -> LiquidityPlan:
    try:
        if direction == "short":
            return build_short_plan(price, df, order_book, mode)
        return build_long_plan(price, df, order_book, mode)
    except Exception as e:
        log.warning("liquidity_plan_error", err=str(e))
        atr = compute_atr(df) or price * 0.005
        sl_dist = atr * 1.5
        return LiquidityPlan(
            entry=round(price, 8),
            sl=round(price - sl_dist if direction == "long" else price + sl_dist, 8),
            tp1=round(price + sl_dist * 1.5 if direction == "long" else price - sl_dist * 1.5, 8),
            tp2=round(price + sl_dist * 3.0 if direction == "long" else price - sl_dist * 3.0, 8),
            tp3=round(price + sl_dist * 5.0 if direction == "long" else price - sl_dist * 5.0, 8),
            sl_pct=round(sl_dist / price * 100, 2),
            rr_tp1=1.5, rr_tp2=3.0, rr_tp3=5.0,
            sl_logic="خطأ — تم استخدام ATR",
            tp_logic="ATR fallback",
            method="atr_fallback",
            warnings=[f"⚠️ خطأ في حساب ICT: {type(e).__name__}"],
        )
