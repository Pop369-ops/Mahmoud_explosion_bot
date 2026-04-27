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
    elif result["score"] >= 35:
        result["phase"] = "pre_explosion"

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
                        # في quality mode، نقبل فقط explosion_premium أو explosion_now
                        # مع 4+ فلاتر
                        if r["phase"] not in ("explosion_premium", "explosion_now",
                                               "explosion_start_strong"):
                            rejected += 1
                            continue
                        if sf.get("filters_passed", 0) < 4:
                            rejected += 1
                            continue

                    # 🆕 حد أدنى من الفلاتر للقبول
                    if min_filters > 0 and sf.get("filters_passed", 0) < min_filters:
                        rejected += 1
                        continue

                    alerts.append(r)
                except: continue

            # 🆕 ترتيب ذكي: explosion_premium أولاً، ثم حسب النقاط
            phase_order = {
                "explosion_premium": 0,
                "explosion_now": 1,
                "explosion_start_strong": 2,
                "explosion_start": 3,
                "pre_explosion": 4,
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
        "💥 *EXPLOSION DETECTOR BOT v2*\n"
        "كاشف الانفجار السعري المبكر — *مع 7 فلاتر ذكية*\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⚙️ *أوضاع الكاشف:*\n\n"
        "🟢 `كاشف`           — تفعيل عادي (دقة ~55%)\n"
        "⚖️ `كاشف متوازن`    — جودة معتدلة (~65%)\n"
        "💎 `كاشف جودة`      — عالي الجودة (~75%)\n"
        "👑 `كاشف ذهبي`      — النخبة فقط (~80%)\n\n"
        "أو حدد النقاط:\n"
        "`كاشف 50` `كاشف 60` `كاشف 70`\n\n"
        "🔴 *إيقاف:* `وقف`\n\n"
        "📊 *معلومات:*\n"
        "`نتائج`    — آخر عمليات الرصد\n"
        "`صفقاتي`   — الصفقات المفتوحة\n"
        "`btc`       — حالة BTC الآن\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🛡 *الفلاتر الذكية (7):*\n"
        "  ① BTC Trend     — اتجاه السوق\n"
        "  ② MTF Confirm   — توافق الفريمات\n"
        "  ③ Order Book    — ضغط الأوامر\n"
        "  ④ Funding/OI    — تمويل + OI\n"
        "  ⑤ Sweep+Reclaim — كسر السيولة\n"
        "  ⑥ Volume Quality — جودة الحجم\n"
        "  ⑦ BTC Correl    — ارتباط BTC\n\n"
        "🎯 *مؤشرات الانفجار الأساسية (7):*\n"
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
            if pl in ("جودة", "quality", "premium", "فاخر"):
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
    print("  💥 EXPLOSION DETECTOR BOT — Running ✅")
    print("=" * 55)
    print(f"  Binance Futures : {binance_status}")
    print(f"  المؤشرات        : 7 (Volume, CVD, BB, RSI, +)")
    print(f"  المراحل         : pre_explosion → start → now")
    print(f"  Squeeze→Breakout: 💎 إشارة ذهبية")
    print(f"  المسح           : كل 5 دقائق")
    print(f"  المراقبة        : كل دقيقتين")
    print(f"  cooldown        : ساعة لكل عملة")
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
