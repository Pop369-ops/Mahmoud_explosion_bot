"""
Awakening alert formatter — Tier B early warning messages.

These are NOT trade signals — they're "watch this coin" alerts.
Format is intentionally different from full Signal alerts to avoid confusion.
"""
from core.models import AwakeningAlert, now_riyadh_str


def build_awakening_alert(alert: AwakeningAlert) -> str:
    """Build the Tier B awakening alert message."""

    # Direction badge
    if alert.direction == "long_likely":
        dir_badge = "🟢 *الاتجاه المحتمل: LONG*"
        action_hint = "راقب لدخول LONG عند تأكيد الحركة"
    elif alert.direction == "short_likely":
        dir_badge = "🔴 *الاتجاه المحتمل: SHORT*"
        action_hint = "راقب لدخول SHORT عند تأكيد الحركة"
    else:
        dir_badge = "⚪ *الاتجاه غير محسوم*"
        action_hint = "حركة قادمة لكن الاتجاه غير واضح بعد"

    # Confidence label based on score
    if alert.awakening_score >= 90:
        quality = "🔥 إنذار قوي جداً"
    elif alert.awakening_score >= 80:
        quality = "⚡ إنذار قوي"
    else:
        quality = "⚠️ إنذار مبدئي"

    sym_short = alert.symbol.replace("USDT", "")

    msg = f"🔔 *تنبيه إيقاظ مبكر — Tier B*\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━━\n\n"
    msg += f"💎 *العملة:* `{alert.symbol}`\n"
    msg += f"💰 *السعر:* `${alert.price:.6g}`\n"
    msg += f"📊 *حركة 15د:* `{alert.change_15m:+.2f}%`\n\n"

    msg += f"{dir_badge}\n"
    msg += f"📈 *درجة الإيقاظ:* `{alert.awakening_score}/100`\n"
    msg += f"🎯 *إشارات نشطة:* `{alert.signals_fired}/6`\n"
    msg += f"💡 *الجودة:* {quality}\n\n"

    msg += f"━━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"🔍 *الإشارات المرصودة:*\n\n"

    for sig in alert.signals:
        if sig.triggered:
            msg += f"  • {sig.reason_ar} `(+{sig.score})`\n"

    msg += f"\n━━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"🎯 *التوصية:*\n{action_hint}\n\n"

    msg += f"⚠️ *تحذير مهم:*\n"
    msg += f"هذا تنبيه مبكر فقط — ليس إشارة دخول.\n"
    msg += f"معدل الصواب المتوقع: ~40-50%.\n"
    msg += f"البوت سيرسل إشارة كاملة (Tier C) لو تأكدت الحركة.\n\n"

    msg += f"⏰ {now_riyadh_str()}"

    return msg


def build_no_awakenings_msg(hours: int = 4) -> str:
    """Message for /awaken when no recent awakenings."""
    return (f"🔕 *لا توجد تنبيهات إيقاظ في آخر {hours} ساعات*\n\n"
            f"النظام يفحص قائمة Watch كل 30 ثانية.\n"
            f"شاهد القائمة الكاملة بأمر /watch")


def build_awakenings_history(alerts: list[AwakeningAlert], hours: int = 4) -> str:
    """Format recent awakening history for /awaken command."""
    if not alerts:
        return build_no_awakenings_msg(hours)

    msg = f"📡 *تنبيهات الإيقاظ — آخر {hours} ساعات*\n"
    msg += f"العدد: {len(alerts)}\n\n"

    # Most recent first
    sorted_alerts = sorted(alerts, key=lambda a: a.timestamp, reverse=True)

    for i, a in enumerate(sorted_alerts[:15], 1):
        sym_short = a.symbol.replace("USDT", "")
        time_str = a.timestamp.strftime("%H:%M")
        dir_emoji = ("🟢" if a.direction == "long_likely"
                      else "🔴" if a.direction == "short_likely" else "⚪")
        msg += (f"`{i:2}.` {dir_emoji} *{sym_short}* — "
                f"score `{a.awakening_score}` "
                f"({a.signals_fired}/6) — `{time_str}`\n")

    return msg
