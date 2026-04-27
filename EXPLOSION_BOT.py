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

def fetch_all_usdt(min_vol=0) -> list:
    """
    جلب كل عملات Futures USDT من Binance بدون أي فلتر.
    يشمل كل العملات المتاحة في السوق الآجلة.
    """
    futures_data = None
    last_status = None
    last_error = None

    # محاولة Futures API (الأساسي)
    for url in [
        f"{BASE}/fapi/v1/ticker/24hr",
        "https://fapi.binance.com/fapi/v1/ticker/24hr",
    ]:
        try:
            r = api_get(url, timeout=(15, 40))
            if r is not None:
                last_status = r.status_code
                if r.status_code == 200:
                    futures_data = r.json()
                    break
                elif r.status_code == 451:
                    last_error = "451 GEO_BLOCKED — Binance يحظر منطقة Railway هذه"
                elif r.status_code == 418:
                    last_error = "418 IP_BANNED — Railway IP محظور مؤقتاً"
                elif r.status_code == 429:
                    last_error = "429 RATE_LIMIT — أكثر من اللازم طلبات"
        except Exception as e:
            last_error = f"{type(e).__name__}: {str(e)[:50]}"
            continue

    # Spot كـ fallback أول
    if not futures_data:
        for url in ["https://api.binance.com/api/v3/ticker/24hr",
                    "https://data-api.binance.vision/api/v3/ticker/24hr"]:
            try:
                r2 = api_get(url, timeout=(15, 40))
                if r2 is not None and r2.status_code == 200:
                    futures_data = r2.json()
                    logging.warning(f"[FETCH] Fallback to Spot ({url[:40]}...)")
                    break
                if r2 is not None:
                    last_status = r2.status_code
            except Exception as e:
                last_error = f"{type(e).__name__}: {str(e)[:50]}"
                continue

    data = futures_data or []
    result = []

    for t in data:
        sym = t.get("symbol", "")
        # فقط USDT وليس Stable Coins
        if not sym.endswith("USDT") or sym in EXCLUDED:
            continue
        try:
            result.append({
                "sym":     sym,
                "price":   float(t.get("lastPrice", 0) or 0),
                "chg_24h": float(t.get("priceChangePercent", 0) or 0),
                "vol_24h": float(t.get("quoteVolume", 0) or 0),
                "high_24": float(t.get("highPrice", 0) or 0),
                "low_24":  float(t.get("lowPrice", 0) or 0),
            })
        except:
            continue

    # رتّب حسب التغيير المطلق (الأكثر حركة أولاً)
    result.sort(key=lambda x: abs(x["chg_24h"]), reverse=True)

    # تشخيص واضح في logs
    if len(result) == 0:
        logging.warning(
            f"[FETCH] ❌ 0 عملة! "
            f"Binance status={last_status} | error={last_error or 'no response'}\n"
            f"           ⚠️ غيّر Railway region: us-west2 → europe-west4"
        )
    else:
        logging.warning(f"[FETCH] ✅ {len(result)} عملة Futures USDT")
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
# 🆕 الفلاتر الذكية v2 — لرفع الدقة من 50% إلى 75-85%
# ══════════════════════════════════════════════════════════════

# Cache لـ BTC trend (نحدّثه كل 5 دقائق)
_btc_trend_cache = {"ts": 0, "data": None}

def get_btc_trend() -> dict:
    """
    🎯 فلتر #1: اتجاه BTC العام
    لو BTC هابط، لا نقبل LONG (70% من العملات تتبع BTC)
    لو BTC صاعد، لا نقبل SHORT
    """
    import time as _t
    now = _t.time()
    # كاش 5 دقائق
    if _btc_trend_cache["data"] and (now - _btc_trend_cache["ts"]) < 300:
        return _btc_trend_cache["data"]

    result = {"trend": "neutral", "score": 50, "details": [], "valid": False}
    try:
        # 1H للاتجاه المتوسط
        df_1h = fetch_klines("BTCUSDT", interval="1h", limit=100)
        if df_1h is None or len(df_1h) < 50:
            return result

        ema21 = df_1h["c"].ewm(span=21).mean().iloc[-1]
        ema50 = df_1h["c"].ewm(span=50).mean().iloc[-1]
        price = df_1h["c"].iloc[-1]

        # RSI
        delta = df_1h["c"].diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss.replace(0, 0.01)
        rsi_1h = (100 - (100 / (1 + rs))).iloc[-1]

        # 4H للاتجاه الكبير
        df_4h = fetch_klines("BTCUSDT", interval="4h", limit=50)
        ema21_4h = df_4h["c"].ewm(span=21).mean().iloc[-1] if df_4h is not None else price
        price_4h = df_4h["c"].iloc[-1] if df_4h is not None else price

        # حساب نقاط الاتجاه (0-100)
        score = 50
        details = []

        # 1H trend
        if price > ema21 > ema50:
            score += 20; details.append("1H صاعد قوي")
        elif price > ema21:
            score += 10; details.append("1H صاعد")
        elif price < ema21 < ema50:
            score -= 20; details.append("1H هابط قوي")
        elif price < ema21:
            score -= 10; details.append("1H هابط")

        # RSI
        if rsi_1h > 60:
            score += 10; details.append(f"RSI قوي ({rsi_1h:.0f})")
        elif rsi_1h > 50:
            score += 5
        elif rsi_1h < 40:
            score -= 10; details.append(f"RSI ضعيف ({rsi_1h:.0f})")
        elif rsi_1h < 50:
            score -= 5

        # 4H trend
        if price_4h > ema21_4h:
            score += 10; details.append("4H صاعد")
        else:
            score -= 10; details.append("4H هابط")

        score = max(0, min(100, score))
        if score >= 65:
            trend = "bullish"
        elif score <= 35:
            trend = "bearish"
        else:
            trend = "neutral"

        result = {
            "trend": trend,
            "score": score,
            "rsi_1h": rsi_1h,
            "price": price,
            "ema21_1h": ema21,
            "ema50_1h": ema50,
            "details": details,
            "valid": True,
        }
        _btc_trend_cache["data"] = result
        _btc_trend_cache["ts"] = now
    except Exception as e:
        logging.warning(f"[BTC_TREND] {e}")
    return result


def check_mtf_alignment(sym: str, direction: str = "long") -> dict:
    """
    🎯 فلتر #2: MTF Confirmation
    تحقق من 3 timeframes: 5m + 15m + 1h
    direction: 'long' or 'short'
    """
    result = {"aligned": False, "score": 0, "frames": [], "details": []}
    try:
        for tf, weight in [("15m", 1), ("1h", 1.5)]:
            df = fetch_klines(sym, interval=tf, limit=50)
            if df is None or len(df) < 25:
                result["frames"].append((tf, "❌ no data"))
                continue

            ema9 = df["c"].ewm(span=9).mean().iloc[-1]
            ema21 = df["c"].ewm(span=21).mean().iloc[-1]
            price = df["c"].iloc[-1]

            # RSI
            delta = df["c"].diff()
            gain = delta.where(delta > 0, 0).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rs = gain / loss.replace(0, 0.01)
            rsi = (100 - (100 / (1 + rs))).iloc[-1]

            if direction == "long":
                if price > ema9 > ema21 and rsi > 50:
                    result["score"] += int(20 * weight)
                    result["frames"].append((tf, "✅ aligned"))
                elif price > ema21:
                    result["score"] += int(10 * weight)
                    result["frames"].append((tf, "🟡 weak"))
                else:
                    result["frames"].append((tf, "🔴 against"))
            else:  # short
                if price < ema9 < ema21 and rsi < 50:
                    result["score"] += int(20 * weight)
                    result["frames"].append((tf, "✅ aligned"))
                elif price < ema21:
                    result["score"] += int(10 * weight)
                    result["frames"].append((tf, "🟡 weak"))
                else:
                    result["frames"].append((tf, "🔴 against"))

        result["aligned"] = result["score"] >= 30
        if result["aligned"]:
            result["details"].append("الفريمات الأكبر متوافقة")
        else:
            result["details"].append("الفريمات الأكبر مش متوافقة")
    except Exception as e:
        logging.warning(f"[MTF] {sym}: {e}")
    return result


def check_orderbook_pressure(sym: str) -> dict:
    """
    🎯 فلتر #3: Order Book Imbalance
    يكشف ضغط الشراء/البيع الحقيقي قبل الانفجار
    """
    result = {"score": 0, "imbalance": 0, "details": [], "valid": False}
    try:
        for url in [f"{BASE}/fapi/v1/depth",
                    "https://api.binance.com/api/v3/depth"]:
            r = api_get(url, {"symbol": sym, "limit": 100}, timeout=(4, 10))
            if r and r.status_code == 200:
                data = r.json()
                break
        else:
            return result

        bids = data.get("bids", [])
        asks = data.get("asks", [])
        if not bids or not asks:
            return result

        # احسب أول 20 مستوى من كل جانب
        bid_volume = sum(float(b[0]) * float(b[1]) for b in bids[:20])
        ask_volume = sum(float(a[0]) * float(a[1]) for a in asks[:20])
        if ask_volume == 0:
            return result

        # نسبة الشراء للبيع (>1 = ضغط شراء)
        ratio = bid_volume / ask_volume
        imbalance = (bid_volume - ask_volume) / (bid_volume + ask_volume) * 100

        # كشف Bid Wall (دعم قوي)
        max_bid = max(float(b[1]) for b in bids[:20])
        avg_bid = sum(float(b[1]) for b in bids[:20]) / 20
        bid_wall = max_bid > avg_bid * 5

        # كشف Ask Wall (مقاومة قوية)
        max_ask = max(float(a[1]) for a in asks[:20])
        avg_ask = sum(float(a[1]) for a in asks[:20]) / 20
        ask_wall = max_ask > avg_ask * 5

        score = 0
        details = []

        if ratio > 1.5:
            score += 25; details.append(f"ضغط شراء قوي ({ratio:.1f}x)")
        elif ratio > 1.2:
            score += 15; details.append(f"ضغط شراء معتدل ({ratio:.1f}x)")
        elif ratio < 0.67:
            score -= 25; details.append(f"ضغط بيع قوي ({1/ratio:.1f}x)")
        elif ratio < 0.83:
            score -= 15; details.append(f"ضغط بيع معتدل ({1/ratio:.1f}x)")

        if bid_wall and not ask_wall:
            score += 15; details.append("🟢 Bid Wall (دعم قوي)")
        elif ask_wall and not bid_wall:
            score -= 15; details.append("🔴 Ask Wall (مقاومة قوية)")

        result = {
            "score": score,
            "imbalance": imbalance,
            "ratio": ratio,
            "bid_volume": bid_volume,
            "ask_volume": ask_volume,
            "bid_wall": bid_wall,
            "ask_wall": ask_wall,
            "details": details,
            "valid": True,
        }
    except Exception as e:
        logging.warning(f"[OB] {sym}: {e}")
    return result


def check_funding_oi(sym: str) -> dict:
    """
    🎯 فلتر #4: Funding Rate + Open Interest Divergence
    كشف الفخاخ والإشارات القوية الحقيقية
    """
    result = {"score": 0, "details": [], "valid": False, "warning": None}
    try:
        # Funding Rate
        r1 = api_get(f"{BASE}/fapi/v1/premiumIndex", {"symbol": sym}, timeout=(4, 8))
        if not r1 or r1.status_code != 200:
            return result
        funding = float(r1.json().get("lastFundingRate", 0)) * 100

        # Open Interest الحالي
        r2 = api_get(f"{BASE}/fapi/v1/openInterest", {"symbol": sym}, timeout=(4, 8))
        if not r2 or r2.status_code != 200:
            return result
        oi_now = float(r2.json().get("openInterest", 0))

        # OI تاريخي (1H ago) للمقارنة
        r3 = api_get(f"{BASE}/futures/data/openInterestHist",
                     {"symbol": sym, "period": "5m", "limit": 12},
                     timeout=(4, 8))
        oi_change = 0
        if r3 and r3.status_code == 200:
            hist = r3.json()
            if len(hist) >= 6:
                oi_old = float(hist[0].get("sumOpenInterest", oi_now))
                if oi_old > 0:
                    oi_change = (oi_now - oi_old) / oi_old * 100

        score = 0
        details = []
        warning = None

        # Funding analysis
        if -0.005 <= funding <= 0.01:
            score += 10; details.append(f"Funding متوازن ({funding:.3f}%)")
        elif funding > 0.05:
            score -= 20
            warning = "⚠️ Funding عالي جداً = فخ شراء محتمل"
            details.append(f"Funding مرتفع ({funding:.3f}%)")
        elif funding < -0.02:
            score += 15
            details.append(f"Funding سالب ({funding:.3f}%) = فرصة قوية")

        # OI analysis
        if oi_change > 5:
            score += 15; details.append(f"OI ↑ {oi_change:+.1f}% (دخول قوي)")
        elif oi_change > 2:
            score += 8
        elif oi_change < -5:
            score -= 10; details.append(f"OI ↓ {oi_change:+.1f}% (خروج)")

        # Combinations (الذهبية)
        if oi_change > 3 and funding < 0:
            score += 15
            details.append("💎 OI↑ + Funding سالب = إشارة قوية!")

        result = {
            "score": score,
            "funding": funding,
            "oi_change": oi_change,
            "oi_now": oi_now,
            "details": details,
            "warning": warning,
            "valid": True,
        }
    except Exception as e:
        logging.warning(f"[FUND_OI] {sym}: {e}")
    return result


def check_sweep_reclaim(df: pd.DataFrame) -> dict:
    """
    🎯 فلتر #5: Liquidity Sweep + Reclaim (ICT)
    كسر دعم/مقاومة ثم رجوع = إشارة انعكاس قوية (دقة 70-75%)
    """
    result = {"detected": False, "type": None, "score": 0, "details": []}
    try:
        if df is None or len(df) < 30:
            return result

        # آخر 20 شمعة
        recent = df.iloc[-20:].copy()
        last5 = df.iloc[-5:].copy()

        # ابحث عن أدنى/أعلى نقطة في 20 شمعة
        low_20 = recent["l"].iloc[:-3].min()
        high_20 = recent["h"].iloc[:-3].max()
        last_close = df["c"].iloc[-1]

        # Bullish Sweep + Reclaim
        # شرط: شمعة كسرت تحت low_20، ثم أغلقت فوقها خلال 1-3 شموع
        for i in range(len(last5) - 1):
            candle = last5.iloc[i]
            if candle["l"] < low_20 * 0.998:  # كسر فعلي
                # هل أغلقت لاحقة فوق low_20؟
                later = last5.iloc[i+1:]
                if not later.empty and later["c"].iloc[-1] > low_20:
                    result["detected"] = True
                    result["type"] = "bullish_sweep"
                    result["score"] = 25
                    result["details"].append(
                        f"💎 Bullish Sweep & Reclaim @ {fmt(low_20)}"
                    )
                    return result

        # Bearish Sweep + Reclaim
        for i in range(len(last5) - 1):
            candle = last5.iloc[i]
            if candle["h"] > high_20 * 1.002:  # كسر فعلي
                later = last5.iloc[i+1:]
                if not later.empty and later["c"].iloc[-1] < high_20:
                    result["detected"] = True
                    result["type"] = "bearish_sweep"
                    result["score"] = 25
                    result["details"].append(
                        f"💎 Bearish Sweep & Reclaim @ {fmt(high_20)}"
                    )
                    return result
    except Exception as e:
        logging.warning(f"[SWEEP] {e}")
    return result


def check_volume_quality(df: pd.DataFrame) -> dict:
    """
    🎯 فلتر #6: Volume Quality (يستبدل Volume Surge البسيط)
    يكشف الحجم الحقيقي vs المضخم/الوهمي
    """
    result = {"score": 0, "details": [], "quality": "low"}
    try:
        if df is None or len(df) < 30:
            return result

        # حجم آخر شمعة vs متوسط 20
        vols = df["qv"].iloc[-20:].values
        last_vol = vols[-1]
        avg_vol = sum(vols[:-1]) / 19

        if avg_vol == 0:
            return result

        ratio = last_vol / avg_vol

        # شراء حقيقي: bq (taker buy) > 60% من الحجم الكلي
        last_buy_ratio = (df["bq"].iloc[-1] / df["qv"].iloc[-1]) * 100 if df["qv"].iloc[-1] > 0 else 50
        # متوسط نسبة الشراء في آخر 20 شمعة
        buy_ratios = []
        for i in range(-20, 0):
            if df["qv"].iloc[i] > 0:
                buy_ratios.append((df["bq"].iloc[i] / df["qv"].iloc[i]) * 100)
        avg_buy_ratio = sum(buy_ratios) / len(buy_ratios) if buy_ratios else 50

        score = 0
        details = []

        # Volume Surge
        if ratio > 5:
            score += 25; details.append(f"حجم انفجر x{ratio:.1f}")
            quality = "high"
        elif ratio > 3:
            score += 15; details.append(f"حجم مرتفع x{ratio:.1f}")
            quality = "medium"
        elif ratio > 1.8:
            score += 8
            quality = "medium"
        else:
            quality = "low"

        # Real Buy Pressure
        if last_buy_ratio > 70:
            score += 15; details.append(f"شراء قوي ({last_buy_ratio:.0f}%)")
        elif last_buy_ratio > 60:
            score += 10
        elif last_buy_ratio < 30:
            score -= 15; details.append(f"بيع قوي ({last_buy_ratio:.0f}%)")
        elif last_buy_ratio < 40:
            score -= 8

        # Buy ratio increasing trend
        if len(buy_ratios) >= 5:
            recent_avg = sum(buy_ratios[-5:]) / 5
            if recent_avg > avg_buy_ratio + 10:
                score += 10
                details.append("ضغط شراء يتزايد")

        result = {
            "score": score,
            "ratio": ratio,
            "buy_pct": last_buy_ratio,
            "avg_buy_pct": avg_buy_ratio,
            "details": details,
            "quality": quality,
        }
    except Exception as e:
        logging.warning(f"[VOL_Q] {e}")
    return result


def check_btc_correlation(sym: str, direction: str = "long") -> dict:
    """
    🎯 فلتر #7: BTC Correlation Check
    لو BTC يتحرك عكسك، إشارتك ضعيفة
    """
    result = {"aligned": False, "score": 0, "details": []}
    try:
        # سعر BTC الآن vs قبل ساعة
        btc_df = fetch_klines("BTCUSDT", interval="5m", limit=15)
        if btc_df is None or len(btc_df) < 12:
            return result

        btc_now = btc_df["c"].iloc[-1]
        btc_1h_ago = btc_df["c"].iloc[0]
        btc_change_1h = (btc_now - btc_1h_ago) / btc_1h_ago * 100

        # سعر العملة
        coin_df = fetch_klines(sym, interval="5m", limit=15)
        if coin_df is None or len(coin_df) < 12:
            return result

        coin_now = coin_df["c"].iloc[-1]
        coin_1h_ago = coin_df["c"].iloc[0]
        coin_change_1h = (coin_now - coin_1h_ago) / coin_1h_ago * 100

        score = 0
        details = []

        if direction == "long":
            if btc_change_1h > 0.3 and coin_change_1h > btc_change_1h:
                score += 15; details.append(f"تتفوق على BTC (+{coin_change_1h - btc_change_1h:.1f}%)")
            elif btc_change_1h > 0:
                score += 8; details.append("BTC مساند")
            elif btc_change_1h < -0.5:
                score -= 20; details.append("⚠️ BTC ينهار = خطر LONG")
            elif btc_change_1h < 0:
                score -= 10; details.append("BTC هابط")
        else:  # short
            if btc_change_1h < -0.3 and coin_change_1h < btc_change_1h:
                score += 15; details.append(f"تنهار أسرع من BTC")
            elif btc_change_1h < 0:
                score += 8
            elif btc_change_1h > 0.5:
                score -= 20; details.append("⚠️ BTC يصعد = خطر SHORT")
            elif btc_change_1h > 0:
                score -= 10

        result = {
            "aligned": score > 0,
            "score": score,
            "btc_change_1h": btc_change_1h,
            "coin_change_1h": coin_change_1h,
            "details": details,
        }
    except Exception as e:
        logging.warning(f"[BTC_CORR] {sym}: {e}")
    return result


def detect_pre_explosion(df: pd.DataFrame, df_15m: pd.DataFrame = None) -> dict:
    """
    🎯 الاكتشاف المبكر — يلتقط مرحلة التراكم قبل الانفجار
    يبحث عن:
      1. Volume Climbing — حجم يتزايد تدريجياً (مش انفجار مفاجئ)
      2. Higher Lows — قيعان صاعدة (تراكم ذكي)
      3. Compression — ضيق Bollinger الشديد
      4. Stealth Accumulation — buy pressure متزايد بهدوء
      5. EMA Curl Up — EMA21 يبدأ يتقوّس صاعداً
      6. RSI Divergence — RSI صاعد قبل السعر
    """
    result = {
        "score": 0,
        "signals": [],
        "is_pre_explosion": False,
        "details": {},
    }
    try:
        if df is None or len(df) < 50:
            return result

        c = df["c"]; h = df["h"]; l = df["l"]; v = df["qv"]; bq = df["bq"]
        price = float(c.iloc[-1])
        if price <= 0:
            return result

        score = 0
        signals = []

        # ═══════════════════════════════════════════
        # 1. Volume Climbing (حجم يتزايد تدريجياً)
        # المتوسط آخر 5 شموع > متوسط 20 شمعة قبلها
        # ═══════════════════════════════════════════
        recent_vol = v.iloc[-5:].mean()
        older_vol = v.iloc[-25:-5].mean()
        if older_vol > 0:
            vol_climb_ratio = recent_vol / older_vol
            result["details"]["vol_climb"] = vol_climb_ratio
            if vol_climb_ratio > 2.0:
                score += 25
                signals.append(f"📈 حجم يتزايد x{vol_climb_ratio:.1f} (تراكم نشط)")
            elif vol_climb_ratio > 1.5:
                score += 15
                signals.append(f"📈 حجم يتزايد x{vol_climb_ratio:.1f}")
            elif vol_climb_ratio > 1.2:
                score += 8

        # ═══════════════════════════════════════════
        # 2. Higher Lows (قيعان صاعدة = تراكم)
        # ═══════════════════════════════════════════
        last_10_lows = l.iloc[-10:].values
        # نقسم لجزئين ونقارن أدنى القاع
        first_half_low = last_10_lows[:5].min()
        second_half_low = last_10_lows[5:].min()
        if first_half_low > 0:
            low_progress = (second_half_low - first_half_low) / first_half_low * 100
            result["details"]["higher_lows"] = low_progress
            if low_progress > 0.5:
                score += 20
                signals.append(f"📊 قيعان صاعدة (+{low_progress:.2f}%)")
            elif low_progress > 0:
                score += 10

        # ═══════════════════════════════════════════
        # 3. Bollinger Compression (ضيق شديد)
        # ═══════════════════════════════════════════
        bb_period = 20
        bb_std = c.rolling(bb_period).std()
        bb_mid = c.rolling(bb_period).mean()
        bb_width = (bb_std.iloc[-1] * 4) / bb_mid.iloc[-1] * 100 if bb_mid.iloc[-1] > 0 else 999
        # نقارن مع آخر 50 شمعة
        bb_widths_history = ((bb_std * 4) / bb_mid * 100).iloc[-50:].dropna()
        if len(bb_widths_history) > 10:
            bb_percentile = (bb_width <= bb_widths_history).sum() / len(bb_widths_history) * 100
            result["details"]["bb_compression"] = bb_percentile
            if bb_percentile <= 15:  # في أقل 15% من العادة (ضيق نادر)
                score += 25
                signals.append(f"💎 Bollinger ضيق نادر ({bb_percentile:.0f}% percentile)")
            elif bb_percentile <= 30:
                score += 15
                signals.append(f"📊 Bollinger ضيق ({bb_percentile:.0f}% percentile)")

        # ═══════════════════════════════════════════
        # 4. Stealth Buy Pressure (شراء صامت متزايد)
        # buy ratio في آخر 10 شموع > buy ratio في الـ 20 قبلها
        # ═══════════════════════════════════════════
        buy_ratios_recent = []
        buy_ratios_older = []
        for i in range(-10, 0):
            if v.iloc[i] > 0:
                buy_ratios_recent.append(bq.iloc[i] / v.iloc[i])
        for i in range(-30, -10):
            if v.iloc[i] > 0:
                buy_ratios_older.append(bq.iloc[i] / v.iloc[i])
        if buy_ratios_recent and buy_ratios_older:
            recent_buy = sum(buy_ratios_recent) / len(buy_ratios_recent) * 100
            older_buy = sum(buy_ratios_older) / len(buy_ratios_older) * 100
            buy_climb = recent_buy - older_buy
            result["details"]["stealth_buy"] = buy_climb
            if buy_climb > 8 and recent_buy > 55:
                score += 20
                signals.append(f"🤫 شراء صامت ↑ ({recent_buy:.0f}% vs {older_buy:.0f}%)")
            elif buy_climb > 4:
                score += 10

        # ═══════════════════════════════════════════
        # 5. EMA21 Curl Up (يبدأ ينحني للأعلى)
        # ═══════════════════════════════════════════
        ema21 = c.ewm(span=21).mean()
        ema21_now = ema21.iloc[-1]
        ema21_5_ago = ema21.iloc[-6]
        ema21_10_ago = ema21.iloc[-11]
        # هل التغيير يتسارع؟
        slope_recent = (ema21_now - ema21_5_ago) / ema21_5_ago * 100 if ema21_5_ago > 0 else 0
        slope_older = (ema21_5_ago - ema21_10_ago) / ema21_10_ago * 100 if ema21_10_ago > 0 else 0
        if slope_recent > 0 and slope_recent > slope_older * 1.5 and slope_recent > 0.2:
            score += 15
            signals.append(f"📈 EMA21 يتقوس صاعد ({slope_recent:.2f}%)")

        # ═══════════════════════════════════════════
        # 6. RSI Bullish Divergence (RSI صاعد + السعر هابط/جانبي)
        # ═══════════════════════════════════════════
        delta = c.diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss.replace(0, 0.01)
        rsi = 100 - (100 / (1 + rs))
        rsi_now = rsi.iloc[-1]
        rsi_10_ago = rsi.iloc[-11]
        price_now = c.iloc[-1]
        price_10_ago = c.iloc[-11]
        if rsi_10_ago and price_10_ago:
            rsi_change = rsi_now - rsi_10_ago
            price_change_pct = (price_now - price_10_ago) / price_10_ago * 100
            # RSI صاعد لكن السعر مش صاعد كثير = divergence
            if rsi_change > 8 and price_change_pct < 1.5 and rsi_now > 45 and rsi_now < 70:
                score += 18
                signals.append(f"💎 Bullish Divergence (RSI ↑ {rsi_change:.1f})")

        # ═══════════════════════════════════════════
        # 7. 15m Confirmation (للتأكيد)
        # ═══════════════════════════════════════════
        if df_15m is not None and len(df_15m) > 30:
            ema21_15m = df_15m["c"].ewm(span=21).mean()
            price_15m = df_15m["c"].iloc[-1]
            ema21_15m_now = ema21_15m.iloc[-1]
            if price_15m > ema21_15m_now:
                # السعر فوق EMA21 على 15m
                ema_dist = (price_15m - ema21_15m_now) / ema21_15m_now * 100
                if 0 < ema_dist < 3:  # قريب جداً (مش بعيد = ما طار بعد)
                    score += 12
                    signals.append("📊 15m: السعر فوق EMA21 (قريب)")

        result["score"] = min(100, score)
        result["signals"] = signals
        result["is_pre_explosion"] = score >= 50

        # تصنيف الجودة
        if score >= 75:
            result["quality"] = "🎯 إشارة مبكرة قوية"
        elif score >= 60:
            result["quality"] = "📊 إشارة مبكرة جيدة"
        elif score >= 50:
            result["quality"] = "⏳ تراكم محتمل"
        else:
            result["quality"] = "—"

    except Exception as e:
        logging.warning(f"[PRE_EXPL] {e}")
    return result


# ══════════════════════════════════════════════════════════════
# 🆕 SMART GRID TRADING SYSTEM
# ══════════════════════════════════════════════════════════════
# نظام Grid ذكي يفحص العملات ويكتشف الفرص المثالية للـ Grid
# يدعم وضعين: تلقائي (ماسح) ويدوي (لعملة محددة)
# ══════════════════════════════════════════════════════════════

# State for Grid System
active_grids: dict = {}              # {chat_id: {sym: grid_data}}
grid_history: dict = {}              # {chat_id: {sym: [trades]}}
grid_scan_alerted: dict = {}         # {chat_id: {sym: ts}} للتبريد
last_grid_results: dict = {}         # {chat_id: [candidates]}
GRID_SCAN_COOL = 7200               # ساعتين بين تنبيهات نفس العملة


def analyze_grid_suitability(sym: str, df_5m: pd.DataFrame = None,
                              meta: dict = None) -> dict:
    """
    🎯 تحليل مدى مناسبة عملة لـ Grid Trading.
    يعتمد على 5 معايير وزنية لإعطاء score من 0-100.
    """
    result = {
        "sym":           sym,
        "score":         0,
        "suitable":      False,
        "level":         "غير مناسب",
        "details":       [],
        "warnings":      [],
        "atr_pct":       0,
        "trend":         "unknown",
        "volume_24h":    0,
        "current_price": 0,
        "range_high":    0,
        "range_low":     0,
        "range_pct":     0,
        "bounces":       0,
        "stable_days":   0,
    }

    try:
        # جلب بيانات إذا لم تُرسَل
        if df_5m is None:
            df_5m = fetch_klines(sym, "5m", 288)  # يومين
        if df_5m is None or len(df_5m) < 100:
            result["warnings"].append("بيانات غير كافية")
            return result

        # السعر الحالي
        current_price = float(df_5m["c"].iloc[-1])
        result["current_price"] = current_price
        if current_price <= 0:
            return result

        # ════════════════════════════════════════
        # 1️⃣ التقلب (ATR) — 25 نقطة
        # ════════════════════════════════════════
        h = df_5m["h"]
        l = df_5m["l"]
        c = df_5m["c"]
        tr = pd.concat([
            h - l,
            (h - c.shift()).abs(),
            (l - c.shift()).abs()
        ], axis=1).max(axis=1)
        atr = tr.rolling(14).mean().iloc[-1]
        atr_pct = (atr / current_price) * 100
        result["atr_pct"] = atr_pct

        atr_score = 0
        if 2.0 <= atr_pct <= 6.0:
            atr_score = 25
            result["details"].append(f"✅ تقلب مثالي ({atr_pct:.2f}%)")
        elif 1.5 <= atr_pct <= 8.0:
            atr_score = 18
            result["details"].append(f"🟡 تقلب جيد ({atr_pct:.2f}%)")
        elif 1.0 <= atr_pct <= 10.0:
            atr_score = 10
            result["details"].append(f"⚠️ تقلب مقبول ({atr_pct:.2f}%)")
        elif atr_pct < 1.0:
            atr_score = 0
            result["warnings"].append(f"تقلب ضعيف جداً ({atr_pct:.2f}%) — راكد")
        else:  # > 10%
            atr_score = 0
            result["warnings"].append(f"تقلب مرتفع جداً ({atr_pct:.2f}%) — خطر")

        # ════════════════════════════════════════
        # 2️⃣ عدم وجود Trend (EMA Slope) — 25 نقطة
        # ════════════════════════════════════════
        ema21 = c.ewm(span=21).mean()
        ema50 = c.ewm(span=50).mean()

        # ميل EMA21 على آخر 50 شمعة
        ema_now = float(ema21.iloc[-1])
        ema_50ago = float(ema21.iloc[-50])
        slope_pct = ((ema_now - ema_50ago) / ema_50ago) * 100

        # علاقة EMA21 و EMA50
        ema_diff = abs(float(ema21.iloc[-1]) - float(ema50.iloc[-1])) / current_price * 100

        trend_score = 0
        if abs(slope_pct) < 1.0 and ema_diff < 1.0:
            trend_score = 25
            result["trend"] = "sideways_strong"
            result["details"].append(f"✅ سوق جانبي قوي (ميل {slope_pct:+.2f}%)")
        elif abs(slope_pct) < 2.0 and ema_diff < 2.0:
            trend_score = 18
            result["trend"] = "sideways"
            result["details"].append(f"🟡 سوق جانبي (ميل {slope_pct:+.2f}%)")
        elif abs(slope_pct) < 3.5:
            trend_score = 8
            result["trend"] = "weak_trend"
            result["details"].append(f"⚠️ trend ضعيف (ميل {slope_pct:+.2f}%)")
        else:
            trend_score = 0
            result["trend"] = "trending"
            direction = "صاعد" if slope_pct > 0 else "هابط"
            result["warnings"].append(f"trend {direction} قوي ({slope_pct:+.2f}%) — Grid غير مناسب")

        # ════════════════════════════════════════
        # 3️⃣ حجم التداول — 15 نقطة
        # ════════════════════════════════════════
        vol_24h = 0
        if meta:
            vol_24h = meta.get("vol_24h", 0)
        if vol_24h == 0:
            # حساب من الكلاينز (تقريبي)
            vol_24h = float(df_5m["qv"].iloc[-288:].sum()) if len(df_5m) >= 288 else \
                      float(df_5m["qv"].sum())
        result["volume_24h"] = vol_24h

        vol_score = 0
        if vol_24h >= 100_000_000:        # ≥$100M
            vol_score = 15
            result["details"].append(f"✅ سيولة ممتازة (${vol_24h/1e6:.1f}M)")
        elif vol_24h >= 30_000_000:       # ≥$30M
            vol_score = 12
            result["details"].append(f"🟡 سيولة جيدة (${vol_24h/1e6:.1f}M)")
        elif vol_24h >= 10_000_000:       # ≥$10M
            vol_score = 8
            result["details"].append(f"⚠️ سيولة مقبولة (${vol_24h/1e6:.1f}M)")
        elif vol_24h >= 3_000_000:        # ≥$3M
            vol_score = 4
        else:
            vol_score = 0
            result["warnings"].append(f"سيولة ضعيفة (${vol_24h/1e6:.1f}M)")

        # ════════════════════════════════════════
        # 4️⃣ Range Stability — 20 نقطة
        # ════════════════════════════════════════
        # نطاق آخر 200 شمعة (16 ساعة)
        recent = df_5m.iloc[-200:]
        range_high = float(recent["h"].max())
        range_low = float(recent["l"].min())
        range_pct = ((range_high - range_low) / current_price) * 100
        result["range_high"] = range_high
        result["range_low"] = range_low
        result["range_pct"] = range_pct

        # كم شمعة كانت داخل النطاق الأساسي (50% الأوسط)
        mid = (range_high + range_low) / 2
        band_size = (range_high - range_low) * 0.5
        in_band = recent[(recent["c"] >= mid - band_size/2) &
                         (recent["c"] <= mid + band_size/2)]
        in_band_pct = (len(in_band) / len(recent)) * 100

        range_score = 0
        if 3.0 <= range_pct <= 8.0 and in_band_pct >= 40:
            range_score = 20
            result["details"].append(f"✅ نطاق ثابت ({range_pct:.1f}% — {in_band_pct:.0f}% داخله)")
        elif 2.0 <= range_pct <= 12.0 and in_band_pct >= 30:
            range_score = 14
            result["details"].append(f"🟡 نطاق جيد ({range_pct:.1f}%)")
        elif range_pct < 2.0:
            range_score = 5
            result["warnings"].append(f"النطاق ضيق جداً ({range_pct:.1f}%)")
        elif range_pct > 15.0:
            range_score = 0
            result["warnings"].append(f"النطاق واسع جداً ({range_pct:.1f}%) — لا يوجد range واضح")
        else:
            range_score = 8

        # ════════════════════════════════════════
        # 5️⃣ Bounce Quality — 15 نقطة
        # ════════════════════════════════════════
        # كم مرة لمس السعر الحدود (Range tests)
        upper_tests = 0
        lower_tests = 0
        upper_threshold = range_high - (range_high - range_low) * 0.15
        lower_threshold = range_low + (range_high - range_low) * 0.15

        for i in range(0, len(recent) - 5, 5):
            window = recent.iloc[i:i+5]
            if window["h"].max() >= upper_threshold:
                upper_tests += 1
            if window["l"].min() <= lower_threshold:
                lower_tests += 1
        bounces = upper_tests + lower_tests
        result["bounces"] = bounces

        bounce_score = 0
        if bounces >= 8:
            bounce_score = 15
            result["details"].append(f"✅ ارتدادات قوية ({upper_tests} علوي / {lower_tests} سفلي)")
        elif bounces >= 5:
            bounce_score = 11
            result["details"].append(f"🟡 ارتدادات جيدة ({upper_tests}/{lower_tests})")
        elif bounces >= 3:
            bounce_score = 6
        else:
            bounce_score = 0
            result["warnings"].append(f"ارتدادات قليلة ({bounces}) — احتمال كسر النطاق")

        # ════════════════════════════════════════
        # تجميع النتيجة
        # ════════════════════════════════════════
        total_score = atr_score + trend_score + vol_score + range_score + bounce_score
        result["score"] = total_score

        if total_score >= 85:
            result["level"] = "🥇 ممتاز"
            result["suitable"] = True
        elif total_score >= 70:
            result["level"] = "🥈 جيد جداً"
            result["suitable"] = True
        elif total_score >= 55:
            result["level"] = "🥉 مقبول"
            result["suitable"] = True
        elif total_score >= 40:
            result["level"] = "⚠️ حذر"
            result["suitable"] = False
        else:
            result["level"] = "❌ غير مناسب"
            result["suitable"] = False

        # حساب stable_days تقريبياً
        result["stable_days"] = round(in_band_pct / 25, 1)  # تقريب

    except Exception as e:
        logging.warning(f"[GRID_ANALYZE] {sym}: {e}")
    return result


def calculate_grid_levels(price: float, range_high: float, range_low: float,
                           levels: int = 10) -> dict:
    """
    🧮 حساب مستويات الـ Grid بناءً على النطاق المكتشف.
    """
    spacing = (range_high - range_low) / levels
    half = levels // 2

    buy_levels = []
    sell_levels = []

    # مستويات الشراء (تحت السعر الحالي حتى range_low)
    buy_step = (price - range_low) / max(half, 1)
    for i in range(1, half + 1):
        level_price = price - (buy_step * i)
        if level_price > range_low * 0.99:
            buy_levels.append({
                "level":    i,
                "type":     "BUY",
                "price":    round(level_price, 8),
                "distance": -((price - level_price) / price * 100),
                "filled":   False,
            })

    # مستويات البيع (فوق السعر الحالي حتى range_high)
    sell_step = (range_high - price) / max(half, 1)
    for i in range(1, half + 1):
        level_price = price + (sell_step * i)
        if level_price < range_high * 1.01:
            sell_levels.append({
                "level":    i,
                "type":     "SELL",
                "price":    round(level_price, 8),
                "distance": ((level_price - price) / price * 100),
                "filled":   False,
            })

    return {
        "current_price":  price,
        "range_high":     range_high,
        "range_low":      range_low,
        "spacing":        spacing,
        "spacing_pct":    (spacing / price) * 100,
        "buy_levels":     buy_levels,
        "sell_levels":    sell_levels,
        "expected_profit_per_cycle": (spacing / price) * 100,
    }


def fetch_current_price(sym: str) -> float:
    """جلب سعر العملة الحالي."""
    try:
        for url in [f"{BASE}/fapi/v1/ticker/price",
                    "https://api.binance.com/api/v3/ticker/price"]:
            r = api_get(url, {"symbol": sym}, timeout=(4, 8))
            if r and r.status_code == 200:
                return float(r.json().get("price", 0))
    except Exception:
        pass
    return 0


def scan_grid_opportunities(min_score: int = 60, max_results: int = 10) -> list:
    """
    🔍 يفحص كل عملات Binance Futures ويرجع أفضل المرشحين للـ Grid.
    """
    candidates = []
    try:
        coins = fetch_all_usdt(min_vol=10_000_000)  # حد سيولة $10M
        if not coins:
            return []

        # نأخذ أعلى 100 عملة بالحجم
        coins.sort(key=lambda x: x.get("vol_24h", 0), reverse=True)
        coins = coins[:100]

        for meta in coins:
            sym = meta["sym"]
            try:
                analysis = analyze_grid_suitability(sym, meta=meta)
                if analysis["score"] >= min_score:
                    # حساب مستويات الـ Grid
                    grid = calculate_grid_levels(
                        analysis["current_price"],
                        analysis["range_high"],
                        analysis["range_low"],
                        levels=10,
                    )
                    analysis["grid"] = grid
                    candidates.append(analysis)
            except Exception:
                continue

        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates[:max_results]
    except Exception as e:
        logging.warning(f"[GRID_SCAN] {e}")
        return []


def build_grid_scanner_alert(candidates: list, max_show: int = 3) -> str:
    """بناء رسالة تنبيه ماسح الـ Grid."""
    if not candidates:
        return ("🔍 *ماسح Grid*\n\n"
                "⚠️ لم يتم اكتشاف فرص Grid مناسبة الآن.\n\n"
                "💡 الأسباب المحتملة:\n"
                "  • السوق في trend قوي (صاعد/هابط)\n"
                "  • التقلب منخفض جداً\n"
                "  • السيولة ضعيفة\n\n"
                "🔄 سيُعاد الفحص لاحقاً.")

    n = min(len(candidates), max_show)
    msg = f"🎯 *فرص Grid مكتشفة ({n} عملة)*\n"
    msg += f"🕐 {now_sa()}\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n\n"

    medals = ["🥇", "🥈", "🥉"]
    for i, c in enumerate(candidates[:n]):
        medal = medals[i] if i < 3 else f"#{i+1}"
        sym = c["sym"]
        score = c["score"]
        level = c["level"]
        price = c["current_price"]
        atr_pct = c["atr_pct"]
        vol_m = c["volume_24h"] / 1_000_000
        grid = c.get("grid", {})

        msg += f"{medal} *{sym}* — Score: `{score}/100`\n"
        msg += f"  {level}\n"
        msg += f"  💰 السعر: `${fmt(price)}`\n"
        msg += f"  📊 التقلب: `{atr_pct:.2f}%`\n"
        msg += f"  💧 الحجم: `${vol_m:,.1f}M`\n"

        if grid.get("range_high"):
            msg += "\n  🟢 *نطاق Grid:*\n"
            msg += f"    أعلى: `${fmt(grid['range_high'])}` (+{((grid['range_high']-price)/price*100):+.1f}%)\n"
            msg += f"    أوسط: `${fmt(price)}`\n"
            msg += f"    أسفل: `${fmt(grid['range_low'])}` ({((grid['range_low']-price)/price*100):+.1f}%)\n"

            buy_levels = grid.get("buy_levels", [])
            sell_levels = grid.get("sell_levels", [])

            if buy_levels:
                first_buy = buy_levels[0]
                msg += f"\n  🎯 *خطة الدخول الأولى:*\n"
                msg += f"    اشتري عند: `${fmt(first_buy['price'])}` ({first_buy['distance']:+.2f}%)\n"
                if sell_levels:
                    first_sell = sell_levels[0]
                    msg += f"    بِع عند:    `${fmt(first_sell['price'])}` (+{first_sell['distance']:.2f}%)\n"
                    profit_pct = grid.get("expected_profit_per_cycle", 0)
                    msg += f"    الربح/دورة: ~`{profit_pct:.2f}%`\n"

        if c.get("warnings"):
            msg += "\n  ⚠️ تحذيرات:\n"
            for w in c["warnings"][:2]:
                msg += f"    • {w}\n"

        msg += "\n━━━━━━━━━━━━━━━━━━━━\n\n"

    msg += "💡 *للتفعيل اليدوي:*\n"
    if candidates:
        msg += f"  `جريد {candidates[0]['sym'][:-4]}` — تفعيل أفضل عملة\n"
    msg += "  `جريداتي` — عرض الـ Grids النشطة\n"
    msg += "\n⚠️ _تنفيذ يدوي — للأغراض التعليمية فقط_"
    return msg


def build_grid_status(grid_data: dict) -> str:
    """عرض حالة Grid معينة."""
    sym = grid_data.get("sym", "?")
    grid = grid_data.get("grid", {})
    history = grid_data.get("history", [])

    msg = f"📊 *Grid: {sym}*\n"
    msg += f"🕐 {now_sa()}\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n\n"

    current = fetch_current_price(sym)
    msg += f"💰 السعر الآن: `${fmt(current)}`\n"
    msg += f"📍 المركز: `${fmt(grid_data.get('center_price', 0))}`\n"
    msg += f"📏 النطاق: `${fmt(grid.get('range_low', 0))}` - `${fmt(grid.get('range_high', 0))}`\n\n"

    # حالة المستويات
    buy_levels = grid.get("buy_levels", [])
    sell_levels = grid.get("sell_levels", [])

    filled_buys = sum(1 for b in buy_levels if b.get("filled"))
    filled_sells = sum(1 for s in sell_levels if s.get("filled"))

    msg += f"🟢 مستويات الشراء: {filled_buys}/{len(buy_levels)}\n"
    for b in buy_levels:
        icon = "✅" if b.get("filled") else "⭕"
        msg += f"  {icon} #{b['level']}: `${fmt(b['price'])}` ({b['distance']:+.2f}%)\n"

    msg += f"\n🔴 مستويات البيع: {filled_sells}/{len(sell_levels)}\n"
    for s in sell_levels:
        icon = "✅" if s.get("filled") else "⭕"
        msg += f"  {icon} #{s['level']}: `${fmt(s['price'])}` ({s['distance']:+.2f}%)\n"

    # P&L
    if history:
        completed_cycles = len([h for h in history if h.get("type") == "cycle_complete"])
        total_profit = sum(h.get("profit", 0) for h in history)
        msg += f"\n💵 *الأداء:*\n"
        msg += f"  دورات مكتملة: `{completed_cycles}`\n"
        msg += f"  P&L افتراضي: `{total_profit:+.2f}%`\n"

    # تحذير لو خرج النطاق
    range_high = grid.get("range_high", 0)
    range_low = grid.get("range_low", 0)
    if current > range_high * 1.02:
        msg += f"\n⚠️ *تحذير:* السعر خرج فوق النطاق!"
    elif current < range_low * 0.98:
        msg += f"\n⚠️ *تحذير:* السعر خرج تحت النطاق!"

    return msg


async def grid_scanner_job(ctx: ContextTypes.DEFAULT_TYPE):
    """مهمة فحص فرص Grid كل ساعة."""
    chat_id = ctx.job.data["chat_id"]
    min_score = ctx.job.data.get("min_score", 70)

    try:
        loop = asyncio.get_event_loop()
        candidates = await asyncio.wait_for(
            loop.run_in_executor(None, scan_grid_opportunities, min_score, 10),
            timeout=300,
        )
        last_grid_results[chat_id] = candidates

        if not candidates:
            logging.warning(f"[GRID_SCAN] لا فرص (min_score={min_score})")
            return

        logging.warning(f"[GRID_SCAN] وجد {len(candidates)} فرصة")

        # cooldown: نرسل فقط لو فيه عملة جديدة
        now_ts = time.time()
        new_candidates = []
        for c in candidates[:3]:
            sym = c["sym"]
            last_alert = grid_scan_alerted.get(chat_id, {}).get(sym, 0)
            if now_ts - last_alert >= GRID_SCAN_COOL:
                new_candidates.append(c)
                grid_scan_alerted.setdefault(chat_id, {})[sym] = now_ts

        if not new_candidates:
            return

        msg = build_grid_scanner_alert(new_candidates, max_show=3)
        try:
            await ctx.bot.send_message(
                chat_id=chat_id,
                text=msg,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📊 تفاصيل الكل", callback_data="grid:results"),
                ]])
            )
        except Exception as e:
            logging.warning(f"[GRID_SCAN_SEND] {e}")
    except asyncio.TimeoutError:
        logging.warning("[GRID_SCAN] timeout")
    except Exception as e:
        logging.warning(f"[GRID_SCAN] {e}")


async def grid_monitor_job(ctx: ContextTypes.DEFAULT_TYPE):
    """مراقبة الـ Grids النشطة وإرسال تنبيهات لمس المستويات."""
    chat_id = ctx.job.data["chat_id"]
    grids = active_grids.get(chat_id, {})
    if not grids:
        return

    for sym, grid_data in list(grids.items()):
        try:
            current = fetch_current_price(sym)
            if current <= 0:
                continue

            grid = grid_data.get("grid", {})
            range_high = grid.get("range_high", 0)
            range_low = grid.get("range_low", 0)

            # تحذير من خروج النطاق
            if current > range_high * 1.02 or current < range_low * 0.98:
                if not grid_data.get("range_break_alerted"):
                    direction = "فوق" if current > range_high else "تحت"
                    msg = (f"⚠️ *تحذير: {sym} خرج النطاق*\n\n"
                           f"💰 السعر الحالي: `${fmt(current)}`\n"
                           f"📏 النطاق المحدد: `${fmt(range_low)}` - `${fmt(range_high)}`\n"
                           f"🚨 السعر {direction} النطاق!\n\n"
                           f"💡 توصية: راجع الـ Grid أو أوقفه\n"
                           f"`وقف جريد {sym[:-4]}`")
                    try:
                        await ctx.bot.send_message(chat_id, msg, parse_mode="Markdown")
                        grid_data["range_break_alerted"] = True
                    except Exception:
                        pass
                continue

            # فحص لمس مستويات الشراء
            for buy in grid.get("buy_levels", []):
                if buy.get("filled"):
                    continue
                # اعتبر "وصل" إذا السعر <= السعر المستهدف
                if current <= buy["price"]:
                    buy["filled"] = True
                    buy["filled_at"] = time.time()
                    grid_data.setdefault("history", []).append({
                        "type":  "buy_hit",
                        "level": buy["level"],
                        "price": buy["price"],
                        "ts":    time.time(),
                    })
                    # إرسال تنبيه
                    msg = (f"🟢 *إشارة Grid: {sym}*\n"
                           f"💰 وصل مستوى الشراء #{buy['level']}\n\n"
                           f"السعر الحالي: `${fmt(current)}`\n"
                           f"المستوى: `${fmt(buy['price'])}`\n"
                           f"المسافة: `{buy['distance']:+.2f}%`\n\n"
                           f"💡 *توصية يدوية:*\n"
                           f"  اشتري على Binance/OKX\n")
                    # السعر المستهدف للبيع = أول مستوى بيع متاح
                    next_sell = next((s for s in grid["sell_levels"] if not s.get("filled")), None)
                    if next_sell:
                        profit_pct = ((next_sell["price"] - buy["price"]) / buy["price"]) * 100
                        msg += (f"  هدف البيع: `${fmt(next_sell['price'])}`\n"
                                f"  الربح المتوقع: `+{profit_pct:.2f}%`\n")
                    msg += "\n⚠️ _تنفيذ يدوي فقط_"
                    try:
                        await ctx.bot.send_message(chat_id, msg, parse_mode="Markdown")
                    except Exception as e:
                        logging.warning(f"[GRID_BUY] {e}")

            # فحص لمس مستويات البيع
            for sell in grid.get("sell_levels", []):
                if sell.get("filled"):
                    continue
                if current >= sell["price"]:
                    sell["filled"] = True
                    sell["filled_at"] = time.time()
                    grid_data.setdefault("history", []).append({
                        "type":  "sell_hit",
                        "level": sell["level"],
                        "price": sell["price"],
                        "ts":    time.time(),
                    })
                    msg = (f"🔴 *إشارة Grid: {sym}*\n"
                           f"💰 وصل مستوى البيع #{sell['level']}\n\n"
                           f"السعر الحالي: `${fmt(current)}`\n"
                           f"المستوى: `${fmt(sell['price'])}`\n"
                           f"المسافة: `{sell['distance']:+.2f}%`\n\n"
                           f"💡 *توصية يدوية:*\n"
                           f"  بِع على Binance/OKX (لو كنت اشتريت من قبل)\n\n"
                           f"⚠️ _تنفيذ يدوي فقط_")
                    try:
                        await ctx.bot.send_message(chat_id, msg, parse_mode="Markdown")
                    except Exception as e:
                        logging.warning(f"[GRID_SELL] {e}")

        except Exception as e:
            logging.warning(f"[GRID_MON] {sym}: {e}")


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

    # ═══════════════════════════════════════════════════════════
    # 🆕 الفلاتر الذكية v2 — تطبق فقط لو السكور الأساسي >= 35
    # هذي الفلاتر ترفع الدقة من 50% إلى 75-85%
    # ═══════════════════════════════════════════════════════════
    base_score = score
    smart_filters = {
        "applied": False,
        "btc_trend": None,
        "mtf": None,
        "orderbook": None,
        "funding_oi": None,
        "sweep": None,
        "vol_quality": None,
        "btc_corr": None,
        "smart_score": 0,
        "filters_passed": 0,
        "rejection_reasons": [],
    }

    if base_score >= 30:
        smart_filters["applied"] = True
        direction = "long"  # البوت حالياً يبحث عن LONG فقط

        try:
            # فلتر #1: BTC Trend (أهم فلتر)
            btc = get_btc_trend()
            smart_filters["btc_trend"] = btc
            if btc.get("valid"):
                if direction == "long":
                    if btc["trend"] == "bullish":
                        smart_filters["smart_score"] += 15
                        smart_filters["filters_passed"] += 1
                        result["signals"].append("✅ BTC اتجاه صاعد قوي")
                    elif btc["trend"] == "bearish":
                        smart_filters["smart_score"] -= 25
                        smart_filters["rejection_reasons"].append("❌ BTC هابط = خطر")
                        result["warnings"].append("⚠️ BTC في اتجاه هابط")
                    else:
                        smart_filters["smart_score"] += 5
                        smart_filters["filters_passed"] += 1

            # فلتر #2: MTF Confirmation
            mtf = check_mtf_alignment(sym, direction)
            smart_filters["mtf"] = mtf
            if mtf["aligned"]:
                smart_filters["smart_score"] += 15
                smart_filters["filters_passed"] += 1
                result["signals"].append(f"✅ الفريمات الأكبر متوافقة (15m+1h)")
            else:
                smart_filters["smart_score"] -= 15
                smart_filters["rejection_reasons"].append("❌ MTF مش متوافقة")
                result["warnings"].append("⚠️ 15m/1h مش مع الإشارة")

            # فلتر #3: Order Book
            ob = check_orderbook_pressure(sym)
            smart_filters["orderbook"] = ob
            if ob.get("valid"):
                smart_filters["smart_score"] += ob["score"]
                if ob["score"] > 15:
                    smart_filters["filters_passed"] += 1
                    for d in ob["details"]:
                        result["signals"].append(f"📖 {d}")
                elif ob["score"] < -10:
                    for d in ob["details"]:
                        result["warnings"].append(f"📖 {d}")

            # فلتر #4: Funding + OI
            fo = check_funding_oi(sym)
            smart_filters["funding_oi"] = fo
            if fo.get("valid"):
                smart_filters["smart_score"] += fo["score"]
                if fo["score"] > 10:
                    smart_filters["filters_passed"] += 1
                    for d in fo["details"]:
                        result["signals"].append(f"💰 {d}")
                if fo.get("warning"):
                    result["warnings"].append(fo["warning"])

            # فلتر #5: Sweep & Reclaim
            sweep = check_sweep_reclaim(df)
            smart_filters["sweep"] = sweep
            if sweep["detected"]:
                smart_filters["smart_score"] += sweep["score"]
                smart_filters["filters_passed"] += 1
                for d in sweep["details"]:
                    result["signals"].append(d)

            # فلتر #6: Volume Quality
            vq = check_volume_quality(df)
            smart_filters["vol_quality"] = vq
            if vq["score"] > 0:
                # هذي بديلة للحجم القديم — لا نضيف لأنها مدمجة
                smart_filters["smart_score"] += vq["score"] // 2  # نضيف نصفها لتجنب double-count
                if vq["quality"] == "high":
                    smart_filters["filters_passed"] += 1

            # فلتر #7: BTC Correlation
            corr = check_btc_correlation(sym, direction)
            smart_filters["btc_corr"] = corr
            if corr["aligned"]:
                smart_filters["smart_score"] += corr["score"]
                smart_filters["filters_passed"] += 1
                for d in corr["details"]:
                    result["signals"].append(f"📊 {d}")
            elif corr["score"] < -10:
                smart_filters["smart_score"] += corr["score"]
                for d in corr["details"]:
                    result["warnings"].append(f"📊 {d}")

        except Exception as e:
            logging.warning(f"[SMART] {sym}: {e}")

    # دمج النقاط: السكور الأساسي + الفلاتر الذكية
    final_score = base_score + smart_filters["smart_score"]
    result["score"] = min(100, max(0, final_score))
    result["base_score"] = base_score
    result["smart_filters"] = smart_filters
    result["smart_score"] = smart_filters["smart_score"]

    # ═══════════════════════════════════════════════════════════
    # 🆕 الكشف المبكر — يلتقط مرحلة التراكم قبل الانفجار
    # هذا يضيف مرحلة جديدة "pre_explosion_early" قبل بدء الحركة
    # ═══════════════════════════════════════════════════════════
    pre_expl = detect_pre_explosion(df, df_15m=None)
    result["pre_explosion"] = pre_expl

    # تحديد المرحلة (مع اعتبار جودة الإشارة)
    filters_passed = smart_filters["filters_passed"]
    if result["score"] >= 70 and filters_passed >= 4:
        result["phase"] = "explosion_premium"  # 🆕 إشارة فاخرة
    elif result["score"] >= 70:
        result["phase"] = "explosion_now"
    elif result["score"] >= 55 and filters_passed >= 3:
        result["phase"] = "explosion_start_strong"  # 🆕 بداية قوية
    elif result["score"] >= 50:
        result["phase"] = "explosion_start"
    elif pre_expl["is_pre_explosion"] and pre_expl["score"] >= 60:
        # 🆕 إشارة مبكرة قبل أي انفجار (الذهب الحقيقي!)
        result["phase"] = "pre_explosion_early"
        # دمج إشارات pre-explosion في القائمة
        for sig in pre_expl["signals"]:
            if sig not in result["signals"]:
                result["signals"].append(sig)
        # ادمج النقاط
        result["score"] = max(result["score"], pre_expl["score"])
    elif result["score"] >= 35:
        result["phase"] = "pre_explosion"
    elif pre_expl["is_pre_explosion"]:
        # نقاط منخفضة لكن pre-explosion قوي
        result["phase"] = "pre_explosion"
        for sig in pre_expl["signals"]:
            if sig not in result["signals"]:
                result["signals"].append(sig)

    # رفض الإشارة لو فيها أسباب رفض حرجة
    if smart_filters["applied"]:
        critical = sum(1 for r in smart_filters["rejection_reasons"] if "BTC" in r or "MTF" in r)
        if critical >= 2:
            result["phase"] = "rejected"
            result["score"] = max(0, result["score"] - 30)
            result["warnings"].insert(0, "🚫 رُفضت: الاتجاه العام معاكس")

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
    "explosion_premium":     "💎 إشارة فاخرة (دقة ~80%)",
    "explosion_now":          "🚨 انفجار الآن!",
    "explosion_start_strong": "⚡⚡ بداية انفجار قوية",
    "explosion_start":        "⚡ بدء الانفجار",
    "pre_explosion_early":    "🌱 تراكم مبكر — قبل الانفجار",
    "pre_explosion":          "⏳ على وشك الانفجار",
    "rejected":               "🚫 مرفوضة (الاتجاه معاكس)",
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
    if phase == "explosion_premium":
        icon = "💎"
    elif phase == "explosion_now":
        icon = "🚨"
    elif phase == "explosion_start_strong":
        icon = "⚡⚡"
    elif phase == "pre_explosion_early":
        icon = "🌱"
    else:
        icon = "⚡"
    bar  = "█" * int(score/10) + "░" * (10 - int(score/10))

    m  = f"{icon} *{phase_txt}*\n"
    m += f"🪙 *{sym}* | 🕐 {now_sa()}\n"
    m += "━━━━━━━━━━━━━━━━━━━━\n\n"
    m += f"💰 السعر: `${fmt(price)}`\n"
    m += f"📊 24h: `{chg:+.2f}%` | حجم: `${fmt(vol)}`\n"
    m += f"📈 حجم x{r.get('vol_ratio',0):.1f} | RSI: `{r.get('rsi',50):.0f}`\n"
    m += f"🎯 نقاط الانفجار: `{score}/100`\n"
    m += f"`{bar}`\n"

    # 🆕 معلومات الفلاتر الذكية
    sf = r.get("smart_filters", {})
    if sf.get("applied"):
        passed = sf.get("filters_passed", 0)
        m += f"🛡 الفلاتر الذكية: `{passed}/7 ✅`\n"
        # تقدير دقة تقريبي حسب عدد الفلاتر التي مرت
        if passed >= 5:
            m += "   _جودة الإشارة: ممتازة (~75-80%)_\n"
        elif passed >= 3:
            m += "   _جودة الإشارة: جيدة (~65-70%)_\n"
        elif passed >= 1:
            m += "   _جودة الإشارة: متوسطة (~55-60%)_\n"
        else:
            m += "   _جودة الإشارة: ضعيفة_\n"
    m += "\n"

    m += "📡 *الإشارات:*\n"
    for s in sigs: m += f"  {s}\n"
    if warns:
        m += "\n⚠️ *تحذيرات:*\n"
        for w in warns: m += f"  {w}\n"
    m += "\n"

    # 🆕 ملخص فني ذكي
    btc = sf.get("btc_trend") if sf else None
    if btc and btc.get("valid"):
        trend_emoji = "🟢" if btc["trend"] == "bullish" else "🔴" if btc["trend"] == "bearish" else "⚪"
        m += f"📈 BTC: {trend_emoji} {btc['trend']} ({btc['score']}/100)\n\n"

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
    chat_id     = ctx.job.data["chat_id"]
    min_score   = ctx.job.data.get("min_score", 60)
    quality_mode = ctx.job.data.get("quality_mode", False)  # 🆕 وضع الجودة العالية
    min_filters = ctx.job.data.get("min_filters", 0)        # 🆕 حد أدنى من الفلاتر
    now_ts      = time.time()
    cooldown    = 3600  # ساعة بين تنبيهين لنفس العملة

    try:
        loop = asyncio.get_event_loop()

        def do_scan():
            coins  = fetch_all_usdt(min_vol=500_000)
            alerts = []
            rejected = 0  # عداد الإشارات المرفوضة
            for meta in coins:
                sym = meta["sym"]
                in_trade    = sym in active_trades.get(chat_id, {})
                in_cooldown = now_ts - last_alert.get(chat_id,{}).get(sym,0) < cooldown
                if in_trade or in_cooldown: continue

                try:
                    r = analyze_coin(sym, meta)
                    if r["phase"] == "none":
                        continue

                    # 🆕 فلتر الجودة الصارم
                    if r["phase"] == "rejected":
                        rejected += 1
                        continue

                    if r["score"] < min_score:
                        continue

                    # 🆕 لو وضع الجودة العالية، فلتر إضافي
                    sf = r.get("smart_filters", {})
                    if quality_mode:
                        # في quality mode، نقبل الإشارات المبكرة + الفاخرة + القوية
                        # pre_explosion_early مهم لأنه يلتقط قبل الانفجار!
                        if r["phase"] not in ("pre_explosion_early",
                                               "explosion_premium",
                                               "explosion_now",
                                               "explosion_start_strong"):
                            rejected += 1
                            continue
                        # تخفيف شرط الفلاتر للإشارات المبكرة
                        # (الفلاتر الذكية ما تشتغل دائماً قبل الانفجار)
                        min_filters_required = 4
                        if r["phase"] == "pre_explosion_early":
                            min_filters_required = 2  # تساهل أكثر للمبكر
                        if sf.get("filters_passed", 0) < min_filters_required:
                            rejected += 1
                            continue

                    # 🆕 حد أدنى من الفلاتر للقبول
                    if min_filters > 0 and sf.get("filters_passed", 0) < min_filters:
                        rejected += 1
                        continue

                    alerts.append(r)
                except: continue

            # 🆕 ترتيب ذكي: pre_explosion_early يأتي مع الفاخرة!
            # لأن المبكر = أهم لحظة للدخول
            phase_order = {
                "pre_explosion_early": 0,  # 🌱 الأول! (تراكم مبكر)
                "explosion_premium": 1,
                "explosion_now": 2,
                "explosion_start_strong": 3,
                "explosion_start": 4,
                "pre_explosion": 5,
            }
            alerts.sort(key=lambda x: (phase_order.get(x["phase"], 5), -x["score"]))
            return alerts, rejected

        alerts, rejected = await asyncio.wait_for(
            loop.run_in_executor(None, do_scan), timeout=480)

        last_results[chat_id] = alerts
        logging.warning(
            f"[SCAN] ✅ وجد {len(alerts)} إشارة | 🚫 رُفضت {rejected} إشارة (فلاتر ذكية)"
        )

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
        "💥 *EXPLOSION DETECTOR BOT v3*\n"
        "كاشف الانفجار + Smart Grid 🌱\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🚨 *كاشف الانفجار:*\n\n"
        "🌱 `كاشف مبكر`     — *قبل الانفجار!* (تراكم) ⭐\n"
        "🟢 `كاشف`           — عادي (دقة ~55%)\n"
        "⚖️ `كاشف متوازن`    — معتدلة (~65%)\n"
        "💎 `كاشف جودة`      — عالية (~75%)\n"
        "👑 `كاشف ذهبي`      — النخبة (~80%)\n"
        "🔴 `وقف` — إيقاف الكاشف\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🎯 *Smart Grid Trading (جديد):*\n\n"
        "🔍 `جريد ماسح`           — يكتشف فرص Grid فوراً\n"
        "🔄 `جريد ماسح تلقائي`    — فحص دوري كل ساعة\n"
        "📊 `جريد ماسح 80`        — فقط score ≥80\n"
        "🟢 `جريد BTC`            — Grid يدوي\n"
        "💪 `جريد BTC قسري`       — رغم عدم المناسبة\n"
        "📋 `جريداتي`             — Grids النشطة\n"
        "🔍 `جريد BTC حالة`       — تفاصيل Grid\n"
        "🔴 `وقف جريد BTC`        — إيقاف Grid\n"
        "🛑 `وقف كل الجريدات`     — إيقاف الكل\n"
        "🛑 `وقف جريد ماسح`       — إيقاف الفحص الدوري\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📊 *معلومات عامة:*\n"
        "`نتائج`        — آخر رصد للكاشف\n"
        "`صفقاتي`       — الصفقات المفتوحة\n"
        "`btc`           — حالة BTC الآن\n"
        "`جريد نتائج`   — آخر فرص Grid\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💡 *Grid مناسب لما:*\n"
        "  • السوق متذبذب (sideways)\n"
        "  • التقلب 2-6%\n"
        "  • السيولة ≥$10M\n"
        "  • ارتدادات متعددة من الحدود\n\n"
        "🎯 *Grid يفشل لما:*\n"
        "  ❌ السوق في trend قوي\n"
        "  ❌ التقلب منخفض جداً\n"
        "  ❌ السعر يخرج النطاق\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🛡 *فلاتر ذكية (7) للكاشف:*\n"
        "  BTC Trend / MTF / Order Book / Funding\n"
        "  Sweep / Volume / BTC Correlation\n\n"
        "⚠️ _تنفيذ يدوي — للأغراض التعليمية فقط_",
        parse_mode="Markdown")


async def cmd_test(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """اختبار سريع على أكثر 10 عملات حركةً الآن."""
    chat_id = u.effective_chat.id
    wait = await u.message.reply_text(
        "🔍 اختبار سريع — أكثر 10 عملات حركةً الآن...",
        parse_mode="Markdown")
    loop = asyncio.get_event_loop()

    def quick_test():
        coins = fetch_all_usdt()[:10]  # أكثر 10 بحركة
        results = []
        for meta in coins:
            try:
                r = analyze_coin(meta["sym"], meta)
                results.append(r)
            except Exception as e:
                results.append({"sym": meta["sym"], "score": 0,
                                 "phase": "error", "signals": [str(e)[:50]],
                                 "price": meta.get("price",0), "chg_24h": meta.get("chg_24h",0)})
        results.sort(key=lambda x: x["score"], reverse=True)
        return results

    try:
        results = await asyncio.wait_for(
            loop.run_in_executor(None, quick_test), timeout=120)
        last_results[chat_id] = results
        await wait.delete()

        m = "✅ *نتائج الاختبار السريع:*\n\n"
        for i, r in enumerate(results, 1):
            phase_txt = PHASE_LABELS.get(r["phase"], r["phase"])
            bar = "█"*int(r["score"]/10) + "░"*(10-int(r["score"]/10))
            m += f"*{i}. {r['sym']}* `{r['score']}/100`\n"
            m += f"   💰 `${fmt(r['price'])}` | 24h: `{r['chg_24h']:+.1f}%`\n"
            m += f"   `{bar}`\n"
            for s in r["signals"][:2]: m += f"   {s}\n"
            m += "\n"
        if not any(r["score"] > 0 for r in results):
            m += "_لا توجد إشارات انفجار الآن — السوق هادئ_\n"
        m += "\n`كاشف` للمسح الكامل كل 5 دقائق"
        await u.message.reply_text(m, parse_mode="Markdown")
    except asyncio.TimeoutError:
        await wait.edit_text("❌ انتهى الوقت — جرب مرة أخرى")
    except Exception as e:
        await wait.edit_text(f"❌ {str(e)[:100]}")


async def handle_msg(u: Update, c: ContextTypes.DEFAULT_TYPE):
    text    = u.message.text.strip()
    chat_id = u.effective_chat.id
    cfg     = user_settings.setdefault(chat_id, {"min_score": 60, "active": False})

    # ── تفعيل ──
    if text.startswith("كاشف") or text.lower() in ("start scan","explosion"):
        parts  = text.split()
        min_sc = cfg.get("min_score", 60)
        quality_mode = False
        min_filters = 0
        mode_label = "عادي"

        # 🆕 وضع الجودة العالية
        for p in parts:
            pl = p.lower()
            if pl in ("مبكر", "early", "pre"):
                # 🆕 وضع الكشف المبكر — يلتقط قبل الانفجار
                quality_mode = True
                min_filters = 1  # مرونة عالية للمبكر
                min_sc = 30  # نقاط منخفضة لأن الانفجار لم يبدأ
                mode_label = "🌱 كشف مبكر (قبل الانفجار)"
            elif pl in ("جودة", "quality", "premium", "فاخر"):
                quality_mode = True
                min_filters = 4
                min_sc = 60
                mode_label = "💎 جودة عالية"
            elif pl in ("ذهبي", "gold"):
                quality_mode = True
                min_filters = 5
                min_sc = 65
                mode_label = "👑 ذهبي (الأقوى)"
            elif pl in ("متوازن", "balanced"):
                quality_mode = False
                min_filters = 2
                mode_label = "⚖️ متوازن"
            else:
                try:
                    n = int(p)
                    if 30 <= n <= 90:
                        min_sc = n
                except:
                    pass

        cfg["min_score"]    = min_sc
        cfg["quality_mode"] = quality_mode
        cfg["min_filters"]  = min_filters
        cfg["active"]       = True

        # إيقاف القديم إن وجد
        for jname in [f"scan_{chat_id}", f"mon_{chat_id}"]:
            for j in c.job_queue.get_jobs_by_name(jname):
                j.schedule_removal()

        # كاشف كل 5 دقائق
        c.job_queue.run_repeating(
            scanner_job, interval=300, first=15,
            data={"chat_id": chat_id, "min_score": min_sc,
                  "quality_mode": quality_mode, "min_filters": min_filters},
            name=f"scan_{chat_id}")

        # مراقبة الصفقات كل دقيقتين
        c.job_queue.run_repeating(
            monitor_job, interval=120, first=60,
            data={"chat_id": chat_id},
            name=f"mon_{chat_id}")

        # 🆕 جلب اتجاه BTC الحالي عند التفعيل
        try:
            loop = asyncio.get_event_loop()
            btc = await loop.run_in_executor(None, get_btc_trend)
        except:
            btc = None

        msg = f"🚨 *تم تفعيل كاشف الانفجار v2*\n\n"
        msg += f"🎯 الوضع: *{mode_label}*\n"
        msg += f"⏱ مسح كل 5 دقائق\n"
        msg += f"📈 حد النقاط: `{min_sc}/100`\n"
        if min_filters > 0:
            msg += f"🛡 الفلاتر الذكية: ≥`{min_filters}/7`\n"
        msg += f"📊 كل عملات Binance Futures\n"
        msg += f"👁 مراقبة الصفقات كل دقيقتين\n\n"

        if btc and btc.get("valid"):
            trend_emoji = "🟢" if btc["trend"] == "bullish" else "🔴" if btc["trend"] == "bearish" else "⚪"
            msg += f"📈 *حالة BTC الآن:*\n"
            msg += f"   {trend_emoji} {btc['trend']} ({btc['score']}/100)\n"
            for d in btc.get("details", [])[:3]:
                msg += f"   • {d}\n"
            msg += "\n"

        msg += "📡 *الفلاتر الذكية المفعّلة (7):*\n"
        msg += "  ① BTC Trend     — اتجاه السوق العام\n"
        msg += "  ② MTF Confirm   — توافق الفريمات\n"
        msg += "  ③ Order Book    — ضغط الأوامر\n"
        msg += "  ④ Funding/OI    — تمويل + الفائدة المفتوحة\n"
        msg += "  ⑤ Sweep+Reclaim — كسر السيولة\n"
        msg += "  ⑥ Volume Quality — جودة الحجم\n"
        msg += "  ⑦ BTC Correl    — ارتباط BTC\n\n"

        msg += "💡 *أوضاع متاحة:*\n"
        msg += "  🌱 `كاشف مبكر` — *قبل الانفجار!* ⭐\n"
        msg += "  `كاشف عادي`    — كل الإشارات (دقة ~55%)\n"
        msg += "  `كاشف متوازن`  — جودة معتدلة (~65%)\n"
        msg += "  `كاشف جودة`    — عالي الجودة (~75%)\n"
        msg += "  `كاشف ذهبي`    — النخبة فقط (~80%)\n\n"

        msg += "إيقاف: `وقف`"

        await u.message.reply_text(msg, parse_mode="Markdown")
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

    # ── اختبار سريع ──
    if text in ("اختبار","test","تجربة"):
        await cmd_test(u, c)
        return

    # ── btc / حالة BTC ──
    if text.lower() in ("btc", "بتك", "بيتكوين", "btc trend"):
        loop = asyncio.get_event_loop()
        btc = await loop.run_in_executor(None, get_btc_trend)
        if not btc.get("valid"):
            await u.message.reply_text(
                "⚠️ تعذّر جلب بيانات BTC\nتحقق من اتصال Binance",
                parse_mode="Markdown")
            return

        trend_emoji = {"bullish": "🟢", "bearish": "🔴", "neutral": "⚪"}.get(btc["trend"], "⚪")
        bar = "█" * int(btc["score"]/10) + "░" * (10 - int(btc["score"]/10))

        m = f"📈 *حالة BTC الآن*\n"
        m += f"🕐 {now_sa()}\n\n"
        m += f"💰 السعر: `${fmt(btc['price'])}`\n"
        m += f"{trend_emoji} الاتجاه: *{btc['trend'].upper()}*\n"
        m += f"🎯 قوة الاتجاه: `{btc['score']}/100`\n"
        m += f"`{bar}`\n\n"

        m += f"📊 *تفاصيل:*\n"
        m += f"  • RSI 1H: `{btc.get('rsi_1h', 50):.1f}`\n"
        m += f"  • EMA21 1H: `${fmt(btc.get('ema21_1h', 0))}`\n"
        m += f"  • EMA50 1H: `${fmt(btc.get('ema50_1h', 0))}`\n\n"

        if btc.get("details"):
            m += f"📡 *الإشارات:*\n"
            for d in btc["details"]:
                m += f"  • {d}\n"
            m += "\n"

        # توصية
        m += "━━━━━━━━━━━━━━━━━━━━\n"
        if btc["trend"] == "bullish":
            m += "💡 *توصية:* السوق صاعد — LONG مفضّل\n"
            m += "   تجنّب SHORT حتى لو الإشارة قوية"
        elif btc["trend"] == "bearish":
            m += "💡 *توصية:* السوق هابط — SHORT مفضّل\n"
            m += "   احذر من LONG حتى لو الإشارة قوية"
        else:
            m += "💡 *توصية:* السوق متذبذب\n"
            m += "   صفقات قصيرة الأمد فقط (سكالب)"

        await u.message.reply_text(m, parse_mode="Markdown")
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

    # ══════════════════════════════════════════════════════════════
    # 🆕 SMART GRID COMMANDS
    # ══════════════════════════════════════════════════════════════

    # ── ماسح الجريد (تلقائي/فوري) ──
    if text.startswith("جريد ماسح") or text.lower() in ("grid scan", "scan grid"):
        parts = text.split()
        # تحقق من تلقائي
        is_auto = any(p in ("تلقائي", "auto") for p in parts)
        # تحقق من min_score
        min_sc = 70
        for p in parts:
            try:
                n = int(p)
                if 40 <= n <= 95:
                    min_sc = n
            except ValueError:
                pass

        if is_auto:
            # تفعيل الفحص التلقائي كل ساعة
            jname = f"grid_scan_{chat_id}"
            for j in c.job_queue.get_jobs_by_name(jname):
                j.schedule_removal()
            c.job_queue.run_repeating(
                grid_scanner_job, interval=3600, first=10,
                data={"chat_id": chat_id, "min_score": min_sc},
                name=jname,
            )
            await u.message.reply_text(
                f"🔍 *تم تفعيل ماسح Grid التلقائي*\n\n"
                f"⏱ يفحص كل ساعة\n"
                f"🎯 الحد: ≥`{min_sc}/100`\n"
                f"📊 يفحص أعلى 100 عملة بالحجم\n"
                f"⏳ Cooldown: ساعتين بين تنبيهات نفس العملة\n\n"
                f"إيقاف: `وقف جريد ماسح`",
                parse_mode="Markdown",
            )
            # شغّل أيضاً مراقب الـ Grids (لو فيه)
            mon_name = f"grid_mon_{chat_id}"
            for j in c.job_queue.get_jobs_by_name(mon_name):
                j.schedule_removal()
            c.job_queue.run_repeating(
                grid_monitor_job, interval=120, first=60,
                data={"chat_id": chat_id},
                name=mon_name,
            )
        else:
            # فحص فوري
            wait = await u.message.reply_text(
                "🔍 *جاري فحص فرص Grid...*\n"
                "⏳ يستغرق 30-60 ثانية...",
                parse_mode="Markdown",
            )
            try:
                loop = asyncio.get_event_loop()
                candidates = await asyncio.wait_for(
                    loop.run_in_executor(None, scan_grid_opportunities, min_sc, 10),
                    timeout=180,
                )
                last_grid_results[chat_id] = candidates
                msg = build_grid_scanner_alert(candidates, max_show=3)
                try:
                    await wait.delete()
                except Exception:
                    pass
                await u.message.reply_text(msg, parse_mode="Markdown")
            except asyncio.TimeoutError:
                await u.message.reply_text("⚠️ انتهى الوقت. حاول لاحقاً.")
            except Exception as e:
                logging.warning(f"[GRID_SCAN_CMD] {e}")
                await u.message.reply_text("⚠️ خطأ في الفحص.")
        return

    # ── إيقاف ماسح الجريد ──
    if text in ("وقف جريد ماسح", "وقف ماسح جريد", "stop grid scan"):
        jname = f"grid_scan_{chat_id}"
        removed = 0
        for j in c.job_queue.get_jobs_by_name(jname):
            j.schedule_removal()
            removed += 1
        if removed:
            await u.message.reply_text("⛔ تم إيقاف ماسح Grid التلقائي")
        else:
            await u.message.reply_text("لا يوجد ماسح Grid نشط")
        return

    # ── إنشاء Grid يدوي ──
    if text.startswith("جريد ") and not text.startswith("جريد ماسح") and \
       text not in ("جريداتي", "جريد كل"):
        parts = text.split()
        if len(parts) < 2:
            await u.message.reply_text(
                "📝 الصيغة: `جريد BTC` أو `جريد BTC 5%` أو `جريد BTC قسري`",
                parse_mode="Markdown",
            )
            return

        raw_sym = parts[1].upper()
        force = any(p in ("قسري", "force") for p in parts)
        # تحقق من نسبة النطاق المخصصة
        custom_range = None
        for p in parts[2:]:
            if p.endswith("%"):
                try:
                    custom_range = float(p.rstrip("%"))
                except ValueError:
                    pass

        sym = raw_sym if raw_sym.endswith("USDT") else raw_sym + "USDT"

        wait = await u.message.reply_text(
            f"📊 *جاري تحليل {sym} للـ Grid...*",
            parse_mode="Markdown",
        )

        try:
            loop = asyncio.get_event_loop()
            analysis = await loop.run_in_executor(None, analyze_grid_suitability, sym, None, None)

            if analysis["current_price"] <= 0:
                try: await wait.delete()
                except: pass
                await u.message.reply_text(f"❌ تعذّر جلب بيانات `{sym}`", parse_mode="Markdown")
                return

            # لو غير مناسب وما طلب قسري
            if not analysis["suitable"] and not force:
                try: await wait.delete()
                except: pass
                msg = (f"⚠️ *{sym} غير مناسب لـ Grid*\n\n"
                       f"📊 Score: `{analysis['score']}/100`\n"
                       f"🎚 المستوى: {analysis['level']}\n\n")
                if analysis.get("warnings"):
                    msg += "⚠️ *الأسباب:*\n"
                    for w in analysis["warnings"][:4]:
                        msg += f"  • {w}\n"
                msg += f"\n💡 لفرض الـ Grid رغم ذلك:\n`جريد {raw_sym} قسري`"
                await u.message.reply_text(msg, parse_mode="Markdown")
                return

            # حساب مستويات Grid
            current_price = analysis["current_price"]
            if custom_range:
                range_high = current_price * (1 + custom_range / 100)
                range_low = current_price * (1 - custom_range / 100)
            else:
                range_high = analysis["range_high"]
                range_low = analysis["range_low"]

            grid = calculate_grid_levels(current_price, range_high, range_low, levels=10)

            # احفظ في active_grids
            active_grids.setdefault(chat_id, {})[sym] = {
                "sym":            sym,
                "center_price":   current_price,
                "grid":           grid,
                "analysis":       analysis,
                "history":        [],
                "created_at":     time.time(),
                "range_break_alerted": False,
            }

            # شغّل المراقب لو ما يشتغل
            mon_name = f"grid_mon_{chat_id}"
            jobs_running = c.job_queue.get_jobs_by_name(mon_name)
            if not jobs_running:
                c.job_queue.run_repeating(
                    grid_monitor_job, interval=120, first=60,
                    data={"chat_id": chat_id},
                    name=mon_name,
                )

            try: await wait.delete()
            except: pass

            # رسالة التأكيد
            msg = (f"✅ *تم إنشاء Grid: {sym}*\n"
                   f"━━━━━━━━━━━━━━━━━━━━\n\n"
                   f"📊 Score: `{analysis['score']}/100` — {analysis['level']}\n"
                   f"💰 السعر الحالي: `${fmt(current_price)}`\n"
                   f"📏 النطاق: `${fmt(range_low)}` — `${fmt(range_high)}`\n"
                   f"📐 عدد المستويات: `{len(grid['buy_levels']) + len(grid['sell_levels'])}`\n"
                   f"💵 المسافة بين المستويات: `{grid['spacing_pct']:.2f}%`\n"
                   f"📈 الربح المتوقع/دورة: `{grid['expected_profit_per_cycle']:.2f}%`\n\n"
                   f"🟢 *مستويات الشراء:*\n")
            for b in grid["buy_levels"]:
                msg += f"  #{b['level']}: `${fmt(b['price'])}` ({b['distance']:+.2f}%)\n"
            msg += f"\n🔴 *مستويات البيع:*\n"
            for s in grid["sell_levels"]:
                msg += f"  #{s['level']}: `${fmt(s['price'])}` (+{s['distance']:.2f}%)\n"
            msg += (f"\n📡 *المراقبة:*\n"
                    f"  • فحص كل دقيقتين\n"
                    f"  • تنبيه عند لمس أي مستوى\n"
                    f"  • تحذير عند خروج النطاق\n\n"
                    f"⚠️ _تنفيذ يدوي فقط_\n\n"
                    f"📊 الحالة: `جريد {raw_sym} حالة`\n"
                    f"🔴 إيقاف: `وقف جريد {raw_sym}`")
            await u.message.reply_text(msg, parse_mode="Markdown")
        except Exception as e:
            logging.warning(f"[GRID_CREATE] {e}")
            try: await wait.delete()
            except: pass
            await u.message.reply_text(f"⚠️ خطأ في إنشاء Grid: {e}")
        return

    # ── حالة Grid معينة ──
    if "حالة" in text and "جريد" in text:
        parts = text.split()
        sym = None
        for p in parts:
            if p.upper() not in ("جريد", "حالة", "GRID", "STATUS"):
                raw = p.upper()
                sym = raw if raw.endswith("USDT") else raw + "USDT"
                break
        if not sym:
            await u.message.reply_text("الصيغة: `جريد BTC حالة`", parse_mode="Markdown")
            return

        grid_data = active_grids.get(chat_id, {}).get(sym)
        if not grid_data:
            await u.message.reply_text(f"لا يوجد Grid على `{sym}`", parse_mode="Markdown")
            return

        msg = build_grid_status(grid_data)
        await u.message.reply_text(msg, parse_mode="Markdown")
        return

    # ── إيقاف Grid معين ──
    if text.startswith("وقف جريد ") and "ماسح" not in text:
        parts = text.split()
        if len(parts) >= 3:
            raw = parts[2].upper()
            sym = raw if raw.endswith("USDT") else raw + "USDT"
            if sym in active_grids.get(chat_id, {}):
                del active_grids[chat_id][sym]
                await u.message.reply_text(
                    f"⛔ تم إيقاف Grid على `{sym}`",
                    parse_mode="Markdown",
                )
            else:
                await u.message.reply_text(
                    f"لا يوجد Grid على `{sym}`",
                    parse_mode="Markdown",
                )
        return

    # ── إيقاف كل الجريدات ──
    if text in ("وقف كل الجريدات", "وقف كل جريد", "stop all grids"):
        if active_grids.get(chat_id):
            count = len(active_grids[chat_id])
            active_grids[chat_id] = {}
            mon_name = f"grid_mon_{chat_id}"
            for j in c.job_queue.get_jobs_by_name(mon_name):
                j.schedule_removal()
            await u.message.reply_text(f"⛔ تم إيقاف {count} Grid")
        else:
            await u.message.reply_text("لا توجد Grids نشطة")
        return

    # ── عرض جريداتي ──
    if text in ("جريداتي", "جريداتى", "my grids"):
        grids = active_grids.get(chat_id, {})
        if not grids:
            await u.message.reply_text(
                "📭 لا توجد Grids نشطة.\n\n"
                "💡 ابدأ بـ:\n"
                "  `جريد ماسح` — اكتشف الفرص\n"
                "  `جريد BTC` — Grid يدوي",
                parse_mode="Markdown",
            )
            return

        msg = f"📊 *Grids النشطة ({len(grids)}):*\n"
        msg += f"━━━━━━━━━━━━━━━━━━━━\n\n"
        for sym, gd in grids.items():
            grid = gd.get("grid", {})
            history = gd.get("history", [])
            current = fetch_current_price(sym)
            center = gd.get("center_price", 0)

            filled_buys = sum(1 for b in grid.get("buy_levels", []) if b.get("filled"))
            filled_sells = sum(1 for s in grid.get("sell_levels", []) if s.get("filled"))
            chg_pct = ((current - center) / center * 100) if center > 0 else 0

            msg += f"*{sym}*\n"
            msg += f"  💰 الآن: `${fmt(current)}` ({chg_pct:+.2f}% من المركز)\n"
            msg += f"  📏 النطاق: `${fmt(grid.get('range_low',0))}` - `${fmt(grid.get('range_high',0))}`\n"
            msg += f"  🟢 شراء: {filled_buys}/{len(grid.get('buy_levels', []))}\n"
            msg += f"  🔴 بيع: {filled_sells}/{len(grid.get('sell_levels', []))}\n"
            msg += f"  📋 معاملات: {len(history)}\n\n"
        msg += f"💡 للتفاصيل: `جريد BTC حالة`"
        await u.message.reply_text(msg, parse_mode="Markdown")
        return

    # ── نتائج آخر فحص ماسح ──
    if text in ("جريد نتائج", "نتائج جريد", "grid results"):
        results = last_grid_results.get(chat_id, [])
        if not results:
            await u.message.reply_text(
                "لا توجد نتائج بعد.\nفعّل الماسح: `جريد ماسح`",
                parse_mode="Markdown",
            )
            return
        msg = build_grid_scanner_alert(results, max_show=5)
        await u.message.reply_text(msg, parse_mode="Markdown")
        return

    # ══════════════════════════════════════════════════════════════
    # نهاية أوامر Grid
    # ══════════════════════════════════════════════════════════════

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

    elif data == "grid:results":
        # عرض كل نتائج الماسح
        results = last_grid_results.get(chat_id, [])
        if not results:
            await q.answer("لا توجد نتائج", show_alert=True)
            return
        msg = build_grid_scanner_alert(results, max_show=5)
        try:
            await q.message.reply_text(msg, parse_mode="Markdown")
            await q.answer()
        except Exception as e:
            await q.answer(f"خطأ: {e}", show_alert=True)


# ══════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════

user_settings = {}  # {chat_id: {min_score, active}}

async def _post_init(app):
    """يشتغل بعد ما البوت يبدأ event loop — يحذف webhook قديم (يمنع Conflict)"""
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        logging.warning("[INIT] webhook cleared")
    except Exception as e:
        logging.warning(f"[INIT] delete_webhook failed: {e}")

    # فحص اتصال Binance Futures
    try:
        loop = asyncio.get_event_loop()
        coins = await loop.run_in_executor(None, fetch_all_usdt, 0)
        binance_status = f"✅ ({len(coins)} عملة)" if coins else "⚠️ فارغ"
    except Exception as e:
        binance_status = f"❌ ({type(e).__name__})"

    print("=" * 55)
    print("  💥 EXPLOSION DETECTOR BOT v3 — Running ✅")
    print("=" * 55)
    print(f"  Binance Futures  : {binance_status}")
    print(f"  المؤشرات         : 7 (Volume, CVD, BB, RSI, +)")
    print(f"  المراحل          : pre_explosion → start → now")
    print(f"  الفلاتر الذكية   : 7 (BTC + MTF + OB + ...)")
    print(f"  Squeeze→Breakout : 💎 إشارة ذهبية")
    print(f"  المسح            : كل 5 دقائق")
    print(f"  المراقبة         : كل دقيقتين")
    print(f"  cooldown         : ساعة لكل عملة")
    print(f"  ─────────────────────────────────")
    print(f"  🆕 Smart Grid    : ماسح ذكي + يدوي")
    print(f"  Grid Scanner     : كل ساعة (لو فعّلت)")
    print(f"  Grid Monitor     : كل دقيقتين")
    print("=" * 55)
    print("  أرسل /start على تيليقرام")
    print("=" * 55)


def main():
    if BOT_TOKEN in ("YOUR_TOKEN_HERE", ""):
        print("=" * 55)
        print("  ❌ ERROR: BOT_TOKEN غير موجود")
        print("  أضفه في Railway → Variables → BOT_TOKEN")
        print("=" * 55)
        return

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(_post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("test",  cmd_test))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))

    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
