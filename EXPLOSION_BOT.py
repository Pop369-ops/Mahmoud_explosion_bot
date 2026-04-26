"""
╔══════════════════════════════════════════════════════════════╗
║         EXPLOSION DETECTOR BOT — كاشف الانفجار            ║
║                                                              ║
║  يعمل تلقائياً 24/7 ويراقب كل عملات Binance               ║
║                                                              ║
║  المهام:                                                     ║
║  ① كشف الانفجار المبكر (قبل الحركة)                        ║
║  ② تنبيه بداية الانفجار (الدخول)                           ║
║  ③ تنبيه نهاية الانفجار (الخروج)                           ║
║  ④ إشارة عكس الصفقة (من شراء لبيع)                        ║
║                                                              ║
║  للأغراض التعليمية فقط                                      ║
╚══════════════════════════════════════════════════════════════╝
"""

import os, asyncio, logging, time
from datetime import datetime, timedelta, timezone
import requests
import pandas as pd
import numpy as np
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler,
    MessageHandler, CallbackQueryHandler,
    filters, ContextTypes,
)

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_TOKEN_HERE")
BASE      = "https://fapi.binance.com"
_TZ3      = timezone(timedelta(hours=3))

# ══════════════════════════════════════════════════════════════
# STATE
# ══════════════════════════════════════════════════════════════
# {chat_id: {sym: {score, phase, entry, sl, tp1, tp2, tp3, alert_ts, peak_price}}}
active_trades: dict = {}

# كاش آخر تنبيه {chat_id: {sym: timestamp}}
last_alert: dict = {}

# إعدادات المستخدم {chat_id: {min_score, scan_interval, active}}
user_cfg: dict = {}

# نتائج آخر مسح {chat_id: list}
last_results: dict = {}

# عملات مستبعدة
EXCLUDED = {
    "USDTUSDT","BUSDUSDT","USDCUSDT","DAIUSDT","TUSDUSDT",
    "FDUSDUSDT","WBTCUSDT","WETHUSDT","STETHUSDT",
}

# ══════════════════════════════════════════════════════════════
# HTTP
# ══════════════════════════════════════════════════════════════
_sess = requests.Session()
_sess.headers.update({"User-Agent": "ExplosionBot/2.0"})

def api_get(url, params=None, timeout=(6, 20)):
    try:
        r = _sess.get(url, params=params, timeout=timeout)
        return r
    except Exception as e:
        logging.warning(f"[HTTP] {url[:50]}: {e}")
        return None

def fmt(v):
    if v is None: return "—"
    v = float(v)
    if v >= 1e9: return f"{v/1e9:.2f}B"
    if v >= 1e6: return f"{v/1e6:.2f}M"
    if v >= 1e3: return f"{v/1e3:.1f}K"
    if v >= 1:   return f"{v:,.4f}"
    return f"{v:.8f}"

def now_sa():
    return datetime.now(_TZ3).strftime("%H:%M:%S %d/%m/%Y")

# ══════════════════════════════════════════════════════════════
# جلب البيانات
# ══════════════════════════════════════════════════════════════

def fetch_all_usdt(min_vol=1_000_000) -> list:
    """جلب كل رموز USDT مع حجم > min_vol."""
    r = api_get(f"https://api.binance.com/api/v3/ticker/24hr", timeout=(10, 25))
    if not r or r.status_code != 200:
        return []
    result = []
    for t in r.json():
        sym = t.get("symbol", "")
        if not sym.endswith("USDT") or sym in EXCLUDED:
            continue
        try:
            vol = float(t.get("quoteVolume", 0))
            if vol >= min_vol:
                result.append({
                    "sym":     sym,
                    "price":   float(t.get("lastPrice", 0)),
                    "chg_24h": float(t.get("priceChangePercent", 0)),
                    "vol_24h": vol,
                    "high_24": float(t.get("highPrice", 0)),
                    "low_24":  float(t.get("lowPrice", 0)),
                })
        except:
            continue
    result.sort(key=lambda x: x["vol_24h"], reverse=True)
    return result


def fetch_klines(sym, interval="5m", limit=80) -> pd.DataFrame:
    """جلب شموع — Futures أولاً ثم Spot."""
    cols = ["t","o","h","l","c","v","ct","qv","tr","bb","bq","ig"]
    num  = ["o","h","l","c","v","qv","bq"]
    for url in [
        f"{BASE}/fapi/v1/klines",
        "https://api.binance.com/api/v3/klines",
    ]:
        r = api_get(url, {"symbol": sym, "interval": interval, "limit": limit}, timeout=(6, 15))
        if r and r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and len(data) > 10:
                df = pd.DataFrame(data, columns=cols)
                for col in num:
                    df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
                return df
    return None

# ══════════════════════════════════════════════════════════════
# محرك الكشف
# ══════════════════════════════════════════════════════════════

def analyze_coin(sym: str, meta: dict) -> dict:
    """
    تحليل عملة واحدة وإعطاء نقاط الانفجار.

    المؤشرات:
    ① Volume Surge     — ارتفاع مفاجئ في الحجم
    ② CVD Breakout     — CVD ينكسر للأعلى
    ③ BB Breakout      — كسر Bollinger العلوي
    ④ BB Squeeze       — هدوء قبل الانفجار
    ⑤ RSI Momentum     — RSI يكسر 60 صاعداً
    ⑥ High Breakout    — كسر أعلى 20 شمعة
    ⑦ Buy Pressure     — ضغط شراء متتالي
    """
    result = {
        "sym":         sym,
        "score":       0,
        "phase":       "none",
        "signals":     [],
        "warnings":    [],
        "price":       meta.get("price", 0),
        "chg_24h":     meta.get("chg_24h", 0),
        "vol_24h":     meta.get("vol_24h", 0),
        "entry":       0, "sl": 0,
        "tp1": 0, "tp2": 0, "tp3": 0,
        "sl_pct":      0,
        "atr":         0,
        "vol_ratio":   0,
        "rsi":         50,
    }

    df = fetch_klines(sym, "5m", 80)
    if df is None or len(df) < 40:
        return result

    c = df["c"]; h = df["h"]; l = df["l"]
    o = df["o"]; v = df["v"]
    bq = df["bq"]; qv = df["qv"]

    price = float(c.iloc[-1])
    if price <= 0:
        return result
    result["price"] = price

    score = 0

    # ── ① Volume Surge ──
    vol_now   = float(v.iloc[-1])
    vol_prev3 = float(v.iloc[-4:-1].mean()) + 1e-9
    vol_avg20 = float(v.iloc[-21:-1].mean()) + 1e-9
    vol_ratio = vol_now / vol_avg20
    result["vol_ratio"] = vol_ratio

    if vol_ratio >= 6.0 and (vol_now / vol_prev3) >= 3.0:
        score += 30
        result["signals"].append(f"⚡ Volume انفجر x{vol_ratio:.1f} — مفاجئ تماماً!")
    elif vol_ratio >= 4.0:
        score += 22
        result["signals"].append(f"📊 Volume ارتفع x{vol_ratio:.1f}")
    elif vol_ratio >= 2.5:
        score += 12
        result["signals"].append(f"📊 Volume مرتفع x{vol_ratio:.1f}")
    elif vol_ratio < 0.5:
        score -= 5
        result["warnings"].append("⚠️ حجم منخفض جداً")

    # ── ② CVD Breakout ──
    try:
        if bq.sum() > 0:
            cvd      = (bq - (qv - bq)).cumsum()
        else:
            bull = (c > o).astype(float)
            cvd  = (v * (2 * bull - 1)).cumsum()

        cvd_now   = float(cvd.iloc[-1])
        cvd_5ago  = float(cvd.iloc[-6])
        cvd_20ago = float(cvd.iloc[-21])
        cvd_delta = cvd_now - cvd_5ago
        cvd_trend = cvd_now - cvd_20ago

        if cvd_delta > 0 and cvd_trend > 0:
            if abs(cvd_delta) > abs(cvd_20ago) * 0.5:
                score += 25
                result["signals"].append("💚 CVD انكسر للأعلى بقوة (شراء ضخم حقيقي)")
            else:
                score += 15
                result["signals"].append("✅ CVD صاعد")
        elif cvd_delta < 0 and cvd_trend < 0:
            score -= 10
            result["warnings"].append("🔴 CVD هابط")
    except:
        pass

    # ── ③ + ④ Bollinger ──
    bb_mid  = c.rolling(20).mean()
    bb_std  = c.rolling(20).std()
    bb_up   = bb_mid + 2 * bb_std
    bb_lo   = bb_mid - 2 * bb_std
    bb_up_n = float(bb_up.iloc[-1])
    bb_lo_n = float(bb_lo.iloc[-1])
    bb_m_n  = float(bb_mid.iloc[-1])
    bb_w    = (bb_up_n - bb_lo_n) / bb_m_n * 100 if bb_m_n > 0 else 5

    # هل كان في Squeeze؟
    bb_w_prev = float(((bb_up - bb_lo) / bb_mid).iloc[-15:-2].mean()) * 100
    was_squeeze = bb_w_prev < 1.8

    prev_price = float(c.iloc[-2])
    prev_bb_up = float(bb_up.iloc[-2])

    if price > bb_up_n and prev_price <= prev_bb_up:
        if was_squeeze:
            score += 30
            result["signals"].append("💎 Squeeze→Breakout — إشارة ذهبية نادرة!")
        else:
            score += 20
            result["signals"].append("🚀 كسر Bollinger العلوي الآن!")
    elif price > bb_up_n:
        score += 12
        result["signals"].append("📈 فوق Bollinger العلوي")
    elif bb_w < 1.5 and was_squeeze:
        score += 8
        result["signals"].append(f"⏳ Squeeze نشط ({bb_w:.2f}%) — انفجار وشيك")
        result["phase"] = "pre_explosion"

    # ── ⑤ RSI ──
    delta = c.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rsi_s = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    rsi_n = float(rsi_s.iloc[-1])
    rsi_p = float(rsi_s.iloc[-2])
    result["rsi"] = rsi_n

    if 60 < rsi_n <= 75 and rsi_p <= 60:
        score += 20
        result["signals"].append(f"⚡ RSI كسر 60 صاعداً ({rsi_n:.1f}) — دخول مثالي!")
    elif rsi_n > 75:
        score += 8
        result["signals"].append(f"🔥 RSI قوي ({rsi_n:.1f})")
    elif rsi_n > 60:
        score += 5
    elif rsi_n < 40:
        score -= 8
        result["warnings"].append(f"⚠️ RSI ضعيف ({rsi_n:.1f})")

    # ── ⑥ High Breakout ──
    high20 = float(h.iloc[-21:-1].max())
    high5  = float(h.iloc[-6:-1].max())
    if price > high20 and prev_price <= high5:
        score += 20
        result["signals"].append(f"🏆 كسر أعلى سعر في 20 شمعة!")
    elif price > high20:
        score += 10
        result["signals"].append(f"📈 فوق أعلى 20 شمعة")

    # ── ⑦ Buy Pressure ──
    bull5 = (c.iloc[-5:] > o.iloc[-5:]).sum()
    if bull5 >= 4:
        score += 10
        result["signals"].append(f"🟢 {bull5}/5 شموع صاعدة متتالية")

    # ── ATR للـ SL/TP ──
    hv = h.values; lv = l.values; cv = c.values
    tr_vals = [
        max(hv[i]-lv[i], abs(hv[i]-cv[i-1]), abs(lv[i]-cv[i-1]))
        for i in range(1, len(cv))
    ]
    atr = float(pd.Series(tr_vals, dtype=float).rolling(14).mean().iloc[-1]) if len(tr_vals) >= 14 else price * 0.015
    result["atr"] = atr

    sl_dist = max(atr * 1.5, price * 0.015)
    result["entry"] = round(price, 8)
    result["sl"]    = round(price - sl_dist, 8)
    result["tp1"]   = round(price + sl_dist * 1.5, 8)
    result["tp2"]   = round(price + sl_dist * 3.0, 8)
    result["tp3"]   = round(price + sl_dist * 5.0, 8)
    result["sl_pct"] = round(sl_dist / price * 100, 2)

    result["score"] = min(100, max(0, score))

    if result["score"] >= 70:
        result["phase"] = "explosion_now"
    elif result["score"] >= 50:
        result["phase"] = "explosion_start"
    elif result["score"] >= 35:
        result["phase"] = "pre_explosion"

    return result


def analyze_exit(sym: str, trade: dict) -> dict:
    """
    تحليل هل يجب الخروج من الصفقة أو عكسها.
    """
    result = {
        "action":  "hold",
        "reason":  "",
        "price":   0,
        "rsi":     50,
        "signals": [],
    }

    df = fetch_klines(sym, "5m", 40)
    if df is None or len(df) < 20:
        return result

    c = df["c"]; h = df["h"]; l = df["l"]
    o = df["o"]; v = df["v"]
    bq = df["bq"]; qv = df["qv"]

    price = float(c.iloc[-1])
    result["price"] = price

    entry      = trade.get("entry", price)
    sl         = trade.get("sl", price * 0.98)
    tp1        = trade.get("tp1", price * 1.02)
    peak_price = trade.get("peak_price", price)
    profit_pct = (price - entry) / entry * 100

    # ── تحديث أعلى سعر ──
    if price > peak_price:
        trade["peak_price"] = price

    # ── تحقق SL ──
    if price <= sl:
        result["action"] = "exit_sl"
        result["reason"] = f"🔴 وصل Stop Loss — خسارة {abs(profit_pct):.1f}%"
        return result

    # ── مؤشرات الخروج ──
    exit_score = 0

    # RSI ذروة شراء
    delta = c.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rsi   = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    rsi_n = float(rsi.iloc[-1])
    rsi_p = float(rsi.iloc[-2])
    result["rsi"] = rsi_n

    if rsi_n > 80 and rsi_n < rsi_p:
        exit_score += 30
        result["signals"].append(f"🔴 RSI ذروة شراء ({rsi_n:.1f}) وبدأ ينزل")
    elif rsi_n > 75:
        exit_score += 15
        result["signals"].append(f"⚠️ RSI مرتفع جداً ({rsi_n:.1f})")

    # CVD يتراجع
    try:
        if bq.sum() > 0:
            cvd = (bq - (qv - bq)).cumsum()
        else:
            bull = (c > o).astype(float)
            cvd  = (v * (2 * bull - 1)).cumsum()
        cvd_now   = float(cvd.iloc[-1])
        cvd_3ago  = float(cvd.iloc[-4])
        if cvd_now < cvd_3ago and profit_pct > 2:
            exit_score += 25
            result["signals"].append("🔴 CVD بدأ ينعكس (بيع يدخل)")
    except:
        pass

    # Volume ينخفض (فقدان الزخم)
    vol_now  = float(v.iloc[-1])
    vol_avg  = float(v.iloc[-10:-1].mean()) + 1e-9
    if vol_now < vol_avg * 0.4 and profit_pct > 3:
        exit_score += 20
        result["signals"].append("⚠️ Volume انخفض كثيراً — الزخم يضعف")

    # Bollinger — السعر رجع داخل النطاق
    bb_mid = c.rolling(20).mean()
    bb_std = c.rolling(20).std()
    bb_up  = bb_mid + 2 * bb_std
    bb_up_n = float(bb_up.iloc[-1])
    prev_price = float(c.iloc[-2])
    if prev_price > bb_up_n and price <= bb_up_n and profit_pct > 2:
        exit_score += 25
        result["signals"].append("🔴 السعر رجع داخل Bollinger — إشارة بيع")

    # انعكاس قوي من القمة
    peak = trade.get("peak_price", price)
    drop_from_peak = (peak - price) / peak * 100 if peak > 0 else 0
    if drop_from_peak >= 3 and profit_pct > 0:
        exit_score += 20
        result["signals"].append(f"⚠️ تراجع {drop_from_peak:.1f}% من القمة")
    elif drop_from_peak >= 5:
        exit_score += 35
        result["signals"].append(f"🚨 تراجع {drop_from_peak:.1f}% من القمة — خروج عاجل!")

    # ── القرار ──
    if exit_score >= 70:
        result["action"] = "exit_now"
        result["reason"] = f"🚨 إشارات خروج قوية — ربح {profit_pct:+.1f}%"
    elif exit_score >= 45:
        result["action"] = "exit_partial"
        result["reason"] = f"⚠️ إشارات خروج — ربح {profit_pct:+.1f}%"
    elif exit_score >= 25 and profit_pct < -2:
        result["action"] = "reverse"
        result["reason"] = f"🔄 عكس الصفقة للبيع — خسارة {abs(profit_pct):.1f}%"

    return result

# ══════════════════════════════════════════════════════════════
# بناء الرسائل
# ══════════════════════════════════════════════════════════════

PHASE_LABELS = {
    "explosion_now":   "🚨 انفجار الآن!",
    "explosion_start": "⚡ بدء الانفجار",
    "pre_explosion":   "⏳ على وشك الانفجار",
}

def build_entry_alert(r: dict, rank: int = 1) -> str:
    sym   = r["sym"]
    score = r["score"]
    phase = r["phase"]
    price = r["price"]
    chg   = r["chg_24h"]
    vol   = r["vol_24h"]
    sigs  = r["signals"]
    warns = r["warnings"]
    slp   = r["sl_pct"]

    phase_txt = PHASE_LABELS.get(phase, "")
    icon = "🚨" if phase == "explosion_now" else "⚡"
    bar  = "█" * int(score/10) + "░" * (10 - int(score/10))

    m  = f"{icon} *{phase_txt}*\n"
    m += f"🪙 *{sym}* | 🕐 {now_sa()}\n"
    m += "━━━━━━━━━━━━━━━━━━━━\n\n"
    m += f"💰 السعر: `${fmt(price)}`\n"
    m += f"📊 24h: `{chg:+.2f}%` | حجم: `${fmt(vol)}`\n"
    m += f"📈 حجم x{r.get('vol_ratio',0):.1f} | RSI: `{r.get('rsi',50):.0f}`\n"
    m += f"🎯 نقاط الانفجار: `{score}/100`\n"
    m += f"`{bar}`\n\n"

    m += "📡 *الإشارات:*\n"
    for s in sigs: m += f"  {s}\n"
    if warns:
        m += "\n⚠️ *تحذيرات:*\n"
        for w in warns: m += f"  {w}\n"
    m += "\n"

    m += "━━━━━━━━━━━━━━━━━━━━\n"
    m += f"🟢 *دخول:* `${fmt(price)}`\n"
    m += f"🔴 *SL:*   `${fmt(r['sl'])}` _(-{slp:.1f}%)_\n"
    m += f"💰 *TP1:*  `${fmt(r['tp1'])}` _(+{slp*1.5:.1f}%)_\n"
    m += f"💰 *TP2:*  `${fmt(r['tp2'])}` _(+{slp*3:.1f}%)_\n"
    m += f"🏆 *TP3:*  `${fmt(r['tp3'])}` _(+{slp*5:.1f}%)_\n\n"
    m += "⚠️ _للأغراض التعليمية فقط_"
    return m


def build_exit_alert(sym: str, trade: dict, ex: dict) -> str:
    action    = ex["action"]
    price     = ex["price"]
    entry     = trade.get("entry", price)
    profit    = (price - entry) / entry * 100
    sigs      = ex.get("signals", [])
    reason    = ex.get("reason", "")

    action_map = {
        "exit_now":     "🚨 خروج فوري!",
        "exit_partial": "⚠️ خروج جزئي مقترح",
        "exit_sl":      "🔴 Stop Loss وصل",
        "reverse":      "🔄 عكس الصفقة للبيع!",
    }
    action_txt = action_map.get(action, "")

    profit_icon = "✅" if profit >= 0 else "❌"

    m  = f"{'🚨' if action in ('exit_now','exit_sl') else '⚠️'} *{action_txt}*\n"
    m += f"🪙 *{sym}* | 🕐 {now_sa()}\n"
    m += "━━━━━━━━━━━━━━━━━━━━\n\n"
    m += f"💰 السعر الحالي: `${fmt(price)}`\n"
    m += f"🟢 سعر الدخول:  `${fmt(entry)}`\n"
    m += f"{profit_icon} الربح/الخسارة: `{profit:+.2f}%`\n\n"
    m += f"السبب: {reason}\n\n"

    if sigs:
        m += "📡 *الإشارات:*\n"
        for s in sigs: m += f"  {s}\n"
        m += "\n"

    if action == "reverse":
        short_tp = price * 0.97
        m += "━━━━━━━━━━━━━━━━━━━━\n"
        m += "🔄 *إشارة بيع (Short):*\n"
        m += f"  دخول: `${fmt(price)}`\n"
        m += f"  TP:   `${fmt(short_tp)}`\n"
        m += f"  SL:   `${fmt(price*1.02)}`\n\n"

    m += "⚠️ _للأغراض التعليمية فقط_"
    return m

# ══════════════════════════════════════════════════════════════
# Jobs
# ══════════════════════════════════════════════════════════════

async def scanner_job(ctx: ContextTypes.DEFAULT_TYPE):
    """مسح كل Binance كل 5 دقائق."""
    chat_id   = ctx.job.data["chat_id"]
    min_score = ctx.job.data.get("min_score", 60)
    now_ts    = time.time()
    cooldown  = 3600  # ساعة بين تنبيهين لنفس العملة

    try:
        loop = asyncio.get_event_loop()

        def do_scan():
            coins  = fetch_all_usdt(min_vol=2_000_000)[:120]
            alerts = []
            for meta in coins:
                sym = meta["sym"]
                # تخطي إذا عندنا صفقة مفتوحة أو تنبهنا مؤخراً
                in_trade = sym in active_trades.get(chat_id, {})
                in_cooldown = now_ts - last_alert.get(chat_id, {}).get(sym, 0) < cooldown
                if in_trade or in_cooldown:
                    continue
                try:
                    r = analyze_coin(sym, meta)
                    if r["score"] >= min_score and r["phase"] != "none":
                        alerts.append(r)
                except:
                    continue
            alerts.sort(key=lambda x: x["score"], reverse=True)
            return alerts

        alerts = await asyncio.wait_for(
            loop.run_in_executor(None, do_scan), timeout=240)

        last_results[chat_id] = alerts

        for i, r in enumerate(alerts[:3], 1):
            sym = r["sym"]
            last_alert.setdefault(chat_id, {})[sym] = now_ts

            # سجّل في الصفقات المفتوحة
            active_trades.setdefault(chat_id, {})[sym] = {
                "entry":       r["entry"],
                "sl":          r["sl"],
                "tp1":         r["tp1"],
                "tp2":         r["tp2"],
                "tp3":         r["tp3"],
                "score":       r["score"],
                "phase":       r["phase"],
                "peak_price":  r["price"],
                "alert_ts":    now_ts,
            }

            msg = build_entry_alert(r, rank=i)
            try:
                await ctx.bot.send_message(
                    chat_id=chat_id, text=msg,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("📊 تابع الصفقة", callback_data=f"track:{sym}"),
                        InlineKeyboardButton("❌ تجاهل", callback_data=f"ignore:{sym}"),
                    ]]))
            except Exception as e:
                logging.warning(f"[SCANNER] send: {e}")

    except asyncio.TimeoutError:
        logging.warning("[SCANNER] timeout")
    except Exception as e:
        logging.warning(f"[SCANNER] error: {e}")


async def monitor_job(ctx: ContextTypes.DEFAULT_TYPE):
    """مراقبة الصفقات المفتوحة كل دقيقتين."""
    chat_id = ctx.job.data["chat_id"]
    trades  = active_trades.get(chat_id, {})
    if not trades:
        return

    loop = asyncio.get_event_loop()

    for sym, trade in list(trades.items()):
        try:
            meta = {"price": 0, "chg_24h": 0, "vol_24h": 0}
            r    = await asyncio.wait_for(
                loop.run_in_executor(None, analyze_exit, sym, trade),
                timeout=30)

            if r["action"] in ("exit_now", "exit_sl", "exit_partial", "reverse"):
                msg = build_exit_alert(sym, trade, r)
                try:
                    await ctx.bot.send_message(
                        chat_id=chat_id, text=msg,
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("✅ تم الخروج", callback_data=f"closed:{sym}"),
                            InlineKeyboardButton("⏳ انتظر", callback_data=f"hold:{sym}"),
                        ]]))
                except Exception as e:
                    logging.warning(f"[MONITOR] send: {e}")

                # إزالة من الصفقات المفتوحة إذا خروج كامل أو SL
                if r["action"] in ("exit_now", "exit_sl"):
                    active_trades.get(chat_id, {}).pop(sym, None)

        except Exception as e:
            logging.warning(f"[MONITOR] {sym}: {e}")

# ══════════════════════════════════════════════════════════════
# Handlers
# ══════════════════════════════════════════════════════════════

async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        "💥 *EXPLOSION DETECTOR BOT*\n"
        "كاشف الانفجار السعري المبكر\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⚙️ *الأوامر:*\n\n"
        "🟢 *تفعيل الكاشف:*\n"
        "`كاشف`         — يبدأ المسح كل 5 دقائق\n"
        "`كاشف 50`      — حساسية أعلى (يرصد أكثر)\n"
        "`كاشف 70`      — فقط الإشارات القوية\n\n"
        "🔴 *إيقاف:*\n"
        "`وقف`          — إيقاف الكاشف\n\n"
        "📊 *معلومات:*\n"
        "`نتائج`        — آخر عمليات الرصد\n"
        "`صفقاتي`       — الصفقات المفتوحة\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🎯 *مؤشرات الكشف:*\n"
        "• Volume Surge x4+ مفاجئ\n"
        "• CVD ينكسر للأعلى\n"
        "• كسر Bollinger Squeeze\n"
        "• RSI يكسر 60 صاعداً\n"
        "• كسر أعلى سعر 20 شمعة\n\n"
        "🔔 *تنبيه خروج تلقائي عند:*\n"
        "• وصول Stop Loss\n"
        "• RSI ذروة شراء + انعكاس\n"
        "• CVD ينعكس\n"
        "• تراجع 3%+ من القمة\n\n"
        "⚠️ _للأغراض التعليمية فقط_",
        parse_mode="Markdown")


async def handle_msg(u: Update, c: ContextTypes.DEFAULT_TYPE):
    text    = u.message.text.strip()
    chat_id = u.effective_chat.id
    cfg     = user_settings.setdefault(chat_id, {"min_score": 60, "active": False})

    # ── تفعيل ──
    if text.startswith("كاشف") or text.lower() in ("start scan","explosion"):
        parts  = text.split()
        min_sc = cfg.get("min_score", 60)
        for p in parts:
            try:
                n = int(p)
                if 30 <= n <= 90:
                    min_sc = n
            except:
                pass
        cfg["min_score"] = min_sc
        cfg["active"]    = True

        # إيقاف القديم إن وجد
        for jname in [f"scan_{chat_id}", f"mon_{chat_id}"]:
            for j in c.job_queue.get_jobs_by_name(jname):
                j.schedule_removal()

        # كاشف كل 5 دقائق
        c.job_queue.run_repeating(
            scanner_job, interval=300, first=15,
            data={"chat_id": chat_id, "min_score": min_sc},
            name=f"scan_{chat_id}")

        # مراقبة الصفقات كل دقيقتين
        c.job_queue.run_repeating(
            monitor_job, interval=120, first=60,
            data={"chat_id": chat_id},
            name=f"mon_{chat_id}")

        await u.message.reply_text(
            f"🚨 *تم تفعيل كاشف الانفجار*\n\n"
            f"⏱ مسح كل 5 دقائق\n"
            f"🎯 حد النقاط: `{min_sc}/100`\n"
            f"📊 يراقب أعلى 120 عملة حجماً\n"
            f"👁 مراقبة الصفقات كل دقيقتين\n\n"
            f"سيرسل تنبيه فور اكتشاف:\n"
            f"• انفجار مبكر\n"
            f"• إشارة دخول\n"
            f"• إشارة خروج أو عكس\n\n"
            f"إيقاف: `وقف`",
            parse_mode="Markdown")
        return

    # ── إيقاف ──
    if text in ("وقف","stop","إيقاف"):
        for jname in [f"scan_{chat_id}", f"mon_{chat_id}"]:
            for j in c.job_queue.get_jobs_by_name(jname):
                j.schedule_removal()
        cfg["active"] = False
        await u.message.reply_text(
            "⛔ *تم إيقاف كاشف الانفجار*\n"
            "الصفقات المفتوحة لا تزال قيد المراقبة حتى تُغلقها.\n"
            "إعادة تفعيل: `كاشف`",
            parse_mode="Markdown")
        return

    # ── نتائج ──
    if text in ("نتائج","آخر مسح","results"):
        results = last_results.get(chat_id, [])
        if not results:
            await u.message.reply_text(
                "لا توجد نتائج بعد.\nفعّل الكاشف: `كاشف`",
                parse_mode="Markdown")
            return
        m = f"📊 *آخر نتائج الكاشف ({len(results)} عملة):*\n\n"
        for i, r in enumerate(results[:5], 1):
            phase_txt = PHASE_LABELS.get(r["phase"], r["phase"])
            m += (f"*{i}. {r['sym']}* `{r['score']}/100` — {phase_txt}\n"
                  f"   💰 `${fmt(r['price'])}` | 24h: `{r['chg_24h']:+.1f}%`\n")
            for s in r["signals"][:2]:
                m += f"   {s}\n"
            m += "\n"
        await u.message.reply_text(m, parse_mode="Markdown")
        return

    # ── صفقاتي ──
    if text in ("صفقاتي","صفقات","trades"):
        trades = active_trades.get(chat_id, {})
        if not trades:
            await u.message.reply_text("لا توجد صفقات مفتوحة حالياً.")
            return
        m = f"📋 *الصفقات المفتوحة ({len(trades)}):*\n\n"
        for sym, t in trades.items():
            entry = t.get("entry", 0)
            peak  = t.get("peak_price", entry)
            m += (f"*{sym}*\n"
                  f"  دخول: `${fmt(entry)}`\n"
                  f"  TP1: `${fmt(t.get('tp1',0))}` | SL: `${fmt(t.get('sl',0))}`\n"
                  f"  أعلى سعر: `${fmt(peak)}`\n\n")
        await u.message.reply_text(m, parse_mode="Markdown")
        return

    await u.message.reply_text(
        "الأوامر:\n`كاشف` — تفعيل\n`وقف` — إيقاف\n`نتائج` — آخر رصد\n`صفقاتي` — صفقات مفتوحة",
        parse_mode="Markdown")


async def handle_callback(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q       = u.callback_query
    chat_id = q.message.chat_id
    data    = q.data
    await q.answer()

    if data.startswith("track:"):
        sym = data.split(":", 1)[1]
        trade = active_trades.get(chat_id, {}).get(sym, {})
        if trade:
            entry = trade.get("entry", 0)
            await q.edit_message_text(
                f"👁 *تتبع {sym}*\n\n"
                f"دخول: `${fmt(entry)}`\n"
                f"TP1: `${fmt(trade.get('tp1',0))}`\n"
                f"TP2: `${fmt(trade.get('tp2',0))}`\n"
                f"TP3: `${fmt(trade.get('tp3',0))}`\n"
                f"SL: `${fmt(trade.get('sl',0))}`\n\n"
                f"سيرسل البوت تنبيه تلقائي عند إشارة الخروج.",
                parse_mode="Markdown")
        else:
            await q.answer("الصفقة غير موجودة", show_alert=True)

    elif data.startswith("ignore:"):
        sym = data.split(":", 1)[1]
        active_trades.get(chat_id, {}).pop(sym, None)
        await q.answer(f"✅ تم تجاهل {sym}", show_alert=True)
        await q.edit_message_reply_markup(reply_markup=None)

    elif data.startswith("closed:"):
        sym = data.split(":", 1)[1]
        active_trades.get(chat_id, {}).pop(sym, None)
        await q.answer(f"✅ تم إغلاق {sym}", show_alert=True)
        await q.edit_message_reply_markup(reply_markup=None)

    elif data.startswith("hold:"):
        await q.answer("⏳ حسناً — سنستمر بالمراقبة", show_alert=True)


# ══════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════

user_settings = {}  # {chat_id: {min_score, active}}

def main():
    if BOT_TOKEN in ("YOUR_TOKEN_HERE", ""):
        print("❌ أضف BOT_TOKEN في Railway Variables")
        return

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))

    print("💥 EXPLOSION DETECTOR BOT — Running")
    print("   يراقب كل عملات Binance كل 5 دقائق")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
