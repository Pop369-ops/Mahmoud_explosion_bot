"""Telegram command handlers."""
from telegram import Update
from telegram.ext import ContextTypes
from core.state import state
from ui.alerts import build_status_message
from ui.keyboards import main_menu_keyboard, settings_keyboard


WELCOME = """🚀 *EXPLOSION_BOT v4 — أهلاً بك*

البوت يحلل العملات بـ **6+ مصادر بيانات مستقلة**:
  🐋 Whale Alert (تحركات الحيتان)
  📊 Volume Delta (ضغط الشراء الفعلي)
  📖 Order Book (دفتر الأوامر)
  💸 Funding Divergence
  📈 Multi-Timeframe (15m + 1h)
  ₿ BTC Trend & Correlation

*التتبع يدوي* — لما تشوف إشارة، اضغط "📊 تتبع الصفقة" لو عجبتك فقط.

*الأوامر:*
/scan - مسح الآن
/status - حالة البوت + صفقاتك
/menu - القائمة الرئيسية
/settings - الإعدادات
/test SYMBOL - اختبر رمز محدد
/help - المساعدة

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
    chat_id = u.effective_chat.id
    parts = u.message.text.split()
    symbol = parts[1].upper() if len(parts) > 1 else "BTCUSDT"
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    msg = await u.message.reply_text(f"🔍 تحليل {symbol}...")
    try:
        sig = await scan_single_symbol(chat_id, symbol, c.bot, send_alert=True)
        if sig and sig.phase.value != "none":
            await msg.delete()
        else:
            await msg.edit_text(f"✅ {symbol}: لا إشارة قوية (confidence: {sig.confidence if sig else 'N/A'})")
    except Exception as e:
        await msg.edit_text(f"❌ {e}")
