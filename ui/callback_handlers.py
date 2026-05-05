"""Inline button callback handlers."""
from telegram import Update
from telegram.ext import ContextTypes
from config.settings import settings
from core.state import state
from core.models import Mode
from core.logger import get_logger
from ui.alerts import build_status_message
from ui.keyboards import (main_menu_keyboard, settings_keyboard,
                           mode_keyboard, conf_keyboard)

log = get_logger(__name__)


async def handle_callback(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    chat_id = q.message.chat_id
    data = q.data
    await q.answer()

    try:
        if data.startswith("track:"):
            await _on_track(q, chat_id, data.split(":", 1)[1])

        elif data.startswith("ignore:"):
            sym = data.split(":", 1)[1]
            state.mark_alerted(chat_id, sym)
            await state.remove_pending(chat_id, sym)
            await q.answer(f"✅ تم تجاهل {sym} لساعة", show_alert=True)
            await q.edit_message_reply_markup(reply_markup=None)

        elif data.startswith("ai:"):
            await _on_ai(q, chat_id, data.split(":", 1)[1])

        elif data.startswith("detail:"):
            await _on_detail(q, chat_id, data.split(":", 1)[1])

        elif data.startswith("closed:"):
            sym = data.split(":", 1)[1]
            # Record manual close to performance log
            trades = state.get_trades(chat_id)
            if sym in trades:
                trade = trades[sym]
                try:
                    from data_sources.binance import binance
                    from trading.manager import record_trade_close
                    df = await binance.fetch_klines(sym, "5m", 5)
                    exit_price = float(df["c"].iloc[-1]) if df is not None and len(df) > 0 else trade.entry
                    await record_trade_close(chat_id, trade, exit_price, "manual_user")
                except Exception as e:
                    log.warning("manual_close_record_error", err=str(e))
            await state.remove_trade(chat_id, sym)
            await q.answer(f"✅ تم إغلاق {sym}", show_alert=True)
            await q.edit_message_reply_markup(reply_markup=None)

        elif data.startswith("hold:"):
            await q.answer("⏳ سنستمر بالمراقبة", show_alert=True)

        elif data == "cmd:scan":
            from scanner.orchestrator import run_scan_for_user
            await q.answer("🔍 جاري المسح...")
            try:
                signals = await run_scan_for_user(chat_id, c.bot, send_alerts=True)
                await c.bot.send_message(chat_id=chat_id,
                                          text=f"✅ المسح اكتمل — {len(signals)} إشارة")
            except Exception as e:
                await c.bot.send_message(chat_id=chat_id, text=f"❌ {e}")

        elif data == "cmd:status":
            trades = state.get_trades(chat_id)
            last = state.get_last_results(chat_id)
            msg = build_status_message(chat_id, trades, last)
            await q.message.reply_text(msg, parse_mode="Markdown")

        elif data == "cmd:menu":
            await q.edit_message_text("🏠 *القائمة الرئيسية*", parse_mode="Markdown",
                                       reply_markup=main_menu_keyboard())

        elif data == "cmd:settings":
            cfg = state.get_user_cfg(chat_id)
            await q.edit_message_text("⚙️ *الإعدادات*", parse_mode="Markdown",
                                       reply_markup=settings_keyboard(cfg))

        elif data == "cmd:trades":
            from ui.keyboards import trades_list_keyboard
            from data_sources.binance import binance
            trades = state.get_trades(chat_id)
            if not trades:
                await q.message.reply_text(
                    "📈 لا توجد صفقات مفتوحة حالياً.",
                )
            else:
                msg = f"📈 *صفقاتي المفتوحة ({len(trades)})*\n"
                msg += "━━━━━━━━━━━━━━━━━━━━\n\n"
                for i, (symbol, trade) in enumerate(list(trades.items()), 1):
                    msg += f"*{i}. {symbol}*\n"
                    msg += f"  💰 الدخول: `${trade.entry:.6g}`\n"
                    msg += f"  🔴 SL: `${trade.sl:.6g}`\n"
                    try:
                        df = await binance.fetch_klines(symbol, "5m", 2)
                        if df is not None and len(df) > 0:
                            current = float(df["c"].iloc[-1])
                            pnl_pct = (current - trade.entry) / trade.entry * 100
                            pnl_emoji = "🟢" if pnl_pct >= 0 else "🔴"
                            msg += f"  {pnl_emoji} الحالي: `${current:.6g}` ({pnl_pct:+.2f}%)\n"
                    except Exception:
                        pass
                    msg += "\n"
                msg += "_اضغط زر الإغلاق لمسح صفقة من البوت._"
                await q.message.reply_text(
                    msg,
                    parse_mode="Markdown",
                    reply_markup=trades_list_keyboard(trades),
                )

        elif data.startswith("force_close:"):
            sym = data.split(":", 1)[1]
            trades = state.get_trades(chat_id)
            if sym in trades:
                trade = trades[sym]
                try:
                    from data_sources.binance import binance
                    from trading.manager import record_trade_close
                    df = await binance.fetch_klines(sym, "5m", 2)
                    exit_price = (
                        float(df["c"].iloc[-1])
                        if df is not None and len(df) > 0
                        else trade.entry
                    )
                    await record_trade_close(chat_id, trade, exit_price, "manual_force_close")
                except Exception as e:
                    log.warning("force_close_record_error", err=str(e))
                await state.remove_trade(chat_id, sym)
                await q.answer(f"✅ تم حذف {sym} من البوت", show_alert=True)
                # Refresh the trades list
                from ui.keyboards import trades_list_keyboard
                remaining = state.get_trades(chat_id)
                if remaining:
                    msg = f"📈 *صفقات متبقية ({len(remaining)}):*\n\n"
                    for s in remaining.keys():
                        msg += f"• {s}\n"
                    await q.edit_message_text(
                        msg,
                        parse_mode="Markdown",
                        reply_markup=trades_list_keyboard(remaining),
                    )
                else:
                    await q.edit_message_text("✅ تم مسح كل الصفقات.")
            else:
                await q.answer("❌ الصفقة غير موجودة")

        elif data == "clear_all_trades":
            from ui.keyboards import confirm_clear_keyboard
            trades = state.get_trades(chat_id)
            if not trades:
                await q.answer("لا صفقات لحذفها")
            else:
                await q.edit_message_text(
                    f"⚠️ *تأكيد المسح*\n\n"
                    f"سيتم حذف *{len(trades)} صفقة* من تتبع البوت.\n\n"
                    f"⚠️ هذا لا يغلق صفقاتك الفعلية على Binance — "
                    f"تأكد من إغلاقها يدوياً هناك أولاً.",
                    parse_mode="Markdown",
                    reply_markup=confirm_clear_keyboard(),
                )

        elif data == "confirm_clear_yes":
            trades = state.get_trades(chat_id)
            count = len(trades)
            for sym in list(trades.keys()):
                await state.remove_trade(chat_id, sym)
            try:
                from risk.daily_limits import risk_manager
                rs = risk_manager.get(chat_id)
                rs.open_trades_count = 0
            except Exception:
                pass
            await q.edit_message_text(
                f"✅ تم مسح *{count} صفقة* من البوت.\n\n"
                f"الإشارات الجديدة ستصل لك الآن.",
                parse_mode="Markdown",
            )

        elif data == "confirm_clear_no":
            await q.edit_message_text("❌ تم الإلغاء.")

        elif data == "noop":
            await q.answer()

        elif data == "cmd:autoscan_toggle":
            from scanner.orchestrator import enable_auto_scan, disable_auto_scan
            cfg = state.get_user_cfg(chat_id)
            new_state = not cfg.get("auto_scan", False)

            # CRITICAL: verify job_queue exists before toggling
            if c.application.job_queue is None:
                await q.answer("⚠️ JobQueue غير مفعل", show_alert=True)
                await c.bot.send_message(
                    chat_id=chat_id,
                    text="❌ *المسح التلقائي غير متاح*\n\n"
                         "السبب: مكتبة `apscheduler` ناقصة في النشر.\n\n"
                         "*الحل:* تأكد إن `requirements.txt` يحتوي:\n"
                         "`python-telegram-bot[job-queue]==20.7`\n"
                         "`APScheduler==3.10.4`\n\n"
                         "ثم أعد النشر على Railway.\n\n"
                         "💡 حالياً: استخدم زر *🔍 مسح الآن* يدوياً.",
                    parse_mode="Markdown",
                )
                return

            try:
                if new_state:
                    enable_auto_scan(c, chat_id)
                    cfg["auto_scan"] = True
                    interval_min = settings.scan_interval // 60
                    await c.bot.send_message(
                        chat_id=chat_id,
                        text=f"✅ *المسح التلقائي: مفعّل*\n\n"
                             f"⏱ كل {interval_min} دقيقة\n"
                             f"🎯 الحد الأدنى للثقة: {cfg['min_confidence']}/100\n"
                             f"🛡 المصادر المطلوبة: {cfg['min_sources']}+\n\n"
                             f"_راح يصلك تنبيه فقط لما يلقى إشارة قوية._\n"
                             f"_لو ما وصلك شيء = مافي إشارات تطابق المعايير حالياً._",
                        parse_mode="Markdown",
                    )
                    await q.answer("✅ المسح التلقائي مفعّل", show_alert=False)
                else:
                    disable_auto_scan(c, chat_id)
                    cfg["auto_scan"] = False
                    await c.bot.send_message(
                        chat_id=chat_id,
                        text="🔴 *المسح التلقائي: معطّل*\n\n"
                             "_استخدم زر 🔍 مسح الآن للمسح اليدوي._",
                        parse_mode="Markdown",
                    )
                    await q.answer("🔴 المسح التلقائي معطّل", show_alert=False)
            except Exception as e:
                log.warning("autoscan_toggle_error", err=str(e))
                await c.bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ خطأ في تشغيل المسح التلقائي:\n`{type(e).__name__}: {e}`",
                    parse_mode="Markdown",
                )

        elif data == "set:mode":
            await q.edit_message_text("اختر الوضع:", reply_markup=mode_keyboard())

        elif data.startswith("mode:"):
            mode = data.split(":", 1)[1]
            state.update_user_cfg(chat_id, mode=Mode(mode))
            await q.answer(f"✅ الوضع: {mode}", show_alert=True)
            cfg = state.get_user_cfg(chat_id)
            await q.edit_message_text("⚙️ *الإعدادات*", parse_mode="Markdown",
                                       reply_markup=settings_keyboard(cfg))

        elif data == "set:conf":
            await q.edit_message_text("اختر الحد الأدنى:", reply_markup=conf_keyboard())

        elif data.startswith("conf:"):
            val = int(data.split(":", 1)[1])
            state.update_user_cfg(chat_id, min_confidence=val)
            await q.answer(f"✅ الحد الأدنى: {val}", show_alert=True)
            cfg = state.get_user_cfg(chat_id)
            await q.edit_message_text("⚙️ *الإعدادات*", parse_mode="Markdown",
                                       reply_markup=settings_keyboard(cfg))

    except Exception as e:
        log.warning("callback_error", err=str(e), data=data)


async def _on_track(q, chat_id: int, symbol: str):
    from trading.manager import open_trade_from_signal
    from core.models import Signal, Phase, Mode

    pending = state.get_pending(chat_id, symbol)
    if not pending:
        await q.answer("⚠️ الإشارة منتهية الصلاحية. أعد المسح.", show_alert=True)
        return

    if state.has_trade(chat_id, symbol):
        await q.answer("هذه العملة متابعة بالفعل", show_alert=True)
        return

    try:
        sig = Signal(
            symbol=symbol,
            phase=Phase(pending.get("phase", "none")),
            confidence=pending.get("confidence", 0),
            sources_agreed=pending.get("sources_agreed", 0),
            sources_total=pending.get("sources_total", 0),
            price=pending.get("price", 0),
            change_24h=pending.get("change_24h", 0),
            volume_24h=pending.get("volume_24h", 0),
            entry=pending.get("entry", 0),
            sl=pending.get("sl", 0),
            tp1=pending.get("tp1", 0),
            tp2=pending.get("tp2", 0),
            tp3=pending.get("tp3", 0),
            sl_pct=pending.get("sl_pct", 0),
            atr=pending.get("atr", 0),
            mode=Mode(pending.get("mode", "day")),
        )
        await open_trade_from_signal(chat_id, sig)
        # Start trailing tracker
        try:
            from trading.trail_manager import trail_mgr
            from core.models import Trade
            trade_obj = state.get_trade(chat_id, symbol)
            if trade_obj:
                trail_mgr.start_tracking(chat_id, symbol, trade_obj, direction="long")
        except Exception as e:
            log.warning("trail_start_error", err=str(e))
        await state.remove_pending(chat_id, symbol)
        await q.answer("✅ بدأت المراقبة", show_alert=False)
        await q.edit_message_text(
            q.message.text + "\n\n👁 *المراقبة بدأت — ستستلم تنبيه عند TP/SL*\n"
            "_(تحريك SL تلقائي إلى Break-Even عند TP1)_",
            parse_mode="Markdown",
        )
    except Exception as e:
        log.warning("track_error", err=str(e))
        await q.answer(f"خطأ: {e}", show_alert=True)


async def _on_ai(q, chat_id: int, symbol: str):
    from ai.analyst import deep_analyze
    from data_sources.binance import binance
    from scorer.confidence_engine import score_symbol
    from core.models import MarketSnapshot

    pending = state.get_pending(chat_id, symbol)
    if not pending:
        await q.answer("⚠️ الإشارة منتهية", show_alert=True)
        return

    msg = await q.message.reply_text("🤖 جاري التحليل العميق...")
    try:
        df = await binance.fetch_klines(symbol, "5m", 80)
        ob = await binance.fetch_order_book(symbol)
        snap = MarketSnapshot(
            symbol=symbol,
            price=pending.get("price", 0),
            volume_24h=pending.get("volume_24h", 0),
            change_24h=pending.get("change_24h", 0),
            klines=df, order_book=ob,
        )
        cfg = state.get_user_cfg(chat_id)
        sig = await score_symbol(snap, mode=cfg["mode"])
        analysis = await deep_analyze(sig, snap)
        await msg.edit_text(analysis, parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"❌ {e}")


async def _on_detail(q, chat_id: int, symbol: str):
    pending = state.get_pending(chat_id, symbol)
    if not pending:
        await q.answer("⚠️ الإشارة منتهية", show_alert=True)
        return

    verdicts = pending.get("verdicts_detail", [])
    if not verdicts:
        await q.answer("لا تفاصيل متوفرة", show_alert=True)
        return

    msg = f"📈 *تفاصيل {symbol}:*\n\n"
    for v in verdicts:
        emoji = "🟢" if v["score"] >= 65 else "🔴" if v["score"] < 40 else "⚪"
        msg += f"{emoji} *{v['name']}*: {v['score']}/100 (ثقة {v['confidence']:.0%})\n"
        for r in v.get("reasons", [])[:2]:
            msg += f"   ✓ {r}\n"
        for w in v.get("warnings", [])[:2]:
            msg += f"   ⚠ {w}\n"
        msg += "\n"
    await q.message.reply_text(msg, parse_mode="Markdown")
