"""
Liquidity-aware SL/TP placement.

Philosophy: Markets HUNT liquidity. Stop losses cluster at obvious levels
(round numbers, recent swing lows, prior support). Smart money sweeps these
levels intentionally to trigger stops, then reverses.

Solution: Place SL BEHIND the liquidity pool, not at it. Place TP at OPPOSING
liquidity pools (where opposing-side stops cluster — those become natural targets).
"""
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from core.logger import get_logger

log = get_logger(__name__)


@dataclass
class LiquidityLevel:
    price: float
    kind: str          # "swing_low", "swing_high", "ob_wall", "round"
    strength: int      # 1-3 (1 = touched once, 3 = strong cluster)
    distance_pct: float = 0.0


@dataclass
class LiquidityPlan:
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    sl_pct: float
    rr_tp1: float       # risk/reward ratio
    rr_tp2: float
    rr_tp3: float
    sl_logic: str       # human-readable reason
    tp_logic: str
    method: str         # "liquidity" or "atr_fallback"
    warnings: list = field(default_factory=list)


def find_swing_lows(df: pd.DataFrame, lookback: int = 50, fractal_size: int = 2) -> list[LiquidityLevel]:
    """Identify swing lows using fractal pattern.

    A swing low is a candle whose LOW is lower than the lows of N candles
    before and N candles after. This finds where price reversed UP — meaning
    buyers stepped in. Stop losses naturally cluster BELOW these.
    """
    if len(df) < lookback:
        lookback = len(df)
    lows = df["l"].astype(float).values[-lookback:]
    swings = []
    for i in range(fractal_size, len(lows) - fractal_size):
        is_swing = all(lows[i] < lows[i - j] for j in range(1, fractal_size + 1)) and \
                   all(lows[i] < lows[i + j] for j in range(1, fractal_size + 1))
        if is_swing:
            # Check if multiple touches near this level (cluster strength)
            level = lows[i]
            tolerance = level * 0.0015  # 0.15% tolerance
            touches = sum(1 for x in lows if abs(x - level) <= tolerance)
            swings.append(LiquidityLevel(
                price=level, kind="swing_low",
                strength=min(3, max(1, touches // 2 + 1)),
            ))
    # Dedupe nearby swings (within 0.3%) — keep strongest
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
    """Swing highs = where sellers stepped in. Sell stops/take profits cluster ABOVE."""
    if len(df) < lookback:
        lookback = len(df)
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
    """Walls in the order book = passive liquidity. Large bids = support, large asks = resistance."""
    bid_walls, ask_walls = [], []
    if not order_book:
        return bid_walls, ask_walls

    bids = order_book.get("bids", [])
    asks = order_book.get("asks", [])

    for price_str, qty_str in bids[:50]:
        try:
            price = float(price_str)
            usd = price * float(qty_str)
            if usd >= min_wall_usd:
                strength = min(3, int(usd / min_wall_usd))
                bid_walls.append(LiquidityLevel(
                    price=price, kind="ob_wall", strength=strength,
                    distance_pct=(current_price - price) / current_price * 100,
                ))
        except (ValueError, IndexError):
            continue

    for price_str, qty_str in asks[:50]:
        try:
            price = float(price_str)
            usd = price * float(qty_str)
            if usd >= min_wall_usd:
                strength = min(3, int(usd / min_wall_usd))
                ask_walls.append(LiquidityLevel(
                    price=price, kind="ob_wall", strength=strength,
                    distance_pct=(price - current_price) / current_price * 100,
                ))
        except (ValueError, IndexError):
            continue

    return bid_walls, ask_walls


def compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    if len(df) < period + 1:
        return 0.0
    h = df["h"].astype(float).values
    l = df["l"].astype(float).values
    c = df["c"].astype(float).values
    tr = [
        max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1]))
        for i in range(1, len(c))
    ]
    return float(pd.Series(tr[-period:]).mean()) if len(tr) >= period else 0.0


def build_long_plan(price: float, df: pd.DataFrame, order_book: dict,
                     mode: str = "day") -> LiquidityPlan:
    """Build SL/TP plan for a LONG position based on liquidity zones.

    SL placement priority:
      1. Below the most recent strong swing low - buffer (sweep protection)
      2. Below a confirmed bid wall - buffer
      3. Fallback: ATR-based with sweep buffer

    TP placement priority:
      1. At swing highs (where sellers/take-profits cluster)
      2. At ask walls
      3. Fallback: ATR multiples
    """
    atr = compute_atr(df)
    if atr == 0:
        atr = price * 0.005  # 0.5% fallback

    # Mode-specific lookback and buffer
    if mode == "scalp":
        lookback, buffer_atr_mult = 30, 0.4
    elif mode == "swing":
        lookback, buffer_atr_mult = 100, 0.7
    else:  # day
        lookback, buffer_atr_mult = 50, 0.5

    swing_lows = find_swing_lows(df, lookback=lookback)
    swing_highs = find_swing_highs(df, lookback=lookback)
    bid_walls, ask_walls = find_orderbook_walls(order_book or {}, price)

    warnings = []
    sl = 0.0
    sl_logic = ""

    # ─── SL: find liquidity to hide BEHIND ───────────────
    candidate_lows = [s for s in swing_lows if s.price < price]
    candidate_lows.sort(key=lambda s: s.price, reverse=True)  # closest below first

    # Want SL between 0.6% and 4% away (mode-dependent)
    sl_min_pct = {"scalp": 0.5, "day": 0.8, "swing": 1.5}[mode]
    sl_max_pct = {"scalp": 2.0, "day": 4.0, "swing": 7.0}[mode]

    chosen_swing = None
    for s in candidate_lows:
        dist_pct = (price - s.price) / price * 100
        if sl_min_pct <= dist_pct <= sl_max_pct and s.strength >= 1:
            chosen_swing = s
            break

    if chosen_swing:
        # Place SL BELOW the swing low with buffer (avoid sweeps)
        buffer = atr * buffer_atr_mult
        sl = chosen_swing.price - buffer
        sl_logic = (f"خلف swing low @ ${chosen_swing.price:.6g} "
                    f"(قوة {chosen_swing.strength}) - buffer {buffer_atr_mult:.1f}×ATR")
    else:
        # Try bid wall as anchor
        strong_bid_walls = [w for w in bid_walls
                             if sl_min_pct <= w.distance_pct <= sl_max_pct
                             and w.strength >= 2]
        strong_bid_walls.sort(key=lambda w: w.distance_pct)
        if strong_bid_walls:
            wall = strong_bid_walls[0]
            buffer = atr * buffer_atr_mult
            sl = wall.price - buffer
            sl_logic = (f"خلف جدار شراء ${wall.price:.6g} "
                        f"(${wall.strength * 250}K+) - buffer")
        else:
            # ATR fallback with extra sweep buffer
            sl_atr_mult = {"scalp": 1.5, "day": 2.0, "swing": 2.5}[mode]
            sl = price - (atr * sl_atr_mult)
            sl_logic = f"ATR fallback ({sl_atr_mult}× ATR) — لا توجد سيولة واضحة"
            warnings.append("⚠️ SL على ATR (سيولة غير واضحة) — قد يضرب وينعكس")

    sl_dist = price - sl
    if sl_dist <= 0 or sl_dist / price > sl_max_pct / 100:
        # Sanity bound
        sl = price * (1 - sl_max_pct / 100)
        sl_dist = price - sl
        warnings.append(f"⚠️ SL تم تقييده عند -{sl_max_pct}%")

    # ─── TP: target opposing liquidity ───────────────────
    candidate_highs = [s for s in swing_highs if s.price > price]
    candidate_highs.sort(key=lambda s: s.price)

    # Filter walls above price as additional TP candidates
    upper_walls = [w for w in ask_walls if w.price > price]
    upper_walls.sort(key=lambda w: w.price)

    # Combine and dedupe TP candidates
    tp_candidates: list[LiquidityLevel] = []
    for s in candidate_highs:
        tp_candidates.append(s)
    for w in upper_walls:
        # Skip if too close to an existing swing high
        if not any(abs(s.price - w.price) / w.price < 0.005 for s in candidate_highs):
            tp_candidates.append(w)
    tp_candidates.sort(key=lambda s: s.price)

    tp1 = tp2 = tp3 = 0.0
    tp_logic = ""

    # Need RR >= 1.0 minimum on TP1
    min_tp1 = price + sl_dist * 1.0
    valid_tp1_candidates = [t for t in tp_candidates if t.price >= min_tp1]

    if len(valid_tp1_candidates) >= 1:
        tp1 = valid_tp1_candidates[0].price
        # Subtract small buffer from TP (exit BEFORE liquidity, not at it)
        tp1 = tp1 - (atr * 0.15)

    if len(valid_tp1_candidates) >= 2:
        tp2 = valid_tp1_candidates[1].price - (atr * 0.15)
    elif tp1 > 0:
        tp2 = price + sl_dist * 3.0

    if len(valid_tp1_candidates) >= 3:
        tp3 = valid_tp1_candidates[2].price - (atr * 0.15)
    elif tp2 > 0:
        tp3 = price + sl_dist * 5.0

    if tp1 == 0:
        # No liquidity above — pure ATR
        tp1 = price + sl_dist * 1.5
        tp2 = price + sl_dist * 3.0
        tp3 = price + sl_dist * 5.0
        tp_logic = "TP بنسب RR ثابتة (لا سيولة واضحة فوق)"
        warnings.append("⚠️ لا توجد قمم سيولة واضحة — TP محتمل يكون بعيد")
    else:
        used = sum(1 for x in [tp1, tp2, tp3] if x > 0)
        target_count = len(valid_tp1_candidates)
        if target_count >= 3:
            tp_logic = f"TP عند 3 قمم سيولة سابقة"
        elif target_count == 2:
            tp_logic = f"TP1/TP2 عند قمم سيولة + TP3 بامتداد ATR"
        else:
            tp_logic = f"TP1 عند قمة سيولة + TP2/TP3 بامتداد ATR"

    # Risk/reward ratios
    rr1 = (tp1 - price) / sl_dist if sl_dist > 0 else 0
    rr2 = (tp2 - price) / sl_dist if sl_dist > 0 else 0
    rr3 = (tp3 - price) / sl_dist if sl_dist > 0 else 0

    if rr1 < 0.8:
        warnings.append(f"⚠️ R:R ضعيف على TP1 ({rr1:.2f}) — أقرب سيولة قريبة جداً")

    return LiquidityPlan(
        entry=round(price, 8),
        sl=round(sl, 8),
        tp1=round(tp1, 8),
        tp2=round(tp2, 8),
        tp3=round(tp3, 8),
        sl_pct=round(sl_dist / price * 100, 2),
        rr_tp1=round(rr1, 2),
        rr_tp2=round(rr2, 2),
        rr_tp3=round(rr3, 2),
        sl_logic=sl_logic,
        tp_logic=tp_logic,
        method="liquidity" if chosen_swing or "خلف" in sl_logic else "atr_fallback",
        warnings=warnings,
    )


def build_short_plan(price: float, df: pd.DataFrame, order_book: dict,
                      mode: str = "day") -> LiquidityPlan:
    """Mirror image for SHORT positions: SL above swing highs, TP at swing lows."""
    atr = compute_atr(df)
    if atr == 0:
        atr = price * 0.005

    if mode == "scalp":
        lookback, buffer_atr_mult = 30, 0.4
    elif mode == "swing":
        lookback, buffer_atr_mult = 100, 0.7
    else:
        lookback, buffer_atr_mult = 50, 0.5

    swing_lows = find_swing_lows(df, lookback=lookback)
    swing_highs = find_swing_highs(df, lookback=lookback)
    bid_walls, ask_walls = find_orderbook_walls(order_book or {}, price)

    warnings = []
    sl = 0.0
    sl_logic = ""

    candidate_highs = [s for s in swing_highs if s.price > price]
    candidate_highs.sort(key=lambda s: s.price)

    sl_min_pct = {"scalp": 0.5, "day": 0.8, "swing": 1.5}[mode]
    sl_max_pct = {"scalp": 2.0, "day": 4.0, "swing": 7.0}[mode]

    chosen_swing = None
    for s in candidate_highs:
        dist_pct = (s.price - price) / price * 100
        if sl_min_pct <= dist_pct <= sl_max_pct and s.strength >= 1:
            chosen_swing = s
            break

    if chosen_swing:
        buffer = atr * buffer_atr_mult
        sl = chosen_swing.price + buffer
        sl_logic = (f"فوق swing high @ ${chosen_swing.price:.6g} "
                    f"(قوة {chosen_swing.strength}) + buffer")
    else:
        strong_ask_walls = [w for w in ask_walls
                             if sl_min_pct <= w.distance_pct <= sl_max_pct
                             and w.strength >= 2]
        strong_ask_walls.sort(key=lambda w: w.distance_pct)
        if strong_ask_walls:
            wall = strong_ask_walls[0]
            buffer = atr * buffer_atr_mult
            sl = wall.price + buffer
            sl_logic = f"فوق جدار بيع ${wall.price:.6g} + buffer"
        else:
            sl_atr_mult = {"scalp": 1.5, "day": 2.0, "swing": 2.5}[mode]
            sl = price + (atr * sl_atr_mult)
            sl_logic = f"ATR fallback ({sl_atr_mult}× ATR)"
            warnings.append("⚠️ SL على ATR (سيولة غير واضحة)")

    sl_dist = sl - price
    if sl_dist <= 0 or sl_dist / price > sl_max_pct / 100:
        sl = price * (1 + sl_max_pct / 100)
        sl_dist = sl - price
        warnings.append(f"⚠️ SL تم تقييده عند +{sl_max_pct}%")

    candidate_lows_below = [s for s in swing_lows if s.price < price]
    candidate_lows_below.sort(key=lambda s: s.price, reverse=True)

    lower_walls = [w for w in bid_walls if w.price < price]
    lower_walls.sort(key=lambda w: w.price, reverse=True)

    tp_candidates: list[LiquidityLevel] = []
    for s in candidate_lows_below:
        tp_candidates.append(s)
    for w in lower_walls:
        if not any(abs(s.price - w.price) / w.price < 0.005 for s in candidate_lows_below):
            tp_candidates.append(w)
    tp_candidates.sort(key=lambda s: s.price, reverse=True)

    tp1 = tp2 = tp3 = 0.0
    max_tp1 = price - sl_dist * 1.0
    valid_tp1_candidates = [t for t in tp_candidates if t.price <= max_tp1]

    if valid_tp1_candidates:
        tp1 = valid_tp1_candidates[0].price + (atr * 0.15)
    if len(valid_tp1_candidates) >= 2:
        tp2 = valid_tp1_candidates[1].price + (atr * 0.15)
    elif tp1 > 0:
        tp2 = price - sl_dist * 3.0
    if len(valid_tp1_candidates) >= 3:
        tp3 = valid_tp1_candidates[2].price + (atr * 0.15)
    elif tp2 > 0:
        tp3 = price - sl_dist * 5.0

    if tp1 == 0:
        tp1 = price - sl_dist * 1.5
        tp2 = price - sl_dist * 3.0
        tp3 = price - sl_dist * 5.0
        tp_logic = "TP بنسب RR ثابتة (لا سيولة واضحة تحت)"
        warnings.append("⚠️ لا توجد قيعان سيولة واضحة")
    else:
        target_count = len(valid_tp1_candidates)
        if target_count >= 3:
            tp_logic = "TP عند 3 قيعان سيولة سابقة"
        elif target_count == 2:
            tp_logic = "TP1/TP2 عند قيعان + TP3 بامتداد"
        else:
            tp_logic = "TP1 عند قاع + TP2/TP3 بامتداد"

    rr1 = (price - tp1) / sl_dist if sl_dist > 0 else 0
    rr2 = (price - tp2) / sl_dist if sl_dist > 0 else 0
    rr3 = (price - tp3) / sl_dist if sl_dist > 0 else 0

    if rr1 < 0.8:
        warnings.append(f"⚠️ R:R ضعيف على TP1 ({rr1:.2f})")

    return LiquidityPlan(
        entry=round(price, 8),
        sl=round(sl, 8),
        tp1=round(tp1, 8),
        tp2=round(tp2, 8),
        tp3=round(tp3, 8),
        sl_pct=round(sl_dist / price * 100, 2),
        rr_tp1=round(rr1, 2),
        rr_tp2=round(rr2, 2),
        rr_tp3=round(rr3, 2),
        sl_logic=sl_logic,
        tp_logic=tp_logic,
        method="liquidity" if chosen_swing else "atr_fallback",
        warnings=warnings,
    )


def build_plan(price: float, df: pd.DataFrame, order_book: dict,
                direction: str = "long", mode: str = "day") -> LiquidityPlan:
    """Public entry point. Routes to long or short builder."""
    try:
        if direction == "short":
            return build_short_plan(price, df, order_book, mode)
        return build_long_plan(price, df, order_book, mode)
    except Exception as e:
        log.warning("liquidity_plan_error", err=str(e))
        # Hard fallback to pure ATR
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
            sl_logic="خطأ في حساب السيولة — تم استخدام ATR",
            tp_logic="ATR fallback",
            method="atr_fallback",
            warnings=["⚠️ خطأ في حساب السيولة"],
        )
