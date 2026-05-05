"""Telegram command handlers."""
from telegram import Update
from telegram.ext import ContextTypes
from core.state import state
from ui.alerts import build_status_message
from ui.keyboards import main_menu_keyboard, settings_keyboard


WELCOME = """🚀 *EXPLOSION_BOT v4 — ICT Pro*

البوت يحلل العملات بـ **8+ مصادر بيانات مستقلة + ICT methodology**

*الأوامر الأساسية:*
/scan - مسح الآن
/test SYMBOL - اختبر رمز محدد
/status - حالة البوت + صفقاتك
/menu - القائمة الرئيسية
/settings - الإعدادات

*إدارة المخاطر:* 💰
/capital 10000 - حدد رأس مالك
/risk 1.0 - نسبة المخاطرة (%)
/perf - أداءك (today/week/month/all)
/pause 4 - إيقاف لساعات
/resume - استئناف

*التحليل المتقدم:* 📊
/sentiment - معنويات السوق + خيارات BTC/ETH
/backtest BTCUSDT 14 - اختبار خلفي

⚠️ _للأغراض التعليمية فقط_
"""

HELP = """📖 *دليل الاستخدام*

*🎯 كيف يكشف البوت؟*
كل عملة تخضع لـ 10 محللين مستقلين. الإشارة تظهر فقط إذا اتفق على الأقل 4 منهم.

*🔥 المراحل:*
🌱 تراكم مبكر — قبل الحركة (الذهب الحقيقي)
⚡ بدء الانفجار
🚨 انفجار الآن
💎 إشارة فاخرة (5+ مصادر متفقة)

*⚙️ الأوضاع:*
- *Scalp:* SL ضيق، TP سريعة
- *Day:* متوازن — افتراضي
- *Swing:* SL واسع، TP بعيدة

*🔘 الأزرار:*
- 📊 *تتبع*: ابدأ مراقبة الصفقة (المراقبة لا تبدأ تلقائياً)
- ❌ *تجاهل*: لن يزعجك بهذه العملة لساعة
- 🤖 *AI*: تحليل عميق
- 📈 *التفاصيل*: breakdown كامل
"""


async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    chat_id = u.effective_chat.id
    state.get_user_cfg(chat_id)
    await u.message.reply_text(WELCOME, parse_mode="Markdown",
                                reply_markup=main_menu_keyboard())


async def cmd_help(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(HELP, parse_mode="Markdown")


async def cmd_menu(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("🏠 *القائمة الرئيسية*", parse_mode="Markdown",
                                reply_markup=main_menu_keyboard())


async def cmd_status(u: Update, c: ContextTypes.DEFAULT_TYPE):
    chat_id = u.effective_chat.id
    trades = state.get_trades(chat_id)
    last = state.get_last_results(chat_id)
    msg = build_status_message(chat_id, trades, last)
    await u.message.reply_text(msg, parse_mode="Markdown")


async def cmd_settings(u: Update, c: ContextTypes.DEFAULT_TYPE):
    chat_id = u.effective_chat.id
    cfg = state.get_user_cfg(chat_id)
    await u.message.reply_text("⚙️ *الإعدادات*", parse_mode="Markdown",
                                reply_markup=settings_keyboard(cfg))


async def cmd_scan(u: Update, c: ContextTypes.DEFAULT_TYPE):
    from scanner.orchestrator import run_scan_for_user
    chat_id = u.effective_chat.id
    msg = await u.message.reply_text("🔍 جاري المسح...")
    try:
        signals = await run_scan_for_user(chat_id, c.bot, send_alerts=True)
        if signals:
            await msg.edit_text(f"✅ انتهى المسح — {len(signals)} إشارة")
        else:
            await msg.edit_text("✅ انتهى المسح — لا توجد إشارات قوية حالياً")
    except Exception as e:
        await msg.edit_text(f"❌ خطأ: {e}")


async def cmd_test(u: Update, c: ContextTypes.DEFAULT_TYPE):
    from scanner.orchestrator import scan_single_symbol
    from core.models import Phase
    chat_id = u.effective_chat.id
    parts = u.message.text.split()
    symbol = parts[1].upper() if len(parts) > 1 else "BTCUSDT"
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    msg = await u.message.reply_text(f"🔍 تحليل {symbol}...")
    try:
        sig = await scan_single_symbol(chat_id, symbol, c.bot, send_alert=False)

        if sig is None:
            await msg.edit_text(f"❌ {symbol}: لم يتمكن من جلب البيانات")
            return

        # Build informative report regardless of phase
        report = f"📊 تحليل {symbol}\n"
        report += "━━━━━━━━━━━━━━━━━━━━\n\n"
        report += f"💰 السعر: ${sig.price:.6g}\n"
        report += f"📊 24h: {sig.change_24h:+.2f}%\n"
        report += f"🎯 Confidence: {sig.confidence}/100\n"
        report += f"🛡 المصادر المتفقة: {sig.sources_agreed}/{sig.sources_total}\n\n"

        # Show top verdict scores
        if sig.verdicts:
            report += "📡 المصادر:\n"
            verdict_names = {
                "volume_delta": "حجم الشراء",
                "order_book": "دفتر الأوامر",
                "funding_divergence": "Funding",
                "whale_flow": "الحيتان",
                "squeeze_breakout": "Squeeze",
                "pre_explosion": "تراكم مبكر",
                "btc_trend": "BTC اتجاه",
                "btc_correlation": "BTC ارتباط",
                "liquidity": "السيولة",
                "multi_timeframe": "Multi-TF",
            }
            for v in sig.verdicts:
                if v.confidence < 0.2:
                    continue
                emoji = "🟢" if v.score >= 65 else "🔴" if v.score < 40 else "⚪"
                name = verdict_names.get(v.name, v.name)
                report += f"  {emoji} {name}: {v.score}/100\n"
            report += "\n"

        # Phase outcome
        if sig.phase == Phase.REJECTED:
            report += f"🚫 مرفوضة بواسطة HARD GATE\n"
            if sig.rejected_reason:
                report += f"السبب: {sig.rejected_reason}\n"
        elif sig.phase == Phase.NONE:
            report += "⚪ لا إشارة (Confidence أو المصادر تحت العتبة)\n"
            if sig.confidence < 50:
                report += f"  (يحتاج confidence >= 50 + sources >= 3)\n"
        else:
            report += f"✅ إشارة: {sig.phase.value}\n"
            report += f"الدخول: ${sig.entry:.6g}\n"
            report += f"SL: ${sig.sl:.6g} ({sig.sl_pct:.2f}%)\n"
            report += f"TP1: ${sig.tp1:.6g}\n"
            # Send full alert too
            from ui.alerts import build_entry_alert
            full_msg = build_entry_alert(sig, chat_id=chat_id)
            await msg.delete()
            await u.message.reply_text(full_msg, parse_mode="Markdown")
            return

        # Show warnings if any
        if sig.warnings:
            report += "\n⚠️ تحذيرات:\n"
            for w in sig.warnings[:3]:
                report += f"  {w}\n"

        await msg.edit_text(report)
    except Exception as e:
        await msg.edit_text(f"❌ {type(e).__name__}: {e}")


# ════════════════════════════════════════════════════════
# Risk management commands
# ════════════════════════════════════════════════════════

async def cmd_capital(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Set trading capital. /capital 10000"""
    from risk.daily_limits import risk_manager
    chat_id = u.effective_chat.id
    parts = u.message.text.split()
    if len(parts) < 2:
        st = risk_manager.get(chat_id)
        await u.message.reply_text(
            f"💰 *رأس المال الحالي:* `${st.capital:,.2f}`\n\n"
            f"*للتحديث:* `/capital 10000`\n"
            f"*مثال:* لو رأس مالك $5000 → `/capital 5000`",
            parse_mode="Markdown",
        )
        return
    try:
        amount = float(parts[1].replace(",", "").replace("$", ""))
        if amount <= 0 or amount > 10_000_000:
            raise ValueError("range")
        risk_manager.set_capital(chat_id, amount)
        await u.message.reply_text(
            f"✅ *تم تحديد رأس المال:* `${amount:,.2f}`\n\n"
            f"البوت سيحسب لك حجم المركز الصحيح في كل إشارة "
            f"بحيث لا تخسر أكثر من *1%* في الصفقة الواحدة.\n\n"
            f"*لتغيير نسبة المخاطرة:* `/risk 1.5` (للمخاطرة بـ 1.5%)",
            parse_mode="Markdown",
        )
    except (ValueError, IndexError):
        await u.message.reply_text("❌ مبلغ غير صحيح. مثال: `/capital 10000`",
                                     parse_mode="Markdown")


async def cmd_risk(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Set risk per trade. /risk 1.5"""
    from risk.daily_limits import risk_manager
    chat_id = u.effective_chat.id
    cfg = state.get_user_cfg(chat_id)
    parts = u.message.text.split()
    if len(parts) < 2:
        risk_pct = cfg.get("risk_pct", 1.0)
        st = risk_manager.get(chat_id)
        await u.message.reply_text(
            f"⚖️ *إدارة المخاطر*\n\n"
            f"💰 رأس المال: `${st.capital:,.2f}`\n"
            f"🎯 المخاطرة لكل صفقة: `{risk_pct}%`\n"
            f"💸 المبلغ المعرض للخسارة: `${st.capital * risk_pct / 100:,.2f}`\n\n"
            f"📉 *الحدود اليومية:*\n"
            f"  • خسارة يومية قصوى: `{st.daily_loss_limit_pct}%`\n"
            f"  • خسارة أسبوعية قصوى: `{st.weekly_loss_limit_pct}%`\n"
            f"  • خسائر متتالية للتوقف: `{st.consecutive_loss_limit}`\n"
            f"  • صفقات متزامنة قصوى: `{st.max_concurrent_trades}`\n\n"
            f"*أمثلة:*\n"
            f"`/risk 0.5` — محافظ\n"
            f"`/risk 1.0` — متوازن (موصى به)\n"
            f"`/risk 2.0` — جريء\n",
            parse_mode="Markdown",
        )
        return
    try:
        risk_pct = float(parts[1])
        if risk_pct <= 0 or risk_pct > 5:
            await u.message.reply_text(
                "❌ نسبة المخاطرة لازم بين 0.1% و 5%",
            )
            return
        cfg["risk_pct"] = risk_pct
        warning = ""
        if risk_pct > 2:
            warning = f"\n\n⚠️ *تنبيه:* {risk_pct}% فوق الموصى به (2%)"
        await u.message.reply_text(
            f"✅ *تم تحديد المخاطرة:* `{risk_pct}%` لكل صفقة{warning}",
            parse_mode="Markdown",
        )
    except (ValueError, IndexError):
        await u.message.reply_text("❌ قيمة غير صحيحة. مثال: `/risk 1.0`",
                                     parse_mode="Markdown")


async def cmd_perf(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Show performance stats. /perf [today|week|month|all]"""
    from risk.performance import PerformanceTracker, render_stats_message
    from config.settings import settings
    chat_id = u.effective_chat.id
    parts = u.message.text.split()
    period = parts[1].lower() if len(parts) > 1 else "all"
    if period not in ("today", "week", "month", "all"):
        period = "all"

    tracker = PerformanceTracker(settings.db_path)
    try:
        stats = await tracker.get_stats(chat_id, period=period)
        msg = render_stats_message(stats)
        await u.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await u.message.reply_text(f"❌ خطأ: {e}")


async def cmd_pause(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Pause bot manually. /pause 4 (hours)"""
    from risk.daily_limits import risk_manager
    chat_id = u.effective_chat.id
    parts = u.message.text.split()
    hours = 4
    if len(parts) > 1:
        try:
            hours = max(1, min(168, int(parts[1])))  # 1h to 7 days
        except ValueError:
            pass
    risk_manager.manual_pause(chat_id, hours, reason="manual user pause")
    await u.message.reply_text(
        f"⏸ *تم إيقاف البوت {hours} ساعة*\n\n"
        f"_للاستئناف قبل الموعد:_ `/resume`",
        parse_mode="Markdown",
    )


async def cmd_resume(u: Update, c: ContextTypes.DEFAULT_TYPE):
    from risk.daily_limits import risk_manager
    chat_id = u.effective_chat.id
    risk_manager.manual_resume(chat_id)
    await u.message.reply_text("▶️ *تم استئناف البوت*", parse_mode="Markdown")


async def cmd_backtest(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Run quick backtest. /backtest BTCUSDT 14"""
    from risk.backtest import backtest_symbol, render_backtest
    parts = u.message.text.split()
    symbol = parts[1].upper() if len(parts) > 1 else "BTCUSDT"
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    days = 14
    if len(parts) > 2:
        try:
            days = max(3, min(30, int(parts[2])))
        except ValueError:
            pass
    msg = await u.message.reply_text(
        f"🔬 جاري الـ backtest لـ {symbol} على آخر {days} يوم...\n"
        f"قد يأخذ 30-60 ثانية"
    )
    try:
        report = await backtest_symbol(symbol, days=days, mode="day")
        text = render_backtest(report)
        # Use plain text — no Markdown parsing
        await msg.edit_text(text)
    except Exception as e:
        await msg.edit_text(f"❌ خطأ: {type(e).__name__}: {e}")


async def cmd_scan_history(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Retroactively scan past N hours. /scan_history [hours] [top_n]"""
    from scanner.retroactive import scan_market_history, render_history_report
    parts = u.message.text.split()
    hours = 4
    top_n = 50
    if len(parts) > 1:
        try:
            hours = max(1, min(24, int(parts[1])))
        except ValueError:
            pass
    if len(parts) > 2:
        try:
            top_n = max(20, min(100, int(parts[2])))
        except ValueError:
            pass
    msg = await u.message.reply_text(
        f"🔍 جاري فحص آخر {hours} ساعات على top {top_n} عملة...\n"
        f"قد يأخذ 60-120 ثانية"
    )
    try:
        report = await scan_market_history(top_n=top_n, hours_back=hours)
        text = render_history_report(report)
        # Plain text — no Markdown
        await msg.edit_text(text)
    except Exception as e:
        await msg.edit_text(f"❌ خطأ: {type(e).__name__}: {e}")


async def cmd_sentiment(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Show market sentiment + options + on-chain summary."""
    from risk.sentiment import analyze_sentiment
    from risk.options_flow import analyze_options
    msg = await u.message.reply_text("📊 جاري تحليل المعنويات...")
    try:
        sent, btc_options, eth_options = await asyncio.gather(
            analyze_sentiment("long"),
            analyze_options("BTC"),
            analyze_options("ETH"),
            return_exceptions=True,
        )
        m = "📊 *معنويات السوق العامة*\n━━━━━━━━━━━━━━━━━━━━\n\n"

        if hasattr(sent, "fear_greed") and sent.fear_greed is not None:
            fg_emoji = "😱" if sent.fear_greed <= 25 else "😨" if sent.fear_greed <= 45 \
                else "😐" if sent.fear_greed <= 55 else "🙂" if sent.fear_greed <= 75 else "🤑"
            m += f"{fg_emoji} *Fear & Greed:* `{sent.fear_greed}/100` ({sent.fear_greed_label})\n"
            if sent.advisory:
                m += f"_{sent.advisory}_\n\n"

        if hasattr(sent, "btc_funding_aggregate"):
            funding_pct = sent.btc_funding_aggregate * 100
            m += f"💸 *BTC Funding:* `{funding_pct:+.4f}%`\n\n"

        for opt, label in [(btc_options, "BTC"), (eth_options, "ETH")]:
            if hasattr(opt, "raw_count") and opt.raw_count > 0:
                m += f"📊 *{label} Options:*\n"
                m += f"  • P/C ratio: `{opt.put_call_oi_ratio}` ({opt.sentiment})\n"
                if opt.max_pain_price > 0:
                    m += f"  • Max Pain: `${opt.max_pain_price:,.0f}` "
                    m += f"({opt.distance_to_max_pain_pct:+.1f}%)\n"
                m += "\n"

        await msg.edit_text(m, parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"❌ {e}")


import asyncio
