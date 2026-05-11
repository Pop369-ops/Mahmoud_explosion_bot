"""
Performance Reports + Recommendations Engine.

Generates user-facing weekly/monthly reports that show:
  - Win rate trends
  - Best/worst performing setups
  - Actionable recommendations for improving the bot

Reports are formatted for Telegram (Arabic, with emojis and clear tables).
"""
from datetime import datetime, timedelta
from risk.advanced_tracker import AdvancedTracker, MethodStats
from core.logger import get_logger

log = get_logger(__name__)


# ────────────────────────────────────────────────────────────
# REPORTS
# ────────────────────────────────────────────────────────────
async def build_weekly_report(tracker: AdvancedTracker, chat_id: int) -> str:
    """Build a comprehensive 7-day performance report."""
    import asyncio

    try:
        overall = await asyncio.wait_for(
            tracker.get_overall_summary(chat_id, days=7), timeout=8.0
        )
    except asyncio.TimeoutError:
        return ("⚠️ *قاعدة البيانات بطيئة الاستجابة*\n\n"
                "حاول مرة أخرى خلال دقيقة.")
    except Exception as e:
        return f"❌ خطأ في قاعدة البيانات: {e}"

    if not overall or overall.get("total_signals", 0) == 0:
        return ("📊 *تقرير الأسبوع*\n\n"
                "لا توجد بيانات كافية بعد.\n\n"
                "البوت بدأ تجميع البيانات الآن.\n"
                "ستحتاج 5+ صفقات مغلقة على الأقل قبل أن يكون التقرير مفيداً.\n\n"
                "_جرّب /stats للملخص السريع._\n"
                "_التقرير الكامل يحتاج 24-48 ساعة من العمل النشط._")

    # Parallelize all queries to speed up report generation
    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                tracker.get_method_stats(chat_id, days=7),
                tracker.get_killzone_stats(chat_id, days=7),
                tracker.get_direction_stats(chat_id, days=7),
                tracker.get_tier_b_accuracy(chat_id, days=7),
                tracker.get_confidence_buckets(chat_id, days=7),
                tracker.get_rejection_summary(chat_id, days=7),
                return_exceptions=True,
            ),
            timeout=15.0,
        )
        method_stats = results[0] if not isinstance(results[0], Exception) else []
        killzone_stats = results[1] if not isinstance(results[1], Exception) else {}
        direction_stats = results[2] if not isinstance(results[2], Exception) else {}
        tier_b_stats = results[3] if not isinstance(results[3], Exception) else {}
        confidence_buckets = results[4] if not isinstance(results[4], Exception) else {}
        rejections = results[5] if not isinstance(results[5], Exception) else {}
    except asyncio.TimeoutError:
        return ("⚠️ *تجميع الإحصائيات تأخر*\n\n"
                "حاول مرة أخرى خلال دقيقة.")
    except Exception as e:
        return f"❌ {e}"

    msg = "📊 *تقرير الأداء — آخر 7 أيام*\n"
    msg += "━━━━━━━━━━━━━━━━━━━━━━\n\n"

    # ─── Overall ────────────────────────────────────────
    msg += "📈 *الملخص العام:*\n"
    msg += f"  • إجمالي الإشارات: `{overall['total_signals']}`\n"
    msg += f"  • مرفوضة: `{overall['rejected_count']}`\n"
    msg += f"  • مقبولة: `{overall['accepted_count']}`\n"
    msg += f"  • مغلقة: `{overall['closed_count']}`\n"

    if overall["closed_count"] > 0:
        wr = overall["win_rate"]
        wr_emoji = "🟢" if wr >= 60 else "🟡" if wr >= 50 else "🔴"
        msg += f"  • معدل الفوز: {wr_emoji} `{wr:.1f}%`\n"
        msg += f"  • متوسط PnL: `{overall['avg_pnl_pct']:+.2f}%`\n"
        msg += f"  • إجمالي PnL: `{overall['total_pnl_pct']:+.2f}%`\n"

    msg += "\n"

    # ─── Direction Breakdown ────────────────────────────
    if direction_stats:
        msg += "🎯 *الأداء حسب الاتجاه:*\n"
        for dirname, stats in direction_stats.items():
            emoji = "🐂" if dirname == "long" else "🐻"
            msg += (f"  • {emoji} *{dirname.upper()}*: "
                    f"`{stats['total']}` صفقة | "
                    f"فوز `{stats['win_rate']:.0f}%` | "
                    f"متوسط `{stats['avg_pnl']:+.2f}%`\n")
        msg += "\n"

    # ─── SL Method Performance ──────────────────────────
    if method_stats:
        msg += "🛡 *أداء طرق SL:*\n"
        # Sort by win rate
        method_stats.sort(key=lambda m: m.win_rate, reverse=True)
        for m in method_stats[:6]:
            if m.total < 2:
                continue
            wr_emoji = "🟢" if m.win_rate >= 60 else "🟡" if m.win_rate >= 50 else "🔴"
            msg += (f"  {wr_emoji} `{m.method[:25]}` — "
                    f"`{m.winners}/{m.total}` "
                    f"({m.win_rate:.0f}%) | متوسط `{m.avg_pnl_pct:+.1f}%`\n")
        msg += "\n"

    # ─── Killzone Performance ───────────────────────────
    if killzone_stats:
        msg += "🌍 *الأداء حسب الـ Killzone:*\n"
        for kz, stats in sorted(killzone_stats.items(),
                                  key=lambda x: x[1]["win_rate"], reverse=True):
            if stats["total"] < 2:
                continue
            kz_name = {
                "london": "🇬🇧 لندن", "ny": "🇺🇸 نيويورك",
                "asian": "🇯🇵 آسيا", "none": "بدون killzone",
            }.get(kz, kz)
            msg += (f"  • {kz_name}: `{stats['winners']}/{stats['total']}` "
                    f"({stats['win_rate']:.0f}%)\n")
        msg += "\n"

    # ─── Confidence Buckets ─────────────────────────────
    if confidence_buckets:
        msg += "🎲 *معدل الفوز حسب الثقة:*\n"
        for bucket, stats in confidence_buckets.items():
            if stats["total"] < 2:
                continue
            msg += (f"  • ثقة `{bucket}`: "
                    f"`{stats['winners']}/{stats['total']}` "
                    f"({stats['win_rate']:.0f}%) | "
                    f"متوسط `{stats['avg_pnl']:+.1f}%`\n")
        msg += "\n"

    # ─── Tier B Accuracy ────────────────────────────────
    if tier_b_stats:
        msg += "🔔 *دقة تنبيهات Tier B:*\n"
        for alert_type, stats in tier_b_stats.items():
            type_ar = "الإيقاظ" if alert_type == "awakening" else "الانعكاس"
            msg += (f"  • تنبيهات {type_ar}: "
                    f"`{stats['accurate']}/{stats['total']}` "
                    f"({stats['accuracy_pct']:.0f}% دقة)\n")
        msg += "\n"

    # ─── Top Rejection Reasons ──────────────────────────
    if rejections:
        msg += "🚫 *أكثر أسباب الرفض:*\n"
        for reason, count in list(rejections.items())[:5]:
            short_reason = reason[:50] + ("..." if len(reason) > 50 else "")
            msg += f"  • `{count}×`: {short_reason}\n"
        msg += "\n"

    msg += "━━━━━━━━━━━━━━━━━━━━━━\n"
    msg += "💡 لتوصيات تحسين البوت: /recommendations"

    return msg


async def build_recommendations(tracker: AdvancedTracker, chat_id: int) -> str:
    """Generate actionable recommendations based on data."""
    import asyncio

    try:
        overall = await asyncio.wait_for(
            tracker.get_overall_summary(chat_id, days=14), timeout=8.0
        )
    except asyncio.TimeoutError:
        return "⚠️ قاعدة البيانات بطيئة الاستجابة. حاول مرة أخرى."
    except Exception as e:
        return f"❌ {e}"

    if not overall or overall.get("closed_count", 0) < 5:
        return ("💡 *التوصيات*\n\n"
                "البوت يحتاج 5+ صفقات مغلقة قبل توليد توصيات.\n"
                "البيانات الحالية غير كافية.\n\n"
                "_حاول مرة أخرى بعد أسبوع._")

    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                tracker.get_method_stats(chat_id, days=14),
                tracker.get_killzone_stats(chat_id, days=14),
                tracker.get_direction_stats(chat_id, days=14),
                tracker.get_confidence_buckets(chat_id, days=14),
                tracker.get_tier_b_accuracy(chat_id, days=14),
                return_exceptions=True,
            ),
            timeout=15.0,
        )
        method_stats = results[0] if not isinstance(results[0], Exception) else []
        killzone_stats = results[1] if not isinstance(results[1], Exception) else {}
        direction_stats = results[2] if not isinstance(results[2], Exception) else {}
        confidence_buckets = results[3] if not isinstance(results[3], Exception) else {}
        tier_b_stats = results[4] if not isinstance(results[4], Exception) else {}
    except asyncio.TimeoutError:
        return "⚠️ تجميع التوصيات تأخر. حاول مرة أخرى."
    except Exception as e:
        return f"❌ {e}"

    msg = "💡 *توصيات تحسين البوت — بناءً على بياناتك*\n"
    msg += "━━━━━━━━━━━━━━━━━━━━━━\n\n"

    recommendations = []

    # ─── Recommendation 1: Overall win rate ─────────────
    wr = overall["win_rate"]
    if wr < 45:
        recommendations.append(
            f"🔴 *معدل الفوز منخفض ({wr:.0f}%)*\n"
            f"  → ارفع `min_confidence` إلى 75+ في /settings\n"
            f"  → ارفع `awakening_threshold` إلى 85"
        )
    elif wr >= 65:
        recommendations.append(
            f"🟢 *معدل فوز ممتاز ({wr:.0f}%)*\n"
            f"  → يمكنك تخفيف الفلاتر قليلاً لإشارات أكثر\n"
            f"  → يمكنك زيادة risk per trade إلى 1.5%"
        )

    # ─── Recommendation 2: SL methods ───────────────────
    if method_stats:
        # Find any method with very low win rate
        weak_methods = [m for m in method_stats
                          if m.total >= 3 and m.win_rate < 35]
        for m in weak_methods:
            recommendations.append(
                f"⚠️ *طريقة SL `{m.method}` ضعيفة* "
                f"({m.winners}/{m.total} = {m.win_rate:.0f}%)\n"
                f"  → فكّر في تجنب الإشارات بهذه الطريقة"
            )
        # Find best method to recommend
        strong = [m for m in method_stats
                   if m.total >= 3 and m.win_rate >= 65]
        if strong:
            best = max(strong, key=lambda m: m.win_rate)
            recommendations.append(
                f"✅ *أقوى طريقة SL: `{best.method}` "
                f"({best.win_rate:.0f}% فوز)*\n"
                f"  → ركّز على الإشارات بهذه الطريقة"
            )

    # ─── Recommendation 3: Direction bias ───────────────
    if direction_stats:
        longs = direction_stats.get("long", {})
        shorts = direction_stats.get("short", {})
        if longs.get("total", 0) >= 3 and shorts.get("total", 0) >= 3:
            long_wr = longs["win_rate"]
            short_wr = shorts["win_rate"]
            if abs(long_wr - short_wr) > 25:
                weaker = "SHORT" if short_wr < long_wr else "LONG"
                recommendations.append(
                    f"⚠️ *تحيّز في الاتجاه*\n"
                    f"  LONG: {long_wr:.0f}% | SHORT: {short_wr:.0f}%\n"
                    f"  → فكر في تجنب اتجاه `{weaker}` مؤقتاً"
                )

    # ─── Recommendation 4: Killzone optimization ────────
    if killzone_stats:
        kz_items = [(k, v) for k, v in killzone_stats.items()
                     if v["total"] >= 3]
        if len(kz_items) >= 2:
            best_kz = max(kz_items, key=lambda x: x[1]["win_rate"])
            worst_kz = min(kz_items, key=lambda x: x[1]["win_rate"])
            if best_kz[1]["win_rate"] - worst_kz[1]["win_rate"] > 25:
                kz_ar = {"london": "لندن", "ny": "نيويورك",
                          "asian": "آسيا", "none": "خارج Killzone"}
                recommendations.append(
                    f"🌍 *فروق Killzone كبيرة*\n"
                    f"  أفضل: {kz_ar.get(best_kz[0], best_kz[0])} ({best_kz[1]['win_rate']:.0f}%)\n"
                    f"  أسوأ: {kz_ar.get(worst_kz[0], worst_kz[0])} ({worst_kz[1]['win_rate']:.0f}%)\n"
                    f"  → تجنب التداول في {kz_ar.get(worst_kz[0], worst_kz[0])}"
                )

    # ─── Recommendation 5: Confidence threshold ─────────
    if confidence_buckets:
        low_bucket = confidence_buckets.get("65-74", {})
        high_bucket = confidence_buckets.get("85-100", {})
        if low_bucket.get("total", 0) >= 3 and high_bucket.get("total", 0) >= 2:
            if low_bucket["win_rate"] < high_bucket["win_rate"] - 20:
                recommendations.append(
                    f"📊 *إشارات الثقة المنخفضة ضعيفة*\n"
                    f"  ثقة 65-74: {low_bucket['win_rate']:.0f}% فوز\n"
                    f"  ثقة 85+: {high_bucket['win_rate']:.0f}% فوز\n"
                    f"  → ارفع `min_confidence` إلى 80"
                )

    # ─── Recommendation 6: Tier B accuracy ──────────────
    if tier_b_stats:
        for alert_type, stats in tier_b_stats.items():
            if stats["total"] >= 5:
                acc = stats["accuracy_pct"]
                type_ar = "الإيقاظ" if alert_type == "awakening" else "الانعكاس"
                threshold_cmd = ("/awaken_threshold"
                                  if alert_type == "awakening"
                                  else "/reversal_threshold")
                if acc < 40:
                    recommendations.append(
                        f"🔔 *تنبيهات {type_ar} ضعيفة* ({acc:.0f}% دقة)\n"
                        f"  → ارفع الحد عبر `{threshold_cmd} 85`"
                    )
                elif acc > 70:
                    recommendations.append(
                        f"✅ *تنبيهات {type_ar} ممتازة* ({acc:.0f}% دقة)\n"
                        f"  → يمكنك تخفيف الحد لاستلام تنبيهات أكثر"
                    )

    # ─── Output ─────────────────────────────────────────
    if not recommendations:
        msg += ("✅ *البوت يعمل بكفاءة جيدة*\n\n"
                "لا توجد تعديلات حرجة موصى بها حالياً.\n"
                "استمر في التشغيل وراجع التقرير بعد أسبوع.")
    else:
        for i, rec in enumerate(recommendations, 1):
            msg += f"*{i}.* {rec}\n\n"

    msg += "━━━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"_بناءً على {overall['closed_count']} صفقة مغلقة في آخر 14 يوم_"

    return msg


async def build_quick_stats(tracker: AdvancedTracker, chat_id: int) -> str:
    """Quick one-line summary for /stats command."""
    import asyncio
    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                tracker.get_overall_summary(chat_id, days=1),
                tracker.get_overall_summary(chat_id, days=7),
                tracker.get_overall_summary(chat_id, days=30),
                return_exceptions=True,
            ),
            timeout=10.0,
        )
        today = results[0] if not isinstance(results[0], Exception) else {}
        week = results[1] if not isinstance(results[1], Exception) else {}
        month = results[2] if not isinstance(results[2], Exception) else {}
    except asyncio.TimeoutError:
        return "⚠️ قاعدة البيانات بطيئة. حاول مرة أخرى."
    except Exception as e:
        return f"❌ {e}"

    msg = "📊 *إحصائيات سريعة*\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n\n"

    for label, data in [("اليوم", today), ("الأسبوع", week), ("الشهر", month)]:
        if data.get("closed_count", 0) > 0:
            wr = data["win_rate"]
            wr_emoji = "🟢" if wr >= 60 else "🟡" if wr >= 50 else "🔴"
            msg += (f"📅 *{label}:*\n"
                    f"  إشارات: `{data['total_signals']}` "
                    f"(مرفوضة `{data['rejected_count']}`)\n"
                    f"  صفقات مغلقة: `{data['closed_count']}` "
                    f"({wr_emoji} `{wr:.0f}%` فوز)\n"
                    f"  PnL إجمالي: `{data['total_pnl_pct']:+.2f}%`\n\n")
        else:
            msg += f"📅 *{label}:* لا صفقات مغلقة\n\n"

    msg += "━━━━━━━━━━━━━━━━━━━━\n"
    msg += "للتفاصيل: /report\n"
    msg += "للتوصيات: /recommendations"
    return msg
