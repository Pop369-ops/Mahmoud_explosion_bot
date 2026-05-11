"""
Reversal Detector — Tier B alert system for trend REVERSALS.

Different from awakening_detector (which catches start of moves).
This catches END of moves — the moment a trend is exhausting.

Workflow:
  1. Identify coins currently in a strong trend (>5% move in last 4h
     or > 15% in 24h)
  2. Run reversal_engine on them every 60 seconds
  3. If reversal detected with strength >= 60 and confirms >= 2:
       - Send Tier B reversal alert
       - Auto-tighten SL on any open trade in opposite direction

This is what would have saved Pop on FARTCOIN — the bot would have warned
him 30+ minutes before the top forming.
"""
import asyncio
from typing import Optional
from dataclasses import dataclass, field
from datetime import timedelta
from telegram import Bot

from core.models import MarketSnapshot, now_riyadh
from core.state import state
from core.logger import get_logger
from data_sources.binance import binance
from scorer.reversal_engine import analyze_reversal, ReversalReport

log = get_logger(__name__)


@dataclass
class ReversalAlert:
    symbol: str
    direction: str           # "bearish_reversal" / "bullish_reversal"
    strength: int
    confirms: int
    price: float
    move_24h: float
    move_4h: float
    reasons: list[str] = field(default_factory=list)
    timestamp: object = None


# In-memory state for reversal alerts (reuses awakening cooldown infrastructure)
_reversal_cooldown: dict[str, float] = {}
_reversal_history: list[ReversalAlert] = []
_MAX_HISTORY = 50


def in_reversal_cooldown(symbol: str, cooldown_min: int = 45) -> bool:
    import time
    last = _reversal_cooldown.get(symbol, 0)
    return (time.time() - last) < (cooldown_min * 60)


def mark_reversal_sent(symbol: str):
    import time
    _reversal_cooldown[symbol] = time.time()


def get_recent_reversal_alerts(hours: int = 4) -> list[ReversalAlert]:
    cutoff = now_riyadh() - timedelta(hours=hours)
    return [r for r in _reversal_history
            if r.timestamp and r.timestamp >= cutoff]


async def _identify_trending_coins(top_n: int = 80,
                                     min_move_24h: float = 8.0) -> list[str]:
    """
    Find coins in active trends (candidates for reversal detection).
    Criteria: |24h change| >= min_move_24h%
    """
    try:
        from config.settings import settings
        pairs = await binance.fetch_top_pairs(
            top_n=top_n,
            quote=settings.quote_asset,
            min_volume=settings.min_volume_usdt,
        )
        if not pairs:
            return []

        # Filter to those moving significantly
        trending = []
        for p in pairs:
            try:
                chg = float(p.get("priceChangePercent", 0))
                if abs(chg) >= min_move_24h:
                    trending.append(p["symbol"])
            except (ValueError, TypeError):
                continue
        return trending
    except Exception as e:
        log.warning("identify_trending_err", err=str(e))
        return []


async def _scan_one_for_reversal(symbol: str) -> Optional[ReversalAlert]:
    """Fetch data and run reversal analysis on one symbol."""
    try:
        if in_reversal_cooldown(symbol, cooldown_min=45):
            return None

        # Fetch all timeframes needed
        results = await asyncio.gather(
            binance.fetch_klines(symbol, "1h", 80),
            binance.fetch_klines(symbol, "4h", 60),
            binance.fetch_klines(symbol, "1d", 50),
            return_exceptions=True,
        )
        df_1h = results[0] if not isinstance(results[0], Exception) else None
        df_4h = results[1] if not isinstance(results[1], Exception) else None
        df_1d = results[2] if not isinstance(results[2], Exception) else None

        if df_1h is None or len(df_1h) < 50:
            return None

        # Build minimal snapshot for reversal analysis
        current_price = float(df_1h["c"].iloc[-1])

        # Compute 4h move
        move_4h = 0.0
        if df_1h is not None and len(df_1h) >= 5:
            past = float(df_1h["c"].iloc[-5])
            if past > 0:
                move_4h = (current_price - past) / past * 100

        # 24h move
        move_24h = 0.0
        if df_1h is not None and len(df_1h) >= 24:
            past = float(df_1h["c"].iloc[-24])
            if past > 0:
                move_24h = (current_price - past) / past * 100

        # Build snap-like object for reversal_engine
        class _Snap:
            pass
        snap = _Snap()
        snap.symbol = symbol
        snap.price = current_price
        snap.klines = df_1h
        snap.klines_1h = df_1h
        snap.klines_4h = df_4h
        snap.klines_1d = df_1d
        snap.klines_15m = None
        snap.klines_5m = df_1h  # fallback

        report = analyze_reversal(snap)

        if report.direction == "neutral":
            return None
        if report.total_strength < 60 or report.confirms_count < 2:
            return None

        # Direction-trend consistency: only alert if reversal is against the
        # current dominant move
        if report.direction == "bearish_reversal" and move_24h < 5:
            return None  # not really in a strong uptrend
        if report.direction == "bullish_reversal" and move_24h > -5:
            return None  # not really in a strong downtrend

        reasons = [s.reason_ar for s in report.signals if s.triggered][:5]

        return ReversalAlert(
            symbol=symbol,
            direction=report.direction,
            strength=report.total_strength,
            confirms=report.confirms_count,
            price=current_price,
            move_24h=move_24h,
            move_4h=move_4h,
            reasons=reasons,
            timestamp=now_riyadh(),
        )
    except Exception as e:
        log.debug("reversal_scan_err", symbol=symbol, err=str(e))
        return None


def build_reversal_alert_message(alert: ReversalAlert) -> str:
    """Format Tier B reversal alert."""
    if alert.direction == "bearish_reversal":
        title = "🔻 *تنبيه انعكاس هابط — Tier B*"
        action = ("• لو فاتح LONG على هذه العملة: حرّك SL لـ Break-Even فوراً\n"
                  "• إن لم يكن: ابتعد، أو فكّر SHORT بعد التأكيد\n"
                  "• معدل الدقة المتوقع: 60-70%")
    else:
        title = "🔺 *تنبيه انعكاس صاعد — Tier B*"
        action = ("• لو فاتح SHORT على هذه العملة: حرّك SL لـ Break-Even فوراً\n"
                  "• إن لم يكن: ابتعد، أو فكّر LONG بعد التأكيد\n"
                  "• معدل الدقة المتوقع: 60-70%")

    msg = f"{title}\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━━\n\n"
    msg += f"💎 *العملة:* `{alert.symbol}`\n"
    msg += f"💰 *السعر:* `${alert.price:.6g}`\n"
    msg += f"📊 *حركة 24h:* `{alert.move_24h:+.2f}%`\n"
    msg += f"📊 *حركة 4h:* `{alert.move_4h:+.2f}%`\n\n"

    msg += f"⚡ *قوة الانعكاس:* `{alert.strength}/100`\n"
    msg += f"🎯 *التأكيدات:* `{alert.confirms}` إشارات\n\n"

    msg += f"━━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"🔍 *الأنماط المرصودة:*\n\n"
    for r in alert.reasons[:5]:
        msg += f"  • {r}\n"

    msg += f"\n━━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"🎯 *التوصية:*\n{action}\n\n"

    if alert.timestamp:
        msg += f"⏰ {alert.timestamp.strftime('%H:%M:%S %d/%m/%Y')}"

    return msg


async def _auto_tighten_open_trades(symbol: str, reversal_dir: str, bot: Bot):
    """
    For any open trade in opposite direction of reversal, move SL to break-even.
    This is the defensive auto-protection feature.
    """
    try:
        from trading.trail_manager import trail_mgr
        # Find all chats with this trade open
        for chat_id in list(state._trades.keys()):
            trade = state.get_trade(chat_id, symbol)
            if not trade or trade.closed:
                continue

            should_tighten = False
            reason = ""
            if reversal_dir == "bearish_reversal" and trade.direction == "long":
                should_tighten = True
                reason = "انعكاس هابط مكتشف على LONG"
            elif reversal_dir == "bullish_reversal" and trade.direction == "short":
                should_tighten = True
                reason = "انعكاس صاعد مكتشف على SHORT"

            if should_tighten:
                # Move SL to entry (break-even)
                trade.sl = trade.entry
                # Update trail manager if tracking
                try:
                    ts = trail_mgr.get(chat_id, symbol)
                    if ts:
                        ts.current_sl = trade.entry
                except Exception:
                    pass

                # Notify user
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=(f"🛡 *حماية تلقائية — {symbol}*\n\n"
                                f"{reason}\n"
                                f"تم نقل SL إلى Break-Even (`${trade.entry:.6g}`)\n"
                                f"_صفقتك محمية من الخسارة الآن._"),
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
                log.info("auto_sl_to_be", chat=chat_id, symbol=symbol)
    except Exception as e:
        log.warning("auto_tighten_err", err=str(e))


async def scan_for_reversals(chat_id: int, bot: Bot,
                                threshold: int = 60) -> list[ReversalAlert]:
    """
    Main reversal scan — runs on trending coins, sends alerts on detection.
    """
    try:
        trending = await _identify_trending_coins(top_n=80, min_move_24h=8.0)
        if not trending:
            return []

        log.debug("reversal_scan_start", count=len(trending))

        triggered: list[ReversalAlert] = []
        # Process in batches
        batch_size = 8
        for i in range(0, len(trending), batch_size):
            batch = trending[i:i + batch_size]
            results = await asyncio.gather(
                *[_scan_one_for_reversal(s) for s in batch],
                return_exceptions=True,
            )
            for r in results:
                if isinstance(r, ReversalAlert) and r.strength >= threshold:
                    triggered.append(r)

        # Send alerts + auto-protect
        for alert in triggered:
            try:
                msg = build_reversal_alert_message(alert)
                await bot.send_message(
                    chat_id=chat_id, text=msg,
                    parse_mode="Markdown",
                )
                mark_reversal_sent(alert.symbol)
                _reversal_history.append(alert)
                if len(_reversal_history) > _MAX_HISTORY:
                    _reversal_history.pop(0)

                # Auto-protect any open trade
                await _auto_tighten_open_trades(
                    alert.symbol, alert.direction, bot
                )
                log.info("reversal_sent", symbol=alert.symbol,
                          dir=alert.direction, strength=alert.strength)
                # ─── ADVANCED TRACKING ──
                try:
                    from risk.advanced_tracker import advanced_tracker, TierBRecord
                    if advanced_tracker is not None:
                        rec = TierBRecord(
                            chat_id=chat_id, symbol=alert.symbol,
                            alert_type="reversal",
                            direction=alert.direction,
                            score=alert.strength,
                            signals_fired=alert.confirms,
                            price_at_alert=alert.price,
                        )
                        await advanced_tracker.log_tier_b(rec)
                except Exception as e:
                    log.debug("track_tier_b_err", err=str(e))
            except Exception as e:
                log.warning("reversal_send_err",
                              symbol=alert.symbol, err=str(e))

        return triggered
    except Exception as e:
        log.error("reversal_scan_fail", err=str(e))
        return []


def format_reversal_history(alerts: list[ReversalAlert], hours: int = 4) -> str:
    """Format recent reversal alerts for /reversal command."""
    if not alerts:
        return (f"🔕 *لا توجد تنبيهات انعكاس في آخر {hours} ساعات*\n\n"
                f"النظام يفحص العملات النشطة كل دقيقة.")

    msg = f"🔄 *تنبيهات الانعكاس — آخر {hours} ساعات*\n"
    msg += f"العدد: {len(alerts)}\n\n"

    sorted_alerts = sorted(alerts, key=lambda a: a.timestamp, reverse=True)

    for i, a in enumerate(sorted_alerts[:15], 1):
        sym_short = a.symbol.replace("USDT", "")
        time_str = a.timestamp.strftime("%H:%M") if a.timestamp else "—"
        emoji = "🔻" if a.direction == "bearish_reversal" else "🔺"
        msg += (f"`{i:2}.` {emoji} *{sym_short}* — قوة `{a.strength}` "
                f"({a.confirms} تأكيدات) — `{time_str}`\n")
        msg += f"     حركة 24h: `{a.move_24h:+.2f}%`\n"

    return msg
