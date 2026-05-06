"""Telegram inline keyboard builders."""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def signal_keyboard(symbol: str, ai_enabled: bool = True) -> InlineKeyboardMarkup:
    row1 = [
        InlineKeyboardButton("📊 تتبع الصفقة", callback_data=f"track:{symbol}"),
        InlineKeyboardButton("❌ تجاهل", callback_data=f"ignore:{symbol}"),
    ]
    row2 = []
    if ai_enabled:
        row2.append(InlineKeyboardButton("🤖 تحليل AI", callback_data=f"ai:{symbol}"))
    row2.append(InlineKeyboardButton("📈 التفاصيل", callback_data=f"detail:{symbol}"))
    return InlineKeyboardMarkup([row1, row2])


def trades_list_keyboard(trades: dict) -> InlineKeyboardMarkup:
    """Show all open trades with close buttons + clear all option."""
    rows = []
    if not trades:
        rows.append([InlineKeyboardButton("لا توجد صفقات مفتوحة", callback_data="noop")])
    else:
        # One row per trade: close button + symbol info
        for symbol in list(trades.keys())[:10]:  # max 10
            rows.append([
                InlineKeyboardButton(
                    f"🗑 إغلاق {symbol}",
                    callback_data=f"force_close:{symbol}",
                ),
            ])
        # Clear all button if multiple trades
        if len(trades) > 1:
            rows.append([
                InlineKeyboardButton(
                    f"🗑️🗑️ مسح كل الصفقات ({len(trades)})",
                    callback_data="clear_all_trades",
                ),
            ])
    rows.append([InlineKeyboardButton("⬅️ رجوع", callback_data="cmd:menu")])
    return InlineKeyboardMarkup(rows)


def confirm_clear_keyboard() -> InlineKeyboardMarkup:
    """Confirmation for clear all trades."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ نعم، احذف الكل", callback_data="confirm_clear_yes"),
         InlineKeyboardButton("❌ إلغاء", callback_data="confirm_clear_no")],
    ])


def exit_keyboard(symbol: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ تم الخروج", callback_data=f"closed:{symbol}"),
        InlineKeyboardButton("⏳ انتظر", callback_data=f"hold:{symbol}"),
    ]])


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 مسح الآن", callback_data="cmd:scan"),
         InlineKeyboardButton("📊 الحالة", callback_data="cmd:status")],
        [InlineKeyboardButton("⚙️ الإعدادات", callback_data="cmd:settings"),
         InlineKeyboardButton("📈 صفقاتي", callback_data="cmd:trades")],
        [InlineKeyboardButton("🤖 المسح التلقائي", callback_data="cmd:autoscan_toggle")],
    ])


def settings_keyboard(cfg: dict) -> InlineKeyboardMarkup:
    mode = cfg.get("mode").value if hasattr(cfg.get("mode"), 'value') else cfg.get("mode", "day")
    min_conf = cfg.get("min_confidence", 65)
    auto_scan = "🟢 ON" if cfg.get("auto_scan") else "🔴 OFF"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"الوضع: {mode}", callback_data="set:mode")],
        [InlineKeyboardButton(f"الحد الأدنى: {min_conf}", callback_data="set:conf")],
        [InlineKeyboardButton(f"المسح التلقائي: {auto_scan}", callback_data="cmd:autoscan_toggle")],
        [InlineKeyboardButton("⬅️ رجوع", callback_data="cmd:menu")],
    ])


def mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ Scalp (15m-1h)", callback_data="mode:scalp")],
        [InlineKeyboardButton("☀️ Day (1-8h)", callback_data="mode:day")],
        [InlineKeyboardButton("🌙 Swing (أيام)", callback_data="mode:swing")],
        [InlineKeyboardButton("⬅️ رجوع", callback_data="cmd:settings")],
    ])


def conf_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("55 (متساهل)", callback_data="conf:55"),
         InlineKeyboardButton("65 (متوازن)", callback_data="conf:65")],
        [InlineKeyboardButton("75 (صارم)", callback_data="conf:75"),
         InlineKeyboardButton("85 (ذهبي)", callback_data="conf:85")],
        [InlineKeyboardButton("⬅️ رجوع", callback_data="cmd:settings")],
    ])
