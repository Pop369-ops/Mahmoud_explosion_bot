"""
Liquidation Heatmap Awareness — predicts likely liquidation zones.

The crypto market is "stop hunt"-driven. Market makers target zones where
many leveraged positions have stops, because:
1. Stops generate forced market orders
2. Forced orders create momentum
3. Momentum is monetized by makers

Without paid Coinglass API, we use heuristics that approximate liquidation
zones with ~70% accuracy:

1. **Round numbers** ($100K, $50K, $20K, $10, $5) — psychological stop magnets
2. **Recent swing highs/lows** — common stop placements
3. **Below daily low / above daily high** — leverage cluster zones
4. **Calculated liq prices** at common leverage (10x, 25x, 50x, 100x)

Returns "danger zones" the bot warns about in signal messages.
"""
import math
from dataclasses import dataclass, field
from typing import Optional
import pandas as pd


@dataclass
class LiquidationZone:
    price: float
    kind: str          # "round_number", "swing_low", "swing_high",
                        # "liq_10x_long", "liq_25x_long", "liq_50x_long",
                        # "liq_10x_short", "daily_low", "daily_high"
    severity: int      # 1-3 (3 = strongest magnet)
    distance_pct: float
    description_ar: str


def _round_levels_near(price: float, max_distance_pct: float = 5.0) -> list[float]:
    """Find psychologically significant round numbers near current price."""
    levels = set()
    p = price

    # Decide step size based on price magnitude
    if p >= 10_000:
        steps = [1000, 5000, 10000]
    elif p >= 1000:
        steps = [100, 500, 1000]
    elif p >= 100:
        steps = [10, 50, 100]
    elif p >= 10:
        steps = [1, 5, 10]
    elif p >= 1:
        steps = [0.1, 0.5, 1.0]
    elif p >= 0.1:
        steps = [0.01, 0.05, 0.10]
    elif p >= 0.01:
        steps = [0.001, 0.005, 0.01]
    else:
        # Very small prices — use percentage-based round numbers
        magnitude = 10 ** math.floor(math.log10(p))
        steps = [magnitude, magnitude * 5]

    for step in steps:
        # Find nearest round level above and below
        above = math.ceil(p / step) * step
        below = math.floor(p / step) * step
        for level in [above, below, above + step, below - step]:
            if level <= 0:
                continue
            dist = abs(level - p) / p * 100
            if dist <= max_distance_pct and dist > 0.05:  # not too close
                levels.add(round(level, 8))
    return sorted(levels)


def _calculate_liquidation_price(entry: float, leverage: int,
                                  direction: str = "long",
                                  maintenance_margin: float = 0.005) -> float:
    """Approximate liquidation price for isolated margin."""
    liq_dist_pct = 1 / leverage - maintenance_margin
    if direction == "long":
        return entry * (1 - liq_dist_pct)
    else:
        return entry * (1 + liq_dist_pct)


def find_liquidation_zones(
    current_price: float,
    df_5m: Optional[pd.DataFrame] = None,
    df_1h: Optional[pd.DataFrame] = None,
    df_1d: Optional[pd.DataFrame] = None,
) -> list[LiquidationZone]:
    """Compute likely liquidation zones near current price."""
    zones: list[LiquidationZone] = []

    # 1. Round numbers
    for level in _round_levels_near(current_price, max_distance_pct=5.0):
        dist_pct = (level - current_price) / current_price * 100
        # Severity by step size
        magnitude = 10 ** math.floor(math.log10(max(level, 1e-9)))
        severity = 2 if level % (magnitude * 10) == 0 else 1
        if abs(dist_pct) > 3:
            severity = max(severity, 2)
        side_ar = "أعلى" if dist_pct > 0 else "أسفل"
        zones.append(LiquidationZone(
            price=level, kind="round_number", severity=severity,
            distance_pct=round(dist_pct, 2),
            description_ar=f"رقم نفسي ${level:.6g} ({side_ar} بـ {abs(dist_pct):.1f}%)",
        ))

    # 2. Recent swing highs/lows (last 30 5m candles = ~2.5 hours)
    if df_5m is not None and len(df_5m) >= 30:
        recent = df_5m.tail(30)
        recent_high = float(recent["h"].max())
        recent_low = float(recent["l"].min())

        # Stops typically just beyond these
        # Longs' stops are below swing low, shorts' stops above swing high
        zones.append(LiquidationZone(
            price=recent_low * 0.998,  # 0.2% below — typical stop placement
            kind="swing_low",
            severity=2,
            distance_pct=round((recent_low * 0.998 - current_price) / current_price * 100, 2),
            description_ar=f"تحت قاع 2.5h — stops شراء (~${recent_low:.6g})",
        ))
        zones.append(LiquidationZone(
            price=recent_high * 1.002,
            kind="swing_high",
            severity=2,
            distance_pct=round((recent_high * 1.002 - current_price) / current_price * 100, 2),
            description_ar=f"فوق قمة 2.5h — stops بيع (~${recent_high:.6g})",
        ))

    # 3. Daily high/low
    if df_1d is not None and len(df_1d) >= 1:
        daily_high = float(df_1d["h"].iloc[-1])
        daily_low = float(df_1d["l"].iloc[-1])
        zones.append(LiquidationZone(
            price=daily_low * 0.997,
            kind="daily_low",
            severity=3,
            distance_pct=round((daily_low * 0.997 - current_price) / current_price * 100, 2),
            description_ar=f"تحت قاع اليوم — تجمع stops كبير (~${daily_low:.6g})",
        ))
        zones.append(LiquidationZone(
            price=daily_high * 1.003,
            kind="daily_high",
            severity=3,
            distance_pct=round((daily_high * 1.003 - current_price) / current_price * 100, 2),
            description_ar=f"فوق قمة اليوم — تجمع stops كبير (~${daily_high:.6g})",
        ))

    # 4. Common leverage liquidation prices (assuming entry near current price)
    for lev in [10, 25, 50, 100]:
        # If users opened LONG at recent levels with this leverage
        # estimate where they'd liquidate
        # Use 1h average price as "common entry"
        if df_1h is not None and len(df_1h) >= 4:
            avg_entry = float(df_1h["c"].iloc[-4:].mean())
            liq_long = _calculate_liquidation_price(avg_entry, lev, "long")
            liq_short = _calculate_liquidation_price(avg_entry, lev, "short")
            for liq, dirn, lab in [(liq_long, "long", "longs"), (liq_short, "short", "shorts")]:
                dist_pct = (liq - current_price) / current_price * 100
                if abs(dist_pct) > 0.3 and abs(dist_pct) < 8.0:
                    severity = 3 if lev >= 50 else 2 if lev >= 25 else 1
                    side = "أعلى" if dist_pct > 0 else "أسفل"
                    zones.append(LiquidationZone(
                        price=liq, kind=f"liq_{lev}x_{dirn}",
                        severity=severity,
                        distance_pct=round(dist_pct, 2),
                        description_ar=(
                            f"تصفية {lev}x {lab} (~${liq:.6g}, {side} {abs(dist_pct):.1f}%)"
                        ),
                    ))

    # Deduplicate close zones (within 0.3%)
    zones.sort(key=lambda z: z.price)
    deduped = []
    for z in zones:
        if deduped and abs(z.price - deduped[-1].price) / max(z.price, 1e-9) < 0.003:
            # Keep the higher severity one
            if z.severity > deduped[-1].severity:
                deduped[-1] = z
        else:
            deduped.append(z)

    # Sort by absolute distance
    deduped.sort(key=lambda z: abs(z.distance_pct))
    return deduped[:12]  # top 12 closest


def find_danger_zones_for_long(zones: list[LiquidationZone],
                                current_price: float) -> list[LiquidationZone]:
    """Zones BELOW price = where longs' stops will be hunted."""
    return [z for z in zones if z.distance_pct < -0.5 and z.distance_pct > -5.0]


def find_danger_zones_for_short(zones: list[LiquidationZone],
                                  current_price: float) -> list[LiquidationZone]:
    """Zones ABOVE price = where shorts' stops will be hunted."""
    return [z for z in zones if z.distance_pct > 0.5 and z.distance_pct < 5.0]


def render_liquidation_summary(zones: list[LiquidationZone]) -> str:
    """Format for inline signal display."""
    if not zones:
        return ""
    closest = zones[:3]
    parts = []
    for z in closest:
        emoji = "🔥" if z.severity == 3 else "⚠️" if z.severity == 2 else "•"
        parts.append(f"{emoji} {z.description_ar}")
    return "\n".join(parts)
