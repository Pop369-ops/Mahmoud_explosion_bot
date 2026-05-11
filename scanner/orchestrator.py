"""
Scanner orchestrator — the main scan loop.

Fetches top-N coins, runs them all in parallel, dispatches alerts
for signals that beat the user's confidence threshold.
"""
import asyncio
from typing import Optional
from telegram import Bot
from telegram.ext import ContextTypes

from config.settings import settings
from core.models import MarketSnapshot, Signal, Phase, Mode
from core.state import state
from core.logger import get_logger
from data_sources.binance import binance
from scorer.confidence_engine import score_symbol, score_symbol_auto
from trading.manager import analyze_exit
from ui.alerts import build_entry_alert, build_exit_alert
from ui.keyboards import signal_keyboard, exit_keyboard

log = get_logger(__name__)


async def scan_single_symbol(chat_id: int, symbol: str, bot: Bot,
                              send_alert: bool = True) -> Optional[Signal]:
    """Scan one symbol, optionally send alert."""
    try:
        # Fetch all timeframes + OB + funding in parallel
        results = await asyncio.gather(
            binance.fetch_klines(symbol, "5m", 80),
            binance.fetch_klines(symbol, "15m", 50),
            binance.fetch_klines(symbol, "1h", 60),
            binance.fetch_klines(symbol, "4h", 60),
            binance.fetch_klines(symbol, "1d", 50),
            binance.fetch_order_book(symbol, 50),
            binance.fetch_funding_rate(symbol),
            return_exceptions=True,
        )
        klines = results[0] if not isinstance(results[0], Exception) else None
        klines_15m = results[1] if not isinstance(results[1], Exception) else None
        klines_1h = results[2] if not isinstance(results[2], Exception) else None
        klines_4h = results[3] if not isinstance(results[3], Exception) else None
        klines_1d = results[4] if not isinstance(results[4], Exception) else None
        ob = results[5] if not isinstance(results[5], Exception) else None
        fr_data = results[6] if isinstance(results[6], dict) else None

        if klines is None or len(klines) < 30:
            return None

        price = float(klines["c"].iloc[-1])
        chg_24h = (price - float(klines["c"].iloc[-min(48, len(klines))])) / float(klines["c"].iloc[-min(48, len(klines))]) * 100
        vol_24h = float(klines["qv"].iloc[-min(288, len(klines)):].sum())

        snap = MarketSnapshot(
            symbol=symbol, price=price,
            volume_24h=vol_24h, change_24h=chg_24h,
            klines=klines, klines_15m=klines_15m,
            klines_1h=klines_1h, klines_4h=klines_4h,
            klines_1d=klines_1d, order_book=ob,
            funding_rate=fr_data.get("funding_rate") if fr_data else None,
        )

        cfg = state.get_user_cfg(chat_id)
        sig = await score_symbol_auto(snap, mode=cfg["mode"])

        if send_alert and sig.phase != Phase.NONE and sig.phase != Phase.REJECTED:
            await _dispatch_alert(chat_id, sig, snap, bot)

        return sig
    except Exception as e:
        log.warning("scan_symbol_error", symbol=symbol, err=str(e))
        return None


async def run_scan_for_user(chat_id: int, bot: Bot, send_alerts: bool = True) -> list[Signal]:
    """Full scan — top N coins, send alerts for strong signals."""
    cfg = state.get_user_cfg(chat_id)
    pairs = await binance.fetch_top_pairs(
        top_n=settings.scan_top_n,
        min_vol_usd=settings.min_volume_usd,
    )
    if not pairs:
        log.warning("scan_no_pairs")
        return []

    log.info("scan_start", chat=chat_id, count=len(pairs))

    # Limit concurrent symbol scoring to avoid overwhelming Binance
    semaphore = asyncio.Semaphore(15)

    async def _process(p: dict) -> Optional[Signal]:
        async with semaphore:
            try:
                # Fetch all timeframes + OB in parallel for speed
                results = await asyncio.gather(
                    binance.fetch_klines(p["sym"], "5m", 80),
                    binance.fetch_klines(p["sym"], "15m", 50),
                    binance.fetch_klines(p["sym"], "1h", 60),
                    binance.fetch_klines(p["sym"], "4h", 60),
                    binance.fetch_klines(p["sym"], "1d", 50),
                    binance.fetch_order_book(p["sym"], 50),
                    return_exceptions=True,
                )
                klines = results[0] if not isinstance(results[0], Exception) else None
                klines_15m = results[1] if not isinstance(results[1], Exception) else None
                klines_1h = results[2] if not isinstance(results[2], Exception) else None
                klines_4h = results[3] if not isinstance(results[3], Exception) else None
                klines_1d = results[4] if not isinstance(results[4], Exception) else None
                ob = results[5] if not isinstance(results[5], Exception) else None

                if klines is None or len(klines) < 30:
                    return None

                snap = MarketSnapshot(
                    symbol=p["sym"], price=p["price"],
                    volume_24h=p["vol_24h"], change_24h=p["chg_24h"],
                    klines=klines, klines_15m=klines_15m,
                    klines_1h=klines_1h, klines_4h=klines_4h,
                    klines_1d=klines_1d, order_book=ob,
                )
                sig = await score_symbol_auto(snap, mode=cfg["mode"])

                if (sig.phase != Phase.NONE
                    and sig.phase != Phase.REJECTED
                    and sig.confidence >= cfg["min_confidence"]
                    and sig.sources_agreed >= cfg["min_sources"]):
                    if state.in_cooldown(chat_id, p["sym"], settings.alert_cooldown):
                        return None
                    if state.has_trade(chat_id, p["sym"]):
                        return None
                    if send_alerts:
                        await _dispatch_alert(chat_id, sig, snap, bot)
                    return sig
                return None
            except Exception as e:
                log.warning("process_error", sym=p["sym"], err=str(e))
                return None

    results = await asyncio.gather(*[_process(p) for p in pairs])
    signals = [s for s in results if s is not None]

    state.set_last_results(chat_id, signals)
    log.info("scan_done", chat=chat_id, signals=len(signals))
    return signals


async def _dispatch_alert(chat_id: int, sig: Signal, snap: MarketSnapshot, bot: Bot):
    """Send alert message + cache pending signal for button callbacks."""
    # ─── Risk gate: only block alerts for ACTUAL risk limits (not missing capital) ──
    from risk.daily_limits import risk_manager
    risk_check = risk_manager.can_take_trade(chat_id)

    # Special case: if capital not set, still send the alert (just without position sizing)
    capital_not_set = (
        risk_check.reason and "لم تحدد رأس المال" in risk_check.reason
    )

    if not risk_check.allowed and not capital_not_set:
        # Real risk limit hit (daily loss, weekly loss, cooldown, etc) — block alert
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=f"🚫 *إشارة جيدة لـ {sig.symbol} لكن:*\n\n{risk_check.reason}",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        log.info("alert_blocked_by_risk", chat=chat_id, symbol=sig.symbol,
                 reason=risk_check.reason)
        return

    cfg = state.get_user_cfg(chat_id)
    msg = build_entry_alert(sig, chat_id=chat_id)
    kb = signal_keyboard(sig.symbol, ai_enabled=cfg.get("ai_enabled", False))

    pending = {
        "phase": sig.phase.value, "confidence": sig.confidence,
        "sources_agreed": sig.sources_agreed, "sources_total": sig.sources_total,
        "price": sig.price, "change_24h": sig.change_24h, "volume_24h": sig.volume_24h,
        "entry": sig.entry, "sl": sig.sl, "tp1": sig.tp1, "tp2": sig.tp2, "tp3": sig.tp3,
        "sl_pct": sig.sl_pct, "atr": sig.atr, "mode": sig.mode.value,
        "direction": getattr(sig, 'direction', 'long'),
        "verdicts_detail": [
            {"name": v.name, "score": v.score, "confidence": v.confidence,
             "reasons": v.reasons, "warnings": v.warnings}
            for v in sig.verdicts
        ],
    }
    await state.add_pending(chat_id, sig.symbol, pending)
    state.mark_alerted(chat_id, sig.symbol)

    try:
        await bot.send_message(
            chat_id=chat_id, text=msg,
            parse_mode="Markdown", reply_markup=kb,
        )
        # If capital wasn't set, send a friendly reminder (max once per 6 hours)
        if capital_not_set and state.should_remind_capital(chat_id):
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    "💡 *لتظهر معك حسابات حجم المركز في الإشارات:*\n"
                    "أرسل: `/capital 1000`\n"
                    "(غيّر 1000 إلى رأس مالك بالدولار)"
                ),
                parse_mode="Markdown",
            )
            state.mark_capital_reminded(chat_id)

        # ─── ADVANCED TRACKING: log this signal ──
        try:
            from risk.advanced_tracker import advanced_tracker, SignalRecord
            import json
            if advanced_tracker is not None:
                # Extract context
                rsi_1h = rsi_4h = rsi_1d = 0.0
                try:
                    from scorer.trend_maturity import analyze_trend_maturity
                    maturity = analyze_trend_maturity(snap)
                    rsi_1h, rsi_4h, rsi_1d = maturity.rsi_1h, maturity.rsi_4h, maturity.rsi_1d
                except Exception:
                    pass

                # Killzone
                kz_name = "none"
                try:
                    if snap.killzone is not None:
                        kz_name = getattr(snap.killzone, "active_zone", "none") or "none"
                except Exception:
                    pass

                # BTC trend score
                btc_score = 50
                for v in sig.verdicts:
                    if v.name == "btc_trend":
                        btc_score = v.score
                        break

                # Verdicts JSON
                verdicts_dict = {v.name: v.score for v in sig.verdicts}

                # SL method
                sl_method = "unknown"
                sl_adjustments = ""
                for w in sig.warnings:
                    if "Equal Lows" in w or "Equal Highs" in w:
                        sl_adjustments += "eq_levels;"
                    if "volatility" in w.lower():
                        sl_adjustments += "vol_floor;"
                    if "HTF" in w or "swing high" in w or "swing low" in w:
                        sl_adjustments += "htf;"

                # Determine market regime
                regime = "ranging"
                if rsi_1d > 60:
                    regime = "uptrend"
                elif rsi_1d < 40:
                    regime = "downtrend"

                rec = SignalRecord(
                    chat_id=chat_id, symbol=sig.symbol,
                    direction=getattr(sig, 'direction', 'long'),
                    phase=sig.phase.value, confidence=sig.confidence,
                    sources_agreed=sig.sources_agreed,
                    sources_total=sig.sources_total,
                    entry=sig.entry, sl=sig.sl,
                    tp1=sig.tp1, tp2=sig.tp2, tp3=sig.tp3,
                    sl_pct=sig.sl_pct,
                    sl_method=sl_method,
                    sl_adjustments=sl_adjustments,
                    verdicts_json=json.dumps(verdicts_dict),
                    rejected=False, rejection_reason="",
                    gates_passed="1-23",
                    gates_failed="",
                    change_24h=sig.change_24h,
                    rsi_1h=rsi_1h, rsi_4h=rsi_4h, rsi_1d=rsi_1d,
                    in_killzone=kz_name,
                    btc_trend_score=btc_score,
                    market_regime=regime,
                )
                await advanced_tracker.log_signal(rec)
        except Exception as e:
            log.debug("track_signal_err", err=str(e))

    except Exception as e:
        log.warning("alert_send_error", err=str(e), symbol=sig.symbol)


# ─────────── Auto-scan job management ───────────

_AUTO_SCAN_PREFIX = "autoscan_"


def enable_auto_scan(c: ContextTypes.DEFAULT_TYPE, chat_id: int):
    name = f"{_AUTO_SCAN_PREFIX}{chat_id}"
    # Remove existing if any
    for j in c.job_queue.get_jobs_by_name(name):
        j.schedule_removal()
    c.job_queue.run_repeating(
        _auto_scan_callback,
        interval=settings.scan_interval,
        first=10,
        name=name,
        data={"chat_id": chat_id},
    )
    log.info("autoscan_enabled", chat=chat_id, interval=settings.scan_interval)


def disable_auto_scan(c: ContextTypes.DEFAULT_TYPE, chat_id: int):
    name = f"{_AUTO_SCAN_PREFIX}{chat_id}"
    for j in c.job_queue.get_jobs_by_name(name):
        j.schedule_removal()
    log.info("autoscan_disabled", chat=chat_id)


async def _auto_scan_callback(c: ContextTypes.DEFAULT_TYPE):
    chat_id = c.job.data["chat_id"]
    try:
        await run_scan_for_user(chat_id, c.bot, send_alerts=True)
    except Exception as e:
        log.warning("autoscan_error", err=str(e), chat=chat_id)


# ─────────── Trade monitor ───────────

async def monitor_trades_callback(c: ContextTypes.DEFAULT_TYPE):
    """Runs every monitor_interval — checks all open trades for exits."""
    bot = c.bot
    for chat_id in list(state._trades.keys()):
        trades = state.get_trades(chat_id)
        for sym, trade in list(trades.items()):
            try:
                # ─── Trailing manager check (TP1/TP2/TP3 + trailing SL) ──
                try:
                    from trading.trail_manager import trail_mgr
                    df = await binance.fetch_klines(sym, "5m", 50)
                    if df is not None and len(df) > 0:
                        current = float(df["c"].iloc[-1])
                        # Auto-start trailing if not yet tracked
                        if trail_mgr.get(chat_id, sym) is None:
                            direction = getattr(trade, 'direction', 'long')
                            trail_mgr.start_tracking(chat_id, sym, trade, direction)

                        # ─── Exhaustion check (defensive SL move) ──
                        try:
                            from scorer.exhaustion import (
                                analyze_exhaustion, render_exhaustion_alert,
                            )
                            funding_data = None
                            try:
                                fr = await binance.fetch_funding_rate(sym)
                                if fr:
                                    funding_data = fr.get("funding_rate")
                            except Exception:
                                pass

                            exhaustion = analyze_exhaustion(
                                df_5m=df, funding_rate=funding_data
                            )
                            ts = trail_mgr.get(chat_id, sym)

                            # If exhaustion against our position → defensive action
                            our_dir = getattr(trade, 'direction', 'long')
                            if (our_dir == "long"
                                and exhaustion.direction == "buying_exhaustion"
                                and exhaustion.signals_count >= 2):
                                # User chose: move SL to BE defensively
                                if ts and ts.current_sl < ts.initial_entry:
                                    old_sl = ts.current_sl
                                    ts.current_sl = ts.initial_entry
                                    msg = render_exhaustion_alert(exhaustion, sym, current)
                                    msg += (f"\n\n🛡 *إجراء دفاعي تلقائي:*\n"
                                            f"تم تحريك SL إلى Break-Even\n"
                                            f"`${old_sl:.6g}` → `${ts.current_sl:.6g}`")
                                    try:
                                        await bot.send_message(
                                            chat_id=chat_id, text=msg,
                                            parse_mode="Markdown",
                                        )
                                    except Exception as e:
                                        log.warning("exhaustion_alert_err", err=str(e))
                            elif (our_dir == "short"
                                  and exhaustion.direction == "selling_exhaustion"
                                  and exhaustion.signals_count >= 2):
                                if ts and ts.current_sl > ts.initial_entry:
                                    old_sl = ts.current_sl
                                    ts.current_sl = ts.initial_entry
                                    msg = render_exhaustion_alert(exhaustion, sym, current)
                                    msg += (f"\n\n🛡 *إجراء دفاعي تلقائي:*\n"
                                            f"تم تحريك SL إلى Break-Even\n"
                                            f"`${old_sl:.6g}` → `${ts.current_sl:.6g}`")
                                    try:
                                        await bot.send_message(
                                            chat_id=chat_id, text=msg,
                                            parse_mode="Markdown",
                                        )
                                    except Exception:
                                        pass
                        except Exception as e:
                            log.debug("exhaustion_check_err", err=str(e))

                        # ─── SL Proximity Warning (Phase C real-time defense) ──
                        # Warn user when price approaches SL within 0.5% — before it hits
                        try:
                            our_dir = getattr(trade, 'direction', 'long')
                            ts_check = trail_mgr.get(chat_id, sym)
                            if ts_check:
                                sl_now = ts_check.current_sl
                                proximity_threshold_pct = 0.7  # warn when within 0.7%
                                if our_dir == "long" and current > sl_now:
                                    dist_to_sl_pct = (current - sl_now) / current * 100
                                    if dist_to_sl_pct <= proximity_threshold_pct:
                                        # Use cooldown to avoid spam
                                        prox_key = f"sl_prox_{chat_id}_{sym}"
                                        if not state.in_cooldown(chat_id, prox_key, 300):
                                            state.mark_alerted(chat_id, prox_key)
                                            await bot.send_message(
                                                chat_id=chat_id,
                                                text=(f"🚨 *تحذير قرب SL — {sym}*\n\n"
                                                        f"السعر `${current:.6g}` قريب جداً من SL `${sl_now:.6g}` "
                                                        f"({dist_to_sl_pct:.2f}% فقط)\n\n"
                                                        f"⚠️ راقب الصفقة عن كثب — أو أغلق يدوياً لو في إشارة انعكاس."),
                                                parse_mode="Markdown",
                                            )
                                elif our_dir == "short" and current < sl_now:
                                    dist_to_sl_pct = (sl_now - current) / current * 100
                                    if dist_to_sl_pct <= proximity_threshold_pct:
                                        prox_key = f"sl_prox_{chat_id}_{sym}"
                                        if not state.in_cooldown(chat_id, prox_key, 300):
                                            state.mark_alerted(chat_id, prox_key)
                                            await bot.send_message(
                                                chat_id=chat_id,
                                                text=(f"🚨 *تحذير قرب SL — {sym}*\n\n"
                                                        f"السعر `${current:.6g}` قريب جداً من SL `${sl_now:.6g}` "
                                                        f"({dist_to_sl_pct:.2f}% فقط)\n\n"
                                                        f"⚠️ راقب الصفقة عن كثب."),
                                                parse_mode="Markdown",
                                            )
                        except Exception as e:
                            log.debug("sl_proximity_err", err=str(e))

                        # ─── Wick rejection on 1m candles (Phase C) ──
                        # If a 1m candle shows huge wick against our trade direction,
                        # tighten SL proactively to lock minimum loss
                        try:
                            our_dir = getattr(trade, 'direction', 'long')
                            df_1m = await binance.fetch_klines(sym, "1m", 10)
                            if df_1m is not None and len(df_1m) >= 5:
                                last = df_1m.iloc[-1]
                                o = float(last["o"])
                                h = float(last["h"])
                                l = float(last["l"])
                                c = float(last["c"])
                                rng = h - l
                                if rng > 0:
                                    upper_wick = h - max(o, c)
                                    lower_wick = min(o, c) - l
                                    upper_pct = upper_wick / rng
                                    lower_pct = lower_wick / rng
                                    upper_wick_price_pct = upper_wick / current * 100
                                    lower_wick_price_pct = lower_wick / current * 100

                                    ts_now = trail_mgr.get(chat_id, sym)
                                    if ts_now and not getattr(ts_now, "wick_defended", False):
                                        # LONG: big upper wick = sellers stepping in
                                        if (our_dir == "long" and upper_pct > 0.55
                                            and upper_wick_price_pct > 0.3
                                            and current >= trade.entry * 1.005):
                                            # Move SL toward entry to lock in small profit
                                            new_sl = max(ts_now.current_sl, trade.entry)
                                            if new_sl > ts_now.current_sl:
                                                ts_now.current_sl = new_sl
                                                ts_now.wick_defended = True
                                                try:
                                                    await bot.send_message(
                                                        chat_id=chat_id,
                                                        text=(f"⚡ *دفاع فوري — {sym}*\n\n"
                                                              f"فتيل علوي قوي على شمعة 1m "
                                                              f"(-{upper_wick_price_pct:.2f}% من القمة)\n"
                                                              f"🛡 تم تحريك SL إلى Break-Even لحماية الأرباح"),
                                                        parse_mode="Markdown",
                                                    )
                                                except Exception:
                                                    pass
                                        elif (our_dir == "short" and lower_pct > 0.55
                                              and lower_wick_price_pct > 0.3
                                              and current <= trade.entry * 0.995):
                                            new_sl = min(ts_now.current_sl, trade.entry)
                                            if new_sl < ts_now.current_sl:
                                                ts_now.current_sl = new_sl
                                                ts_now.wick_defended = True
                                                try:
                                                    await bot.send_message(
                                                        chat_id=chat_id,
                                                        text=(f"⚡ *دفاع فوري — {sym}*\n\n"
                                                              f"فتيل سفلي قوي على شمعة 1m "
                                                              f"(+{lower_wick_price_pct:.2f}% من القاع)\n"
                                                              f"🛡 تم تحريك SL إلى Break-Even لحماية الأرباح"),
                                                        parse_mode="Markdown",
                                                    )
                                                except Exception:
                                                    pass
                        except Exception as e:
                            log.debug("wick_defense_err", err=str(e))

                        result = trail_mgr.update(chat_id, sym, current)
                        for action in result.get("actions", []):
                            try:
                                await bot.send_message(
                                    chat_id=chat_id,
                                    text=action["msg"],
                                    parse_mode="Markdown",
                                )
                            except Exception as e:
                                log.warning("trail_msg_error", err=str(e))
                            if action.get("close"):
                                try:
                                    from trading.manager import record_trade_close
                                    reason = ("SL" if action["type"] == "sl_hit"
                                                else action["type"])
                                    await record_trade_close(chat_id, trade, current, reason)
                                except Exception:
                                    pass
                                trail_mgr.stop_tracking(chat_id, sym)
                                await state.remove_trade(chat_id, sym)
                        if result.get("should_stop"):
                            continue
                except Exception as e:
                    log.warning("trail_update_error", sym=sym, err=str(e))

                ex = await analyze_exit(trade)
                if ex["action"] != "hold" and ex.get("price", 0) > 0:
                    msg = build_exit_alert(trade, ex)
                    kb = exit_keyboard(sym)
                    await bot.send_message(
                        chat_id=chat_id, text=msg,
                        parse_mode="Markdown", reply_markup=kb,
                    )
                    if ex["action"] in ("exit_now", "exit_sl"):
                        # Record to performance log before removing
                        try:
                            from trading.manager import record_trade_close
                            close_reason = "SL" if ex["action"] == "exit_sl" else "TP/manual"
                            await record_trade_close(
                                chat_id, trade,
                                ex.get("price", 0),
                                close_reason,
                            )
                        except Exception as e:
                            log.warning("record_close_error", err=str(e))
                        try:
                            from trading.trail_manager import trail_mgr
                            trail_mgr.stop_tracking(chat_id, sym)
                        except Exception:
                            pass
                        await state.remove_trade(chat_id, sym)
            except Exception as e:
                log.warning("monitor_error", sym=sym, err=str(e))


def start_monitor(c, application):
    """Schedule the trade monitor to run periodically."""
    application.job_queue.run_repeating(
        monitor_trades_callback,
        interval=settings.monitor_interval,
        first=30,
        name="trade_monitor",
    )
    log.info("trade_monitor_started", interval=settings.monitor_interval)


# ═══════════════════════════════════════════════════════════════
# AWAKENING SYSTEM — Watch List + Early Warning Jobs
# ═══════════════════════════════════════════════════════════════
async def watch_list_refresh_callback(c: ContextTypes.DEFAULT_TYPE):
    """Refresh watch list of coiled coins. Runs every 5 minutes."""
    try:
        from scanner.watch_list_manager import refresh_watch_list
        entries = await refresh_watch_list(top_n=settings.scan_top_n)
        log.info("watch_list_job_done", count=len(entries))
    except Exception as e:
        log.warning("watch_list_job_err", err=str(e))


async def awakening_scan_callback(c: ContextTypes.DEFAULT_TYPE):
    """Scan watch list for awakening signals. Runs every 30 seconds."""
    try:
        from scanner.awakening_detector import scan_watch_list_for_awakening

        bot = c.application.bot

        # Send to all chat_ids that have awakening enabled
        all_chats = list(state._user_cfg.keys())
        for chat_id in all_chats:
            cfg = state.get_user_cfg(chat_id)
            if not cfg.get("awakening_enabled", True):
                continue
            threshold = cfg.get("awakening_threshold", 75)
            try:
                await scan_watch_list_for_awakening(
                    chat_id=chat_id, bot=bot, threshold=threshold
                )
            except Exception as e:
                log.debug("awaken_chat_err", chat=chat_id, err=str(e))
    except Exception as e:
        log.warning("awakening_scan_err", err=str(e))


async def reversal_scan_callback(c: ContextTypes.DEFAULT_TYPE):
    """Scan trending coins for reversals. Runs every 60 seconds."""
    try:
        from scanner.reversal_detector import scan_for_reversals

        bot = c.application.bot
        all_chats = list(state._user_cfg.keys())
        for chat_id in all_chats:
            cfg = state.get_user_cfg(chat_id)
            if not cfg.get("reversal_enabled", True):
                continue
            threshold = cfg.get("reversal_threshold", 60)
            try:
                await scan_for_reversals(
                    chat_id=chat_id, bot=bot, threshold=threshold
                )
            except Exception as e:
                log.debug("reversal_chat_err", chat=chat_id, err=str(e))
    except Exception as e:
        log.warning("reversal_scan_err", err=str(e))


async def tier_b_validator_callback(c: ContextTypes.DEFAULT_TYPE):
    """Validate Tier B alerts after 1h and 4h. Runs every 15 minutes."""
    try:
        from risk.advanced_tracker import advanced_tracker
        if advanced_tracker is None:
            return

        import aiosqlite
        from config.settings import settings
        from data_sources.binance import binance
        from datetime import datetime, timedelta
        from core.models import now_riyadh

        now = now_riyadh()

        # Find alerts that need validation
        async with aiosqlite.connect(settings.db_path) as db:
            cursor = await db.execute("""
                SELECT id, symbol, sent_at FROM tier_b_log
                WHERE validated_at IS NULL
                  AND sent_at >= ?
            """, ((now - timedelta(hours=8)).isoformat(),))
            rows = await cursor.fetchall()

        for row in rows:
            alert_id, symbol, sent_at_str = row
            try:
                sent_at = datetime.fromisoformat(sent_at_str)
                hours_passed = (now - sent_at).total_seconds() / 3600

                # Validate at ~1h and ~4h marks
                if 0.9 <= hours_passed <= 1.3 or 3.5 <= hours_passed <= 4.5:
                    df = await binance.fetch_klines(symbol, "1m", 5)
                    if df is not None and len(df) > 0:
                        current_price = float(df["c"].iloc[-1])
                        await advanced_tracker.validate_tier_b(
                            alert_id, current_price, hours_passed,
                        )
            except Exception as e:
                log.debug("validate_one_err", err=str(e))
    except Exception as e:
        log.warning("tier_b_validator_err", err=str(e))


def start_awakening_system(application):
    """Schedule both watch list refresh and awakening scan jobs."""
    # Watch list refresh: every 5 minutes, first run after 60 seconds
    application.job_queue.run_repeating(
        watch_list_refresh_callback,
        interval=300,    # 5 minutes
        first=60,
        name="watch_list_refresh",
    )
    # Awakening scan: every 30 seconds, first run after 90 seconds
    application.job_queue.run_repeating(
        awakening_scan_callback,
        interval=30,
        first=90,
        name="awakening_scan",
    )
    # Reversal scan: every 60 seconds, first run after 120 seconds
    application.job_queue.run_repeating(
        reversal_scan_callback,
        interval=60,
        first=120,
        name="reversal_scan",
    )
    # Tier B validator: every 15 minutes, first run after 60 minutes
    application.job_queue.run_repeating(
        tier_b_validator_callback,
        interval=900,    # 15 min
        first=3600,      # wait 1 hour before first validation
        name="tier_b_validator",
    )
    log.info("awakening_system_started",
              watch_interval=300, scan_interval=30, reversal_interval=60,
              validator_interval=900)

