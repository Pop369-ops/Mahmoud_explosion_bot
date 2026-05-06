"""
Watch List Manager — identifies COILED coins ready to potentially explode.

A coin is "coiled" when:
  - 1h ATR < 2.0% of price (low volatility)
  - 1h volume < 50% of recent average (quiet)
  - No major move (>5%) in last 4 hours
  - Volume hasn't been suppressed for >24 hours (avoid dead coins)

Watch list refreshes every 5 minutes. Awakening detector then runs every
30 seconds on JUST these symbols.
"""
import asyncio
import pandas as pd
import numpy as np
from typing import Optional
from core.models import WatchListEntry
from core.state import state
from core.logger import get_logger
from data_sources.binance import binance
from config.settings import settings

log = get_logger(__name__)


def _calculate_atr_pct(df_1h: pd.DataFrame, period: int = 14) -> float:
    """ATR as percentage of current price."""
    if df_1h is None or len(df_1h) < period + 1:
        return 999.0
    work = df_1h.tail(period + 1).copy()
    high = work["h"].astype(float)
    low = work["l"].astype(float)
    close = work["c"].astype(float)
    prev_close = close.shift(1).fillna(close.iloc[0])
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = float(tr.tail(period).mean())
    current_price = float(close.iloc[-1])
    if current_price <= 0:
        return 999.0
    return (atr / current_price) * 100


def _calculate_volume_ratio(df_1h: pd.DataFrame, recent: int = 4,
                              baseline: int = 20) -> float:
    """recent N hours volume vs baseline N hours volume avg."""
    if df_1h is None or len(df_1h) < recent + baseline:
        return 999.0
    qv = df_1h["qv"].astype(float)
    recent_avg = float(qv.tail(recent).mean())
    baseline_avg = float(qv.tail(recent + baseline).head(baseline).mean())
    if baseline_avg <= 0:
        return 999.0
    return recent_avg / baseline_avg


def _max_recent_move(df_1h: pd.DataFrame, hours: int = 4) -> float:
    """Largest pct move (high to low) in recent N hours."""
    if df_1h is None or len(df_1h) < hours:
        return 0.0
    work = df_1h.tail(hours)
    high = float(work["h"].astype(float).max())
    low = float(work["l"].astype(float).min())
    if low <= 0:
        return 0.0
    return ((high - low) / low) * 100


def _quiet_hours(df_1h: pd.DataFrame, threshold_pct: float = 2.5) -> float:
    """Count consecutive recent hours where (h-l)/o < threshold."""
    if df_1h is None or len(df_1h) < 1:
        return 0.0
    count = 0.0
    for i in range(1, min(len(df_1h), 24) + 1):
        bar = df_1h.iloc[-i]
        h = float(bar["h"])
        l = float(bar["l"])
        o = float(bar["o"])
        if o <= 0:
            break
        range_pct = ((h - l) / o) * 100
        if range_pct < threshold_pct:
            count += 1
        else:
            break
    return count


async def evaluate_coin_for_watch(symbol: str) -> Optional[WatchListEntry]:
    """Check if a single symbol qualifies for the watch list."""
    try:
        klines_1h = await binance.fetch_klines(symbol, "1h", 50)
        if klines_1h is None or len(klines_1h) < 25:
            return None

        atr_pct = _calculate_atr_pct(klines_1h)
        vol_ratio = _calculate_volume_ratio(klines_1h)
        max_move = _max_recent_move(klines_1h, hours=4)
        quiet = _quiet_hours(klines_1h)

        # Filter conditions for "coiled" state
        if atr_pct >= 2.0:
            return None
        if vol_ratio >= 0.5:
            return None
        if max_move >= 5.0:
            return None
        if quiet < 2:
            return None
        if quiet > 24:
            return None  # dead coin

        current_price = float(klines_1h["c"].iloc[-1])

        reason = (f"ATR {atr_pct:.2f}% / Vol {vol_ratio*100:.0f}% المعدل / "
                  f"هادئ {quiet:.0f}س")

        return WatchListEntry(
            symbol=symbol,
            last_price=current_price,
            atr_1h_pct=atr_pct,
            vol_ratio_1h=vol_ratio,
            quiet_hours=quiet,
            reason=reason,
        )
    except Exception as e:
        log.debug("watch_eval_err", symbol=symbol, err=str(e))
        return None


async def refresh_watch_list(top_n: int = 100) -> dict[str, WatchListEntry]:
    """Scan top-N pairs and identify coiled candidates. Called every 5 minutes."""
    try:
        pairs = await binance.fetch_top_pairs(
            top_n=top_n,
            quote=settings.quote_asset,
            min_volume=settings.min_volume_usdt,
        )
        if not pairs:
            log.warning("watch_no_pairs")
            return {}

        symbols = [p["symbol"] for p in pairs]
        log.info("watch_evaluating", count=len(symbols))

        # Evaluate in batches of 10 to avoid hitting rate limits
        entries: dict[str, WatchListEntry] = {}
        batch_size = 10
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i:i + batch_size]
            results = await asyncio.gather(
                *[evaluate_coin_for_watch(s) for s in batch],
                return_exceptions=True,
            )
            for s, r in zip(batch, results):
                if isinstance(r, WatchListEntry):
                    entries[s] = r

        log.info("watch_list_refreshed", total=len(entries),
                  evaluated=len(symbols))
        await state.update_watch_list(entries)
        return entries
    except Exception as e:
        log.error("watch_refresh_err", err=str(e))
        return {}


def format_watch_list_msg(entries: dict[str, WatchListEntry], limit: int = 15) -> str:
    """Format watch list for /watch command."""
    if not entries:
        return ("📋 *Watch List فارغة*\n\n"
                "لا توجد عملات في حالة كامنة حالياً.\n"
                "يتم التحديث كل 5 دقائق.")

    sorted_entries = sorted(
        entries.values(),
        key=lambda e: (e.quiet_hours, -e.atr_1h_pct),
        reverse=True,
    )

    msg = f"📋 *Watch List* — {len(entries)} عملة كامنة\n"
    msg += f"_الأكثر هدوءاً (top {min(limit, len(entries))}):_\n\n"

    for i, e in enumerate(sorted_entries[:limit], 1):
        sym_short = e.symbol.replace("USDT", "")
        msg += (f"`{i:2}.` *{sym_short}* — "
                f"هادئ `{e.quiet_hours:.0f}س` / "
                f"ATR `{e.atr_1h_pct:.2f}%`\n")

    msg += f"\n_يفحصها كاشف الإيقاظ كل 30 ثانية._"
    return msg
