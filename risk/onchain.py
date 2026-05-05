"""
On-chain Metrics — institutional-grade blockchain signals.

Uses what we can extract from existing data sources:
1. Whale Alert (already integrated) → exchange netflow approximation
2. Funding rate aggregate → derivatives sentiment
3. Open Interest changes → leverage cycle position
4. Long/Short ratio → crowd positioning

For full on-chain (Glassnode, CryptoQuant, Santiment) we'd need paid APIs.
This module gives 70% of the value using only what we already have.
"""
import asyncio
from dataclasses import dataclass, field
from typing import Optional
from data_sources.binance import binance
from data_sources.whale_alert import whale_alert
from core.logger import get_logger

log = get_logger(__name__)


@dataclass
class OnchainSnapshot:
    symbol: str
    # Exchange flow (from whale alert)
    exchange_inflow_usd: float = 0.0       # whales depositing → bearish
    exchange_outflow_usd: float = 0.0      # whales withdrawing → bullish (accumulation)
    netflow_usd: float = 0.0               # negative = accumulation
    netflow_signal: str = "neutral"        # "bullish_accumulation" | "bearish_distribution" | "neutral"

    # Derivatives sentiment
    funding_rate: float = 0.0
    funding_signal: str = "neutral"        # "extreme_long" | "extreme_short" | "balanced"

    # Open Interest dynamics
    oi_change_24h_pct: float = 0.0
    oi_signal: str = "neutral"             # "rising_with_price" | "falling_with_price" | etc

    # Long/Short ratio (top traders)
    ls_ratio: float = 1.0
    ls_signal: str = "balanced"            # "crowded_long" | "crowded_short" | "balanced"

    # Composite score
    composite_score: int = 50              # 0..100, higher = more bullish
    summary: str = ""
    warnings: list = field(default_factory=list)


async def fetch_funding_history_safe(symbol: str, limit: int = 30) -> list:
    """Get last N funding rates."""
    try:
        return await binance.fetch_funding_history(symbol, limit=limit)
    except Exception:
        return []


async def fetch_oi_change(symbol: str) -> tuple[Optional[float], Optional[float]]:
    """Returns (current_oi, oi_change_pct_24h) using premium index history."""
    try:
        # Binance: /fapi/v1/openInterest gives current
        # /futures/data/openInterestHist gives history (5m/15m/30m/1h/2h/4h/6h/12h/1d)
        from config.settings import settings
        if not binance._session:
            await binance.start()
        url = f"{settings.binance_rest}/futures/data/openInterestHist"
        params = {"symbol": symbol, "period": "1h", "limit": 30}
        async with binance._session.get(url, params=params) as r:
            if r.status != 200:
                return None, None
            data = await r.json()
        if not data or len(data) < 24:
            return None, None
        current_oi = float(data[-1].get("sumOpenInterestValue", 0))
        prev_oi = float(data[-24].get("sumOpenInterestValue", 0))
        if prev_oi <= 0:
            return current_oi, None
        change_pct = (current_oi - prev_oi) / prev_oi * 100
        return current_oi, change_pct
    except Exception as e:
        log.warning("oi_fetch_error", symbol=symbol, err=str(e))
        return None, None


async def fetch_long_short_ratio(symbol: str) -> Optional[float]:
    """Top traders' long/short ratio (recent)."""
    try:
        from config.settings import settings
        if not binance._session:
            await binance.start()
        url = f"{settings.binance_rest}/futures/data/topLongShortAccountRatio"
        params = {"symbol": symbol, "period": "1h", "limit": 1}
        async with binance._session.get(url, params=params) as r:
            if r.status != 200:
                return None
            data = await r.json()
        if data:
            return float(data[0].get("longShortRatio", 1.0))
    except Exception as e:
        log.warning("ls_ratio_error", symbol=symbol, err=str(e))
    return None


async def analyze_onchain(symbol: str, base_asset: str,
                            current_price: float, price_change_24h: float) -> OnchainSnapshot:
    """Full on-chain analysis. Pulls from multiple data sources in parallel."""
    snap = OnchainSnapshot(symbol=symbol)

    whale_task = whale_alert.whale_flow_for_symbol(base_asset) if whale_alert.enabled else None
    funding_task = fetch_funding_history_safe(symbol, limit=10)
    oi_task = fetch_oi_change(symbol)
    ls_task = fetch_long_short_ratio(symbol)

    tasks = [funding_task, oi_task, ls_task]
    if whale_task is not None:
        tasks.insert(0, whale_task)

    results = await asyncio.gather(*tasks, return_exceptions=True)
    idx = 0
    if whale_task is not None:
        whale_data = results[idx] if not isinstance(results[idx], Exception) else None
        idx += 1
    else:
        whale_data = None
    funding_hist = results[idx] if not isinstance(results[idx], Exception) else []
    idx += 1
    oi_result = results[idx] if not isinstance(results[idx], Exception) else (None, None)
    idx += 1
    ls_ratio = results[idx] if not isinstance(results[idx], Exception) else None

    # ─── Whale flow analysis ─────────────────────────────
    if whale_data:
        snap.exchange_inflow_usd = whale_data.get("inflow_to_exchange_usd", 0)
        snap.exchange_outflow_usd = whale_data.get("outflow_from_exchange_usd", 0)
        snap.netflow_usd = snap.exchange_inflow_usd - snap.exchange_outflow_usd
        if snap.netflow_usd < -500_000:
            snap.netflow_signal = "bullish_accumulation"
        elif snap.netflow_usd > 500_000:
            snap.netflow_signal = "bearish_distribution"

    # ─── Funding rate signal ─────────────────────────────
    if funding_hist:
        snap.funding_rate = funding_hist[-1].get("rate", 0)
        if snap.funding_rate >= 0.0007:    # 0.07% = ~21% APR
            snap.funding_signal = "extreme_long"
            snap.warnings.append(
                f"⚠️ Funding مرتفع جداً ({snap.funding_rate*100:.3f}%) — "
                f"longs مكدسة، احتمال squeeze هابط"
            )
        elif snap.funding_rate <= -0.0007:
            snap.funding_signal = "extreme_short"
        else:
            snap.funding_signal = "balanced"

    # ─── OI signal ───────────────────────────────────────
    current_oi, oi_change = oi_result
    if oi_change is not None:
        snap.oi_change_24h_pct = oi_change
        if abs(oi_change) > 5:
            if oi_change > 0 and price_change_24h > 0:
                snap.oi_signal = "rising_with_price"  # healthy continuation
            elif oi_change > 0 and price_change_24h < 0:
                snap.oi_signal = "rising_against_price"  # shorts piling on
            elif oi_change < 0 and price_change_24h > 0:
                snap.oi_signal = "short_squeeze"  # OI down + price up = covering
            elif oi_change < 0 and price_change_24h < 0:
                snap.oi_signal = "long_capitulation"  # liquidations cascade

    # ─── Long/Short ratio ────────────────────────────────
    if ls_ratio is not None:
        snap.ls_ratio = ls_ratio
        if ls_ratio >= 2.5:
            snap.ls_signal = "crowded_long"
            snap.warnings.append(
                f"⚠️ L/S ratio = {ls_ratio:.2f} — متداولون كثر long، خطر تصحيح"
            )
        elif ls_ratio <= 0.4:
            snap.ls_signal = "crowded_short"
        else:
            snap.ls_signal = "balanced"

    # ─── Composite score ─────────────────────────────────
    score = 50  # neutral baseline
    if snap.netflow_signal == "bullish_accumulation":
        score += 15
    elif snap.netflow_signal == "bearish_distribution":
        score -= 15

    # Funding contrarian: extreme long = bearish, extreme short = bullish
    if snap.funding_signal == "extreme_long":
        score -= 10
    elif snap.funding_signal == "extreme_short":
        score += 10

    if snap.oi_signal == "rising_with_price":
        score += 8
    elif snap.oi_signal == "short_squeeze":
        score += 12
    elif snap.oi_signal == "long_capitulation":
        score -= 12
    elif snap.oi_signal == "rising_against_price":
        score -= 5

    # L/S contrarian
    if snap.ls_signal == "crowded_long":
        score -= 10
    elif snap.ls_signal == "crowded_short":
        score += 10

    snap.composite_score = max(0, min(100, score))

    # ─── Summary ────────────────────────────────────────
    parts = []
    if snap.netflow_signal == "bullish_accumulation":
        parts.append(f"🐋 تجميع (${abs(snap.netflow_usd)/1000:.0f}K outflow)")
    elif snap.netflow_signal == "bearish_distribution":
        parts.append(f"🐋 توزيع (${snap.netflow_usd/1000:.0f}K inflow)")

    if snap.funding_signal in ("extreme_long", "extreme_short"):
        sign = "+" if snap.funding_rate > 0 else ""
        parts.append(f"💸 Funding {snap.funding_signal} ({sign}{snap.funding_rate*100:.3f}%)")

    if snap.oi_signal not in ("neutral",):
        ar_map = {
            "rising_with_price": "OI صاعد مع السعر",
            "rising_against_price": "OI صاعد ضد السعر",
            "short_squeeze": "🔥 short squeeze",
            "long_capitulation": "🩸 long capitulation",
        }
        parts.append(ar_map.get(snap.oi_signal, snap.oi_signal))

    if snap.ls_signal != "balanced":
        ar = "longs مكدسة" if snap.ls_signal == "crowded_long" else "shorts مكدسة"
        parts.append(f"⚖️ {ar} ({snap.ls_ratio:.2f})")

    snap.summary = " | ".join(parts) if parts else "بيانات on-chain متعادلة"
    return snap
