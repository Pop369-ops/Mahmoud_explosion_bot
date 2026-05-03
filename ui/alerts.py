"""Telegram message builders. Ported from v3 with multi-source transparency."""
from core.models import Signal, Trade, PHASE_LABELS, PHASE_ICONS, now_riyadh_str, fmt


def build_entry_alert(sig: Signal, rank: int = 1) -> str:
    icon = PHASE_ICONS.get(sig.phase, "⚡")
    phase_txt = PHASE_LABELS.get(sig.phase, sig.phase.value)
    bar_full = int(sig.confidence / 10)
    bar = "█" * bar_full + "░" * (10 - bar_full)

    m = f"{icon} *{phase_txt}*\n"
    m += f"🪙 *{sig.symbol}* | 🕐 {now_riyadh_str()}\n"
    m += "━━━━━━━━━━━━━━━━━━━━\n\n"
    m += f"💰 السعر: `${fmt(sig.price)}`\n"
    m += f"📊 24h: `{sig.change_24h:+.2f}%` | حجم: `${fmt(sig.volume_24h)}`\n"
    if sig.vol_ratio:
        m += f"📈 حجم x{sig.vol_ratio:.1f}"
    if sig.rsi:
        m += f" | RSI: `{sig.rsi:.0f}`"
    m += "\n"
    m += f"\n🎯 *الثقة: {sig.confidence}/100*\n`{bar}`\n"
    m += f"🛡 *المصادر المتفقة: {sig.sources_agreed}/{sig.sources_total} ✅*\n"
    m += f"   _جودة الإشارة: {sig.quality_label}_\n\n"

    if sig.verdicts:
        m += "📡 *المصادر:*\n"
        for v in sig.verdicts:
            if v.confidence < 0.2: continue
            emoji = "🟢" if v.score >= 65 else "🔴" if v.score < 40 else "⚪"
            m += f"  {emoji} {_pretty_name(v.name)}: `{v.score}/100`\n"
        m += "\n"

    if sig.signals:
        m += "✅ *إشارات:*\n"
        for s in sig.signals[:6]:
            m += f"  {s}\n"
        m += "\n"

    if sig.warnings:
        m += "⚠️ *تحذيرات:*\n"
        for w in sig.warnings[:4]:
            m += f"  {w}\n"
        m += "\n"

    m += "━━━━━━━━━━━━━━━━━━━━\n"
    m += f"🟢 *دخول:* `${fmt(sig.entry)}`\n"
    m += f"🔴 *SL:* `${fmt(sig.sl)}` _(-{sig.sl_pct:.1f}%)_\n"
    m += f"💰 *TP1:* `${fmt(sig.tp1)}` _(+{sig.sl_pct*1.5:.1f}%)_\n"
    m += f"💰 *TP2:* `${fmt(sig.tp2)}` _(+{sig.sl_pct*3:.1f}%)_\n"
    m += f"🏆 *TP3:* `${fmt(sig.tp3)}` _(+{sig.sl_pct*5:.1f}%)_\n\n"
    m += f"🎮 _الوضع: {sig.mode.value}_ | ⚠️ _للأغراض التعليمية فقط_"
    return m


def build_exit_alert(trade: Trade, ex: dict) -> str:
    action = ex.get("action", "hold")
    price = ex.get("price", trade.entry)
    pnl = (price - trade.entry) / trade.entry * 100 if trade.entry > 0 else 0
    reason = ex.get("reason", "")

    action_map = {
        "exit_now": "🚨 خروج فوري",
        "exit_partial": "💰 خروج جزئي مقترح",
        "exit_sl": "🔴 Stop Loss",
        "trail": "📈 رفع SL لنقطة الدخول",
    }
    action_txt = action_map.get(action, action)
    profit_icon = "✅" if pnl >= 0 else "❌"

    m = f"🚨 *{action_txt}* — {trade.symbol}\n"
    m += f"🕐 {now_riyadh_str()}\n"
    m += "━━━━━━━━━━━━━━━━━━━━\n\n"
    m += f"💰 السعر: `${fmt(price)}`\n"
    m += f"🟢 الدخول: `${fmt(trade.entry)}`\n"
    m += f"{profit_icon} *P/L: `{pnl:+.2f}%`*\n\n"
    m += f"📝 السبب: {reason}\n\n"
    m += "━━━━━━━━━━━━━━━━━━━━\n"
    m += "⚠️ _للأغراض التعليمية فقط_"
    return m


def build_status_message(chat_id: int, trades: dict, last_results: list) -> str:
    m = "📊 *حالة البوت*\n━━━━━━━━━━━━━━━━━━━━\n\n"
    if trades:
        m += f"*📈 الصفقات المفتوحة ({len(trades)}):*\n"
        for sym, t in trades.items():
            pnl = t.pnl_pct
            icon = "✅" if pnl >= 0 else "❌"
            m += f"  {icon} {sym}: `{pnl:+.2f}%`\n"
        m += "\n"
    else:
        m += "_لا صفقات مفتوحة_\n\n"

    if last_results:
        m += f"*🎯 آخر مسح ({len(last_results)} إشارة):*\n"
        for r in last_results[:5]:
            sym = r.symbol if hasattr(r, 'symbol') else r.get('sym', '?')
            conf = r.confidence if hasattr(r, 'confidence') else r.get('score', 0)
            m += f"  • {sym}: `{conf}/100`\n"
    return m


def _pretty_name(name: str) -> str:
    return {
        "volume_delta": "حجم الشراء",
        "order_book": "دفتر الأوامر",
        "funding_divergence": "Funding",
        "whale_flow": "الحيتان",
        "squeeze_breakout": "Squeeze",
        "pre_explosion": "تراكم مبكر",
        "multi_timeframe": "MTF",
        "btc_trend": "اتجاه BTC",
        "btc_correlation": "ارتباط BTC",
        "liquidity": "السيولة",
    }.get(name, name)
