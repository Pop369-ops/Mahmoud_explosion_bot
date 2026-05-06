"""
Liquidity Buildup Detectors — 6 EARLY WARNING signals.

These run on coiled/quiet symbols at 1m granularity to catch the
*moment of awakening* — the 5-30 minute window BEFORE a big move.

Each detector returns a LiquiditySignal:
  - triggered: bool
  - score: contribution to total awakening score (0..100)
  - reason_ar: short Arabic explanation

Aggregation philosophy:
  - 1 signal triggered: noise, ignore
  - 2 signals: suspicion, watch closely
  - 3+ signals (≥75 score): SEND ALERT
  - 4+ signals (≥85 score): HIGH CONFIDENCE alert

Critical: these detect ACCUMULATION, not extension.
A signal is ONLY valid if the coin was QUIET before (Watch List entry).
"""
import pandas as pd
import numpy as np
from typing import Optional
from core.models import LiquiditySignal
from core.logger import get_logger

log = get_logger(__name__)


# ============================================================================
# 1. CVD DIRECTION LOCK — momentum building in one direction
# ============================================================================
def detect_cvd_direction_lock(df_1m: pd.DataFrame, lookback: int = 10) -> LiquiditySignal:
    """
    Detect when 70%+ of recent 1m candles share the same buy/sell pressure.
    """
    if df_1m is None or len(df_1m) < lookback + 5:
        return LiquiditySignal("cvd_lock", False, 0, "")

    recent = df_1m.tail(lookback).copy()
    bqv = recent["bqv"].astype(float).values
    qv = recent["qv"].astype(float).values
    sqv = qv - bqv

    buy_dominant = sum(1 for i in range(lookback) if bqv[i] > sqv[i] * 1.1)
    sell_dominant = sum(1 for i in range(lookback) if sqv[i] > bqv[i] * 1.1)

    buy_pct = buy_dominant / lookback
    sell_pct = sell_dominant / lookback

    if buy_pct >= 0.70:
        avg_ratio = float(np.mean([bqv[i] / max(sqv[i], 1e-9) for i in range(lookback)]))
        score = min(25, int(15 + buy_pct * 10 + min(5, avg_ratio - 1)))
        return LiquiditySignal(
            "cvd_lock", True, score,
            f"🟢 ضغط شراء مستمر ({buy_dominant}/{lookback} شمعة)",
            raw={"direction": "long", "ratio": avg_ratio, "buy_pct": buy_pct},
        )
    elif sell_pct >= 0.70:
        avg_ratio = float(np.mean([sqv[i] / max(bqv[i], 1e-9) for i in range(lookback)]))
        score = min(25, int(15 + sell_pct * 10 + min(5, avg_ratio - 1)))
        return LiquiditySignal(
            "cvd_lock", True, score,
            f"🔴 ضغط بيع مستمر ({sell_dominant}/{lookback} شمعة)",
            raw={"direction": "short", "ratio": avg_ratio, "sell_pct": sell_pct},
        )

    return LiquiditySignal("cvd_lock", False, 0, "")


# ============================================================================
# 2. VOLUME AWAKENING — sudden volume jump from quiet baseline
# ============================================================================
def detect_volume_awakening(df_1m: pd.DataFrame, recent_bars: int = 5,
                              baseline_bars: int = 60) -> LiquiditySignal:
    """Volume awakening = recent N min volume > 2x average of last hour."""
    if df_1m is None or len(df_1m) < baseline_bars + recent_bars:
        return LiquiditySignal("vol_awaken", False, 0, "")

    recent = df_1m.tail(recent_bars)
    baseline = df_1m.tail(baseline_bars + recent_bars).head(baseline_bars)

    recent_avg = float(recent["qv"].astype(float).mean())
    baseline_avg = float(baseline["qv"].astype(float).mean())

    if baseline_avg <= 0:
        return LiquiditySignal("vol_awaken", False, 0, "")

    ratio = recent_avg / baseline_avg

    if ratio < 2.0:
        return LiquiditySignal("vol_awaken", False, 0, "")

    bqv = recent["bqv"].astype(float).sum()
    sqv = recent["qv"].astype(float).sum() - bqv
    direction = "long" if bqv > sqv * 1.2 else ("short" if sqv > bqv * 1.2 else "unknown")

    if ratio >= 5.0:
        score = 25
    elif ratio >= 3.0:
        score = 20
    else:
        score = 15

    dir_emoji = "🟢" if direction == "long" else ("🔴" if direction == "short" else "⚪")
    return LiquiditySignal(
        "vol_awaken", True, score,
        f"{dir_emoji} حجم استيقظ ({ratio:.1f}x المعدل)",
        raw={"ratio": ratio, "direction": direction, "recent_avg": recent_avg},
    )


# ============================================================================
# 3. ORDER BOOK IMBALANCE — bid/ask walls thickening
# ============================================================================
def detect_orderbook_imbalance(order_book: dict) -> LiquiditySignal:
    """Detect when one side of the order book is significantly thicker."""
    if not order_book or "bids" not in order_book or "asks" not in order_book:
        return LiquiditySignal("ob_imbalance", False, 0, "")

    bids = order_book.get("bids", [])
    asks = order_book.get("asks", [])
    if len(bids) < 10 or len(asks) < 10:
        return LiquiditySignal("ob_imbalance", False, 0, "")

    try:
        bid_depth = sum(float(b[0]) * float(b[1]) for b in bids[:20])
        ask_depth = sum(float(a[0]) * float(a[1]) for a in asks[:20])

        if bid_depth <= 0 or ask_depth <= 0:
            return LiquiditySignal("ob_imbalance", False, 0, "")

        ratio = bid_depth / ask_depth

        bid_top5 = sum(float(b[0]) * float(b[1]) for b in bids[:5])
        ask_top5 = sum(float(a[0]) * float(a[1]) for a in asks[:5])
        bid_concentration = bid_top5 / bid_depth if bid_depth > 0 else 0
        ask_concentration = ask_top5 / ask_depth if ask_depth > 0 else 0

        if ratio >= 2.0:
            score = min(25, int(15 + (ratio - 2.0) * 5))
            wall_msg = " + جدران سميكة" if bid_concentration > 0.5 else ""
            return LiquiditySignal(
                "ob_imbalance", True, score,
                f"🟢 طلب أعلى من العرض ({ratio:.1f}:1){wall_msg}",
                raw={"ratio": ratio, "direction": "long",
                     "bid_concentration": bid_concentration},
            )
        elif ratio <= 0.5:
            inv_ratio = 1.0 / ratio
            score = min(25, int(15 + (inv_ratio - 2.0) * 5))
            wall_msg = " + جدران سميكة" if ask_concentration > 0.5 else ""
            return LiquiditySignal(
                "ob_imbalance", True, score,
                f"🔴 عرض أعلى من الطلب ({inv_ratio:.1f}:1){wall_msg}",
                raw={"ratio": ratio, "direction": "short",
                     "ask_concentration": ask_concentration},
            )

        return LiquiditySignal("ob_imbalance", False, 0, "")
    except Exception as e:
        log.debug("ob_imbalance_err", err=str(e))
        return LiquiditySignal("ob_imbalance", False, 0, "")


# ============================================================================
# 4. WHALE INFLOW — large transfers approaching
# ============================================================================
def detect_whale_inflow(whale_data: Optional[dict],
                         min_amount_usd: float = 500_000) -> LiquiditySignal:
    """Detect whale transfers TO/FROM exchanges."""
    if not whale_data:
        return LiquiditySignal("whale_inflow", False, 0, "")

    inflows = whale_data.get("inflow_usd", 0) or 0
    outflows = whale_data.get("outflow_usd", 0) or 0
    largest = whale_data.get("largest_tx_usd", 0) or 0

    if outflows >= min_amount_usd:
        score = min(20, int(10 + (outflows / min_amount_usd) * 3))
        return LiquiditySignal(
            "whale_inflow", True, score,
            f"🟢 خروج whale ${outflows/1e6:.2f}M من البورصة (تجميع)",
            raw={"direction": "long", "outflows": outflows, "largest": largest},
        )
    elif inflows >= min_amount_usd * 1.5:
        score = min(20, int(10 + (inflows / min_amount_usd) * 3))
        return LiquiditySignal(
            "whale_inflow", True, score,
            f"🔴 دخول whale ${inflows/1e6:.2f}M للبورصة (توزيع محتمل)",
            raw={"direction": "short", "inflows": inflows, "largest": largest},
        )

    return LiquiditySignal("whale_inflow", False, 0, "")


# ============================================================================
# 5. OI SLOW BUILD — open interest growing while price stable
# ============================================================================
def detect_oi_slow_build(current_oi: Optional[float], prev_oi: Optional[float],
                          price_change_1h_pct: float) -> LiquiditySignal:
    """OI building up while price doesn't move = positions being established quietly."""
    if not current_oi or not prev_oi or prev_oi <= 0:
        return LiquiditySignal("oi_build", False, 0, "")

    oi_change_pct = (current_oi - prev_oi) / prev_oi * 100

    if oi_change_pct < 15:
        return LiquiditySignal("oi_build", False, 0, "")
    if abs(price_change_1h_pct) > 3:
        return LiquiditySignal("oi_build", False, 0, "")

    if oi_change_pct >= 30:
        score = 20
    elif oi_change_pct >= 20:
        score = 17
    else:
        score = 15

    return LiquiditySignal(
        "oi_build", True, score,
        f"⚡ OI قفز +{oi_change_pct:.1f}% والسعر ثابت ({price_change_1h_pct:+.1f}%)",
        raw={"oi_change": oi_change_pct, "price_change": price_change_1h_pct},
    )


# ============================================================================
# 6. COMPRESSION-TO-EXPANSION — volatility coiling and starting to expand
# ============================================================================
def detect_compression_expansion(df_1m: pd.DataFrame,
                                   compression_window: int = 30,
                                   expansion_window: int = 5) -> LiquiditySignal:
    """Coiled spring: low ATR for 30 min, then ATR starts expanding in last 5 min."""
    if df_1m is None or len(df_1m) < compression_window + expansion_window:
        return LiquiditySignal("compression", False, 0, "")

    work = df_1m.tail(compression_window + expansion_window).copy()
    high = work["h"].astype(float)
    low = work["l"].astype(float)
    close = work["c"].astype(float)

    prev_close = close.shift(1).fillna(close.iloc[0])
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    compression_atr = float(tr.iloc[:compression_window].mean())
    expansion_atr = float(tr.iloc[-expansion_window:].mean())

    if compression_atr <= 0:
        return LiquiditySignal("compression", False, 0, "")

    expansion_ratio = expansion_atr / compression_atr

    if expansion_ratio < 1.5:
        return LiquiditySignal("compression", False, 0, "")

    recent_closes = close.iloc[-expansion_window:].values
    direction = "long" if recent_closes[-1] > recent_closes[0] else "short"

    if expansion_ratio >= 3.0:
        score = 18
    elif expansion_ratio >= 2.0:
        score = 15
    else:
        score = 10

    dir_emoji = "🟢" if direction == "long" else "🔴"
    return LiquiditySignal(
        "compression", True, score,
        f"{dir_emoji} انضغاط ينفجر (تقلب {expansion_ratio:.1f}x)",
        raw={"ratio": expansion_ratio, "direction": direction,
             "compression_atr": compression_atr, "expansion_atr": expansion_atr},
    )


# ============================================================================
# AGGREGATOR — runs all 6 and returns combined verdict
# ============================================================================
def analyze_liquidity_buildup(df_1m: pd.DataFrame, order_book: Optional[dict],
                               whale_data: Optional[dict],
                               current_oi: Optional[float],
                               prev_oi: Optional[float],
                               price_change_1h_pct: float) -> dict:
    """Run all 6 detectors and aggregate."""
    signals = [
        detect_cvd_direction_lock(df_1m),
        detect_volume_awakening(df_1m),
        detect_orderbook_imbalance(order_book),
        detect_whale_inflow(whale_data),
        detect_oi_slow_build(current_oi, prev_oi, price_change_1h_pct),
        detect_compression_expansion(df_1m),
    ]

    triggered = [s for s in signals if s.triggered]
    total_score = min(100, sum(s.score for s in triggered))

    long_votes = sum(1 for s in triggered if s.raw.get("direction") == "long")
    short_votes = sum(1 for s in triggered if s.raw.get("direction") == "short")

    if long_votes > short_votes and long_votes >= 2:
        direction = "long_likely"
    elif short_votes > long_votes and short_votes >= 2:
        direction = "short_likely"
    else:
        direction = "unknown"

    return {
        "signals": signals,
        "score": total_score,
        "direction": direction,
        "signals_fired": len(triggered),
        "long_votes": long_votes,
        "short_votes": short_votes,
    }
