"""
Volume Profile — institutional-grade price acceptance analysis.

Concepts:
- **VPOC (Volume Point of Control):** the single price where most volume traded
  → strongest support/resistance, "fair value"
- **Value Area High (VAH):** upper bound of 70% of volume
- **Value Area Low (VAL):** lower bound of 70% of volume
- **HVN (High Volume Nodes):** price clusters with heavy activity → S/R
- **LVN (Low Volume Nodes):** vacuum zones → price moves through quickly

How professionals use it:
- Trade rejection at VAH/VAL with tight SL
- Use VPOC as ultimate magnet (price always returns)
- LVN zones = breakout targets (price accelerates through)

We compute VP from 5m candles over the last N days (default 5 days).
"""
import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class VolumeProfile:
    vpoc: float = 0.0           # Volume Point of Control
    vah: float = 0.0            # Value Area High (upper 70%)
    val: float = 0.0            # Value Area Low (lower 70%)

    high_volume_nodes: list = field(default_factory=list)   # [(price, volume), ...]
    low_volume_nodes: list = field(default_factory=list)

    range_high: float = 0.0
    range_low: float = 0.0

    current_price: float = 0.0

    # Derived signals
    above_vpoc: bool = False     # price above VPOC = bullish bias
    in_value_area: bool = False  # price between VAL/VAH
    distance_to_vpoc_pct: float = 0.0
    summary_ar: str = ""


def compute_volume_profile(df: pd.DataFrame, num_bins: int = 50,
                             value_area_pct: float = 70.0) -> Optional[VolumeProfile]:
    """Calculate volume profile from OHLCV data.

    Args:
        df: DataFrame with columns 'h', 'l', 'qv' (high, low, quote volume)
        num_bins: how finely to slice the price range (50 = good balance)
        value_area_pct: what % of volume defines the "value area" (default 70)
    """
    if df is None or len(df) < 30:
        return None

    highs = df["h"].astype(float).values
    lows = df["l"].astype(float).values
    volumes = df["qv"].astype(float).values  # quote volume in USD

    range_high = float(highs.max())
    range_low = float(lows.min())
    if range_high <= range_low:
        return None

    # Build price bins
    bins = np.linspace(range_low, range_high, num_bins + 1)
    bin_mids = (bins[:-1] + bins[1:]) / 2
    bin_volumes = np.zeros(num_bins)

    # Distribute each candle's volume across the bins it spans
    for h, l, v in zip(highs, lows, volumes):
        if h <= l or v <= 0:
            continue
        # Find which bins this candle's range covers
        bin_low_idx = np.searchsorted(bins, l) - 1
        bin_high_idx = np.searchsorted(bins, h) - 1
        bin_low_idx = max(0, min(num_bins - 1, bin_low_idx))
        bin_high_idx = max(0, min(num_bins - 1, bin_high_idx))
        n_bins = bin_high_idx - bin_low_idx + 1
        if n_bins > 0:
            v_per_bin = v / n_bins
            bin_volumes[bin_low_idx:bin_high_idx + 1] += v_per_bin

    if bin_volumes.sum() == 0:
        return None

    # VPOC = bin with highest volume
    vpoc_idx = int(np.argmax(bin_volumes))
    vpoc = float(bin_mids[vpoc_idx])

    # Value Area: expand from VPOC until we have value_area_pct of total volume
    total_vol = bin_volumes.sum()
    target_vol = total_vol * (value_area_pct / 100)
    accumulated = bin_volumes[vpoc_idx]
    low_idx = high_idx = vpoc_idx

    while accumulated < target_vol:
        # Expand to whichever side has more volume next
        next_low = bin_volumes[low_idx - 1] if low_idx > 0 else 0
        next_high = bin_volumes[high_idx + 1] if high_idx < num_bins - 1 else 0
        if next_low == 0 and next_high == 0:
            break
        if next_low >= next_high:
            low_idx -= 1
            accumulated += next_low
        else:
            high_idx += 1
            accumulated += next_high

    val = float(bin_mids[low_idx])
    vah = float(bin_mids[high_idx])

    # Find HVN and LVN (top/bottom 20% of bins by volume)
    sorted_indices = np.argsort(bin_volumes)
    n_extremes = max(3, num_bins // 5)
    hvn_indices = sorted_indices[-n_extremes:]
    lvn_indices = sorted_indices[:n_extremes]

    hvn = sorted([(float(bin_mids[i]), float(bin_volumes[i])) for i in hvn_indices],
                  key=lambda x: -x[1])[:5]
    lvn = sorted([(float(bin_mids[i]), float(bin_volumes[i])) for i in lvn_indices],
                  key=lambda x: x[1])[:5]

    current_price = float(df["c"].astype(float).iloc[-1])

    profile = VolumeProfile(
        vpoc=round(vpoc, 8), vah=round(vah, 8), val=round(val, 8),
        high_volume_nodes=hvn,
        low_volume_nodes=lvn,
        range_high=round(range_high, 8),
        range_low=round(range_low, 8),
        current_price=round(current_price, 8),
        above_vpoc=current_price > vpoc,
        in_value_area=val <= current_price <= vah,
        distance_to_vpoc_pct=round((current_price - vpoc) / vpoc * 100, 2),
    )

    # Generate summary
    if profile.in_value_area:
        zone = "داخل منطقة القيمة"
        if profile.above_vpoc:
            zone += " (فوق VPOC — تحيز صاعد)"
        else:
            zone += " (تحت VPOC — تحيز هابط)"
    elif current_price > vah:
        zone = f"فوق VAH @ ${vah:.6g} — overbought (احتمال عودة لـ VAH)"
    else:
        zone = f"تحت VAL @ ${val:.6g} — oversold (احتمال ارتداد لـ VAL)"
    profile.summary_ar = zone
    return profile


def find_vp_for_long(profile: VolumeProfile) -> dict:
    """Suggest VP-based SL/TP for long trades."""
    if profile is None or profile.vpoc == 0:
        return {}
    suggestions = {
        "support_levels": [],
        "resistance_levels": [],
        "summary": profile.summary_ar,
    }
    p = profile.current_price
    # Supports below
    if profile.val < p:
        suggestions["support_levels"].append(("VAL", profile.val))
    if profile.vpoc < p:
        suggestions["support_levels"].append(("VPOC", profile.vpoc))
    for hvn_price, _ in profile.high_volume_nodes:
        if hvn_price < p:
            suggestions["support_levels"].append(("HVN", hvn_price))
    # Resistances above
    if profile.vah > p:
        suggestions["resistance_levels"].append(("VAH", profile.vah))
    if profile.vpoc > p:
        suggestions["resistance_levels"].append(("VPOC", profile.vpoc))
    for hvn_price, _ in profile.high_volume_nodes:
        if hvn_price > p:
            suggestions["resistance_levels"].append(("HVN", hvn_price))
    # Sort by distance
    suggestions["support_levels"].sort(key=lambda x: -x[1])  # highest first (closest to p)
    suggestions["resistance_levels"].sort(key=lambda x: x[1])  # lowest first
    return suggestions


def render_vp_summary(profile: VolumeProfile) -> str:
    if profile is None or profile.vpoc == 0:
        return ""
    m = f"📊 Volume Profile (آخر 5 أيام):\n"
    m += f"  • VPOC: ${profile.vpoc:.6g}"
    if profile.above_vpoc:
        m += f" (تحت السعر بـ {abs(profile.distance_to_vpoc_pct):.1f}%)\n"
    else:
        m += f" (فوق السعر بـ {abs(profile.distance_to_vpoc_pct):.1f}%)\n"
    m += f"  • VAH: ${profile.vah:.6g} | VAL: ${profile.val:.6g}\n"
    m += f"  • {profile.summary_ar}"
    return m
