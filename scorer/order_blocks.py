"""
ICT Order Blocks (OB) — institutional reference candles.

Definition (ICT methodology):
- Bullish OB: the LAST bearish candle BEFORE a strong bullish move that breaks
  recent structure. Smart money accumulates here. Price often returns to "test"
  this OB before continuing higher.
- Bearish OB: mirror — last bullish candle before a strong bearish move.

What makes a "valid" OB (vs random candle):
1. The displacement after the OB must be significant (≥ 1.5× ATR or ≥ 0.8% body)
2. The move must break a recent swing point (BOS — Break of Structure)
3. The OB must NOT have been "mitigated" yet (price hasn't fully traded back through it)

These zones act as high-probability support/resistance because that's where
real institutional orders sit.
"""
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional
from core.logger import get_logger

log = get_logger(__name__)


@dataclass
class OrderBlock:
    kind: str           # "bullish_ob" or "bearish_ob"
    high: float
    low: float
    open: float
    close: float
    candle_idx: int     # index in dataframe
    displacement: float # how strong the move after this OB was (ATR multiples)
    mitigated: bool = False
    mitigation_pct: float = 0.0  # 0..1, how much of the OB has been retraced
    bos_confirmed: bool = False  # did the move break structure?

    @property
    def midpoint(self) -> float:
        return (self.high + self.low) / 2

    @property
    def height(self) -> float:
        return abs(self.high - self.low)

    def contains(self, price: float) -> bool:
        return self.low <= price <= self.high

    def distance_pct(self, current_price: float) -> float:
        if self.kind == "bullish_ob":
            # Bullish OB sits below price
            return (current_price - self.high) / current_price * 100
        else:
            return (self.low - current_price) / current_price * 100


def _compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    if len(df) < period + 1: return 0.0
    h = df["h"].astype(float).values
    l = df["l"].astype(float).values
    c = df["c"].astype(float).values
    tr = [max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1])) for i in range(1, len(c))]
    return float(pd.Series(tr[-period:]).mean()) if len(tr) >= period else 0.0


def _find_swing_high_before(df: pd.DataFrame, idx: int, lookback: int = 20) -> Optional[float]:
    start = max(0, idx - lookback)
    if start >= idx: return None
    return float(df["h"].iloc[start:idx].max())


def _find_swing_low_before(df: pd.DataFrame, idx: int, lookback: int = 20) -> Optional[float]:
    start = max(0, idx - lookback)
    if start >= idx: return None
    return float(df["l"].iloc[start:idx].min())


def find_order_blocks(df: pd.DataFrame, lookback: int = 60,
                       displacement_atr_mult: float = 1.5,
                       max_blocks: int = 8) -> list[OrderBlock]:
    """Detect bullish and bearish order blocks per ICT definition."""
    if df is None or len(df) < 30:
        return []

    atr = _compute_atr(df)
    if atr == 0:
        return []

    # Restrict to recent candles
    work_df = df.tail(lookback).reset_index(drop=True)
    blocks: list[OrderBlock] = []

    o = work_df["o"].astype(float).values
    h = work_df["h"].astype(float).values
    l = work_df["l"].astype(float).values
    c = work_df["c"].astype(float).values

    # Need at least 4 candles after a candidate OB to confirm displacement
    for i in range(len(work_df) - 4):
        candle_open = o[i]
        candle_high = h[i]
        candle_low = l[i]
        candle_close = c[i]
        is_bearish = candle_close < candle_open
        is_bullish = candle_close > candle_open

        # ─── Bullish OB candidate ────────────────────────
        # Last bearish candle before strong up-move
        if is_bearish:
            # Look at next 3 candles for displacement
            window_high = max(h[i+1:i+5]) if i+5 <= len(h) else max(h[i+1:])
            up_move = window_high - candle_high
            if up_move >= atr * displacement_atr_mult and up_move >= candle_high * 0.005:
                # BOS check: did up_move break a recent swing high before this candle?
                prev_swing_high = _find_swing_high_before(work_df, i, lookback=15)
                bos = bool(prev_swing_high) and window_high > prev_swing_high

                # Mitigation: any candle after this trade fully through OB low?
                mitigated = False
                mitigation_pct = 0.0
                for j in range(i + 1, len(work_df)):
                    if l[j] <= candle_low:
                        mitigated = True
                        mitigation_pct = 1.0
                        break
                    elif l[j] <= candle_high:
                        # partial fill
                        partial = (candle_high - l[j]) / max(candle_high - candle_low, 1e-9)
                        mitigation_pct = max(mitigation_pct, min(1.0, partial))

                blocks.append(OrderBlock(
                    kind="bullish_ob",
                    high=candle_high, low=candle_low,
                    open=candle_open, close=candle_close,
                    candle_idx=i,
                    displacement=up_move / atr,
                    mitigated=mitigated,
                    mitigation_pct=round(mitigation_pct, 2),
                    bos_confirmed=bos,
                ))

        # ─── Bearish OB candidate ────────────────────────
        elif is_bullish:
            window_low = min(l[i+1:i+5]) if i+5 <= len(l) else min(l[i+1:])
            down_move = candle_low - window_low
            if down_move >= atr * displacement_atr_mult and down_move >= candle_low * 0.005:
                prev_swing_low = _find_swing_low_before(work_df, i, lookback=15)
                bos = bool(prev_swing_low) and window_low < prev_swing_low

                mitigated = False
                mitigation_pct = 0.0
                for j in range(i + 1, len(work_df)):
                    if h[j] >= candle_high:
                        mitigated = True
                        mitigation_pct = 1.0
                        break
                    elif h[j] >= candle_low:
                        partial = (h[j] - candle_low) / max(candle_high - candle_low, 1e-9)
                        mitigation_pct = max(mitigation_pct, min(1.0, partial))

                blocks.append(OrderBlock(
                    kind="bearish_ob",
                    high=candle_high, low=candle_low,
                    open=candle_open, close=candle_close,
                    candle_idx=i,
                    displacement=down_move / atr,
                    mitigated=mitigated,
                    mitigation_pct=round(mitigation_pct, 2),
                    bos_confirmed=bos,
                ))

    # Keep most recent unmitigated blocks first
    blocks.sort(key=lambda b: (b.mitigated, -b.candle_idx))
    return blocks[:max_blocks]


def find_active_obs_for_long(blocks: list[OrderBlock], current_price: float) -> list[OrderBlock]:
    """Bullish OBs below current price that are still un-mitigated (or partial)."""
    return [b for b in blocks
            if b.kind == "bullish_ob"
            and b.high < current_price
            and not b.mitigated]


def find_active_obs_for_short(blocks: list[OrderBlock], current_price: float) -> list[OrderBlock]:
    return [b for b in blocks
            if b.kind == "bearish_ob"
            and b.low > current_price
            and not b.mitigated]
