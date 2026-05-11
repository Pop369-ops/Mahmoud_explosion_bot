"""
Awakening Detector — runs every 30 seconds on watch list symbols.

For each coiled symbol, fetches:
  - 1m klines (last 90 bars = 1.5 hours)
  - Order book (top 20 levels)
  - Open interest (current + 1h ago)
  - Whale flow (if enabled)

Then runs analyze_liquidity_buildup() to compute awakening score.

If score >= threshold (default 75) AND signals_fired >= 3:
  - Send Tier B early warning alert
  - Mark cooldown (30 min default)
  - Continue watching

Tier B alerts are NOT trade signals — they say "watch this coin, something's
brewing." User decides whether to enter manually.
"""
import asyncio
from typing import Optional
from telegram import Bot

from core.models import AwakeningAlert
from core.state import state
from core.logger import get_logger
from data_sources.binance import binance
from data_sources.whale_alert import whale_alert
from detectors.liquidity_buildup import analyze_liquidity_buildup

log = get_logger(__name__)


# In-memory cache for previous OI values (per symbol)
_oi_history: dict[str, tuple[float, float]] = {}  # sym -> (timestamp, oi_value)


async def _get_oi_change(symbol: str) -> tuple[Optional[float], Optional[float]]:
    """Returns (current_oi, prev_oi_1h_ago). Maintains history cache."""
    import time
    current = await binance.fetch_open_interest(symbol)
    if current is None:
        return None, None

    now = time.time()
    prev_entry = _oi_history.get(symbol)

    # Clean old entry, store new
    if prev_entry:
        prev_ts, prev_oi = prev_entry
        # If we have a value from ~1 hour ago (50-70 min window), use it
        age = now - prev_ts
        if 50 * 60 <= age <= 70 * 60:
            _oi_history[symbol] = (now, current)
            return current, prev_oi
        elif age > 70 * 60:
            # Too old, replace
            _oi_history[symbol] = (now, current)
            return current, None

    # No prior data, store
    if not prev_entry:
        _oi_history[symbol] = (now, current)
    return current, None


async def _scan_one_symbol(symbol: str) -> Optional[AwakeningAlert]:
    """Scan one watch list symbol for awakening signals."""
    try:
        # Skip if in cooldown
        if state.in_awakening_cooldown(symbol, cooldown_min=30):
            return None

        # Fetch all data sources in parallel
        results = await asyncio.gather(
            binance.fetch_klines(symbol, "1m", 90),
            binance.fetch_klines(symbol, "1h", 5),
            binance.fetch_order_book(symbol, 50),
            return_exceptions=True,
        )
        df_1m = results[0] if not isinstance(results[0], Exception) else None
        df_1h_short = results[1] if not isinstance(results[1], Exception) else None
        ob = results[2] if not isinstance(results[2], Exception) else None

        if df_1m is None or len(df_1m) < 70:
            return None

        # Calculate 1h price change
        price_change_1h = 0.0
        if df_1h_short is not None and len(df_1h_short) >= 2:
            curr = float(df_1h_short["c"].iloc[-1])
            prev = float(df_1h_short["c"].iloc[-2])
            if prev > 0:
                price_change_1h = (curr - prev) / prev * 100

        # Get OI history
        current_oi, prev_oi = await _get_oi_change(symbol)

        # Get whale flow (best effort)
        whale_data = None
        try:
            if whale_alert.enabled:
                base = symbol.replace("USDT", "").lower()
                whale_data = await whale_alert.analyze_flow(base)
        except Exception:
            pass

        # Run aggregator
        result = analyze_liquidity_buildup(
            df_1m=df_1m, order_book=ob, whale_data=whale_data,
            current_oi=current_oi, prev_oi=prev_oi,
            price_change_1h_pct=price_change_1h,
        )

        # Calculate 15m price change for context
        price_change_15m = 0.0
        if len(df_1m) >= 15:
            current = float(df_1m["c"].iloc[-1])
            past = float(df_1m["c"].iloc[-15])
            if past > 0:
                price_change_15m = (current - past) / past * 100

        # Build alert object (always — caller decides if to send)
        alert = AwakeningAlert(
            symbol=symbol,
            direction=result["direction"],
            awakening_score=result["score"],
            signals_fired=result["signals_fired"],
            price=float(df_1m["c"].iloc[-1]),
            change_15m=price_change_15m,
            signals=result["signals"],
        )
        return alert
    except Exception as e:
        log.debug("awaken_scan_err", symbol=symbol, err=str(e))
        return None


async def scan_watch_list_for_awakening(
    chat_id: int, bot: Bot, threshold: int = 75
) -> list[AwakeningAlert]:
    """
    Scan all watch list symbols. Returns alerts that exceeded threshold.
    Sends Telegram messages for triggered alerts.
    """
    symbols = state.get_watch_symbols()
    if not symbols:
        return []

    log.debug("awaken_scan_start", count=len(symbols))

    # Process in batches to avoid rate limits
    triggered_alerts: list[AwakeningAlert] = []
    batch_size = 10

    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        results = await asyncio.gather(
            *[_scan_one_symbol(s) for s in batch],
            return_exceptions=True,
        )
        for alert in results:
            if isinstance(alert, AwakeningAlert):
                # Check threshold AND minimum signals
                if alert.awakening_score >= threshold and alert.signals_fired >= 3:
                    triggered_alerts.append(alert)

    # Send alerts
    if triggered_alerts:
        from ui.awakening_alerts import build_awakening_alert
        for alert in triggered_alerts:
            try:
                msg = build_awakening_alert(alert)
                await bot.send_message(
                    chat_id=chat_id, text=msg,
                    parse_mode="Markdown",
                )
                await state.record_awakening(alert)
                log.info("awakening_sent", symbol=alert.symbol,
                          score=alert.awakening_score,
                          signals=alert.signals_fired)
                # ─── ADVANCED TRACKING ──
                try:
                    from risk.advanced_tracker import advanced_tracker, TierBRecord
                    if advanced_tracker is not None:
                        rec = TierBRecord(
                            chat_id=chat_id, symbol=alert.symbol,
                            alert_type="awakening",
                            direction=alert.direction,
                            score=alert.awakening_score,
                            signals_fired=alert.signals_fired,
                            price_at_alert=alert.price,
                        )
                        await advanced_tracker.log_tier_b(rec)
                except Exception as e:
                    log.debug("track_tier_b_err", err=str(e))
            except Exception as e:
                log.warning("awaken_send_err", symbol=alert.symbol, err=str(e))

    return triggered_alerts
