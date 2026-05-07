"""
Exhaustion Detector — identifies the END of momentum in BOTH directions.

Buying Exhaustion (top signals):
1. Buying Climax: massive volume + price stalls (sellers absorbing)
2. Bearish CVD divergence: price HH, CVD LH
3. Failed BOS high: wick above swing high, rejected
4. Extreme positive funding + price stagnation
5. Volume drying on rallies

Selling Exhaustion (bottom signals - mirror image):
1. Selling Climax: massive volume + price refuses to drop
2. Bullish CVD divergence: price LL, CVD HL
3. Failed BOS low: wick below swing low, rejected
4. Extreme negative funding + stagnation
5. Volume drying on selloffs

Returns ExhaustionSnapshot with strength score and recommended actions.
"""
import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from typing import Optional
from core.logger import get_logger

log = get_logger(__name__)


@dataclass
class ExhaustionSnapshot:
    direction: str = "neutral"
    strength: int = 0
    signals_detected: list = field(default_factory=list)
    summary_ar: str = ""

    buying_climax: bool = False
    bearish_cvd_divergence: bool = False
    failed_bos_high: bool = False
    funding_too_long: bool = False
    volume_drying_up: bool = False

    selling_climax: bool = False
    bullish_cvd_divergence: bool = False
    failed_bos_low: bool = False
    funding_too_short: bool = False
    volume_drying_down: bool = False

    should_close_long: bool = False
    should_close_short: bool = False
    should_consider_short: bool = False
    should_consider_long: bool = False

    @property
    def signals_count(self) -> int:
        """How many distinct exhaustion signals fired."""
        return len(self.signals_detected)


def render_exhaustion_alert(exhaustion: ExhaustionSnapshot,
                              symbol: str, current_price: float) -> str:
    """Format exhaustion alert message for Telegram."""
    if exhaustion.direction == "neutral":
        return ""

    if exhaustion.direction == "buying_exhaustion":
        emoji = "🛑"
        title = "إنذار إرهاق شرائي"
        action_hint = (
            "💡 *المعنى:* قوة المشترين تضعف. السعر قد يبدأ بالهبوط قريباً.\n"
            "  ⚠️ احذر إذا كنت في صفقة شراء — فكر في الإغلاق."
        )
    else:
        emoji = "⚡"
        title = "إنذار إرهاق بيعي"
        action_hint = (
            "💡 *المعنى:* قوة البائعين تضعف. السعر قد يبدأ بالصعود قريباً.\n"
            "  ⚠️ احذر إذا كنت في صفقة بيع — فكر في الإغلاق."
        )

    m = f"{emoji} *{title}*\n"
    m += f"━━━━━━━━━━━━━━━━━━━━\n\n"
    m += f"🪙 *{symbol}* | السعر: `${current_price:.6g}`\n"
    m += f"💪 قوة الإرهاق: *{exhaustion.strength}/100*\n\n"

    m += f"📊 *العلامات المرصودة ({exhaustion.signals_count}):*\n"
    for sig in exhaustion.signals_detected[:5]:
        m += f"  • {sig}\n"
    m += "\n"

    m += action_hint

    if exhaustion.should_consider_short:
        m += f"\n\n🐻 *تنبيه إضافي:* قوة الإرهاق عالية ({exhaustion.strength}/100)\n"
        m += "قد تكون فرصة جيدة لـ SHORT (بعد تأكيد كسر هيكل)."
    elif exhaustion.should_consider_long:
        m += f"\n\n🐂 *تنبيه إضافي:* قوة الإرهاق عالية ({exhaustion.strength}/100)\n"
        m += "قد تكون فرصة جيدة لـ LONG (بعد تأكيد كسر هيكل صاعد)."

    return m


def _detect_climax(df: pd.DataFrame, lookback: int = 5) -> dict:
    if df is None or len(df) < lookback + 25:
        return {"detected": False}
    recent = df.tail(lookback)
    historical = df.tail(35).head(30)
    avg_vol = float(historical["qv"].astype(float).mean())
    recent_vol = float(recent["qv"].astype(float).sum() / lookback)
    if recent_vol < avg_vol * 2.0:
        return {"detected": False}
    high = float(recent["h"].astype(float).max())
    low = float(recent["l"].astype(float).min())
    open_p = float(recent["o"].astype(float).iloc[0])
    close_p = float(recent["c"].astype(float).iloc[-1])
    range_pct = (high - low) / max(open_p, 1e-9) * 100
    move_pct = abs(close_p - open_p) / max(open_p, 1e-9) * 100
    if range_pct < 3.0 and move_pct < 1.2:
        bqv = float(recent["bqv"].astype(float).sum())
        sqv = float(recent["qv"].astype(float).sum() - bqv)
        if bqv > sqv * 1.15 and close_p <= open_p * 1.008:
            return {"detected": True, "kind": "buying_climax",
                     "vol_mult": recent_vol / avg_vol,
                     "msg_ar": (f"🛑 ذروة شراء ({recent_vol/avg_vol:.1f}x volume، "
                                 f"السعر يرفض الصعود)")}
        if sqv > bqv * 1.15 and close_p >= open_p * 0.992:
            return {"detected": True, "kind": "selling_climax",
                     "vol_mult": recent_vol / avg_vol,
                     "msg_ar": (f"🛑 ذروة بيع ({recent_vol/avg_vol:.1f}x volume، "
                                 f"السعر يرفض الهبوط)")}
    return {"detected": False}


def _detect_cvd_divergence(df: pd.DataFrame, lookback: int = 30) -> dict:
    if df is None or len(df) < lookback:
        return {"bullish": False, "bearish": False, "bear_strength": 0, "bull_strength": 0}
    work = df.tail(lookback).copy()
    closes = work["c"].astype(float).values
    bqv = work["bqv"].astype(float).values
    qv = work["qv"].astype(float).values
    delta = bqv - (qv - bqv)
    cvd = np.cumsum(delta)

    def _peaks(arr, w=4):
        return [(i, arr[i]) for i in range(w, len(arr) - w)
                if all(arr[i] >= arr[i-j] for j in range(1, w+1))
                and all(arr[i] >= arr[i+j] for j in range(1, w+1))]
    def _troughs(arr, w=4):
        return [(i, arr[i]) for i in range(w, len(arr) - w)
                if all(arr[i] <= arr[i-j] for j in range(1, w+1))
                and all(arr[i] <= arr[i+j] for j in range(1, w+1))]

    p_peaks = _peaks(closes)
    p_troughs = _troughs(closes)
    bearish = bullish = False
    bear_strength = bull_strength = 0

    if len(p_peaks) >= 2:
        p1_i, p1_v = p_peaks[-2]
        p2_i, p2_v = p_peaks[-1]
        if p2_v > p1_v:
            c1, c2 = cvd[p1_i], cvd[p2_i]
            if c2 < c1:
                bearish = True
                cvd_drop = abs(c1 - c2) / max(abs(c1), 1) * 100
                bear_strength = (3 if cvd_drop > 30 else 2 if cvd_drop > 15 else 1)

    if len(p_troughs) >= 2:
        t1_i, t1_v = p_troughs[-2]
        t2_i, t2_v = p_troughs[-1]
        if t2_v < t1_v:
            c1, c2 = cvd[t1_i], cvd[t2_i]
            if c2 > c1:
                bullish = True
                cvd_rise = abs(c2 - c1) / max(abs(c1), 1) * 100
                bull_strength = (3 if cvd_rise > 30 else 2 if cvd_rise > 15 else 1)

    return {"bullish": bullish, "bearish": bearish,
            "bull_strength": bull_strength, "bear_strength": bear_strength}


def _detect_failed_bos(df: pd.DataFrame, lookback: int = 30) -> dict:
    if df is None or len(df) < lookback:
        return {"failed_high": False, "failed_low": False, "fh_strength": 0, "fl_strength": 0}
    work = df.tail(lookback).reset_index(drop=True)
    highs = work["h"].astype(float).values
    lows = work["l"].astype(float).values
    closes = work["c"].astype(float).values
    n = len(work)
    if n < 15:
        return {"failed_high": False, "failed_low": False, "fh_strength": 0, "fl_strength": 0}
    swing_zone_end = int(n * 0.7)
    earlier_high = float(highs[:swing_zone_end].max())
    earlier_low = float(lows[:swing_zone_end].min())
    failed_high = failed_low = False
    fh_strength = fl_strength = 0
    for i in range(max(swing_zone_end, n - 4), n):
        if highs[i] > earlier_high and closes[i] < earlier_high:
            wick_pct = (highs[i] - earlier_high) / earlier_high * 100
            close_below_pct = (earlier_high - closes[i]) / earlier_high * 100
            if wick_pct > 0.15 and close_below_pct > 0.05:
                failed_high = True
                fh_strength = max(fh_strength,
                                    3 if wick_pct > 0.5 else
                                    2 if wick_pct > 0.25 else 1)
        if lows[i] < earlier_low and closes[i] > earlier_low:
            wick_pct = (earlier_low - lows[i]) / earlier_low * 100
            close_above_pct = (closes[i] - earlier_low) / earlier_low * 100
            if wick_pct > 0.15 and close_above_pct > 0.05:
                failed_low = True
                fl_strength = max(fl_strength,
                                    3 if wick_pct > 0.5 else
                                    2 if wick_pct > 0.25 else 1)
    return {"failed_high": failed_high, "failed_low": failed_low,
            "fh_strength": fh_strength, "fl_strength": fl_strength}


def _detect_volume_drying(df: pd.DataFrame, lookback: int = 12) -> dict:
    if df is None or len(df) < lookback + 5:
        return {"drying_up": False, "drying_down": False}
    work = df.tail(lookback)
    volumes = work["qv"].astype(float).values
    closes = work["c"].astype(float).values
    overall_change = (closes[-1] - closes[0]) / closes[0]
    vol_first_half = volumes[:lookback // 2].mean()
    vol_second_half = volumes[lookback // 2:].mean()
    vol_decreasing = vol_second_half < vol_first_half * 0.7
    return {
        "drying_up": overall_change > 0.005 and vol_decreasing,
        "drying_down": overall_change < -0.005 and vol_decreasing,
    }


def analyze_exhaustion(df_5m: pd.DataFrame, df_1h: Optional[pd.DataFrame] = None,
                          funding_rate: Optional[float] = None,
                          change_24h: float = 0.0) -> ExhaustionSnapshot:
    """Full exhaustion analysis. Detects both buying AND selling exhaustion."""
    snap = ExhaustionSnapshot()
    if df_5m is None or len(df_5m) < 30:
        return snap

    climax = _detect_climax(df_5m)
    if climax.get("detected"):
        if climax["kind"] == "buying_climax":
            snap.buying_climax = True
            snap.signals_detected.append(climax["msg_ar"])
        else:
            snap.selling_climax = True
            snap.signals_detected.append(climax["msg_ar"])

    div = _detect_cvd_divergence(df_5m)
    if div["bearish"]:
        snap.bearish_cvd_divergence = True
        snap.signals_detected.append(
            f"⚠️ تباعد سلبي CVD (قوة {div['bear_strength']}) — قوة الشراء تضعف"
        )
    if div["bullish"]:
        snap.bullish_cvd_divergence = True
        snap.signals_detected.append(
            f"⚡ تباعد إيجابي CVD (قوة {div['bull_strength']}) — قوة البيع تضعف"
        )

    bos = _detect_failed_bos(df_5m)
    if bos["failed_high"]:
        snap.failed_bos_high = True
        snap.signals_detected.append(
            f"❌ فشل اختراق القمة (قوة {bos['fh_strength']}) — المشترين عجزوا"
        )
    if bos["failed_low"]:
        snap.failed_bos_low = True
        snap.signals_detected.append(
            f"❌ فشل كسر القاع (قوة {bos['fl_strength']}) — البائعين عجزوا"
        )

    if funding_rate is not None:
        funding_pct = funding_rate * 100
        if funding_pct > 0.04 and abs(change_24h) < 3:
            snap.funding_too_long = True
            snap.signals_detected.append(
                f"⚠️ Funding مرتفع ({funding_pct:.3f}%) + ركود — longs مكدسة"
            )
        elif funding_pct < -0.04 and abs(change_24h) < 3:
            snap.funding_too_short = True
            snap.signals_detected.append(
                f"⚡ Funding سالب ({funding_pct:.3f}%) + ركود — shorts مكدسة"
            )

    vol_dry = _detect_volume_drying(df_5m)
    if vol_dry["drying_up"]:
        snap.volume_drying_up = True
        snap.signals_detected.append("📉 الحجم يتراجع رغم الصعود — momentum يموت")
    if vol_dry["drying_down"]:
        snap.volume_drying_down = True
        snap.signals_detected.append("📈 الحجم يتراجع رغم الهبوط — selling pressure تموت")

    # Score weighting
    buying_score = 0
    if snap.buying_climax: buying_score += 30
    if snap.bearish_cvd_divergence: buying_score += 25 + div["bear_strength"] * 5
    if snap.failed_bos_high: buying_score += 15 + bos["fh_strength"] * 3
    if snap.funding_too_long: buying_score += 15
    if snap.volume_drying_up: buying_score += 10

    selling_score = 0
    if snap.selling_climax: selling_score += 30
    if snap.bullish_cvd_divergence: selling_score += 25 + div["bull_strength"] * 5
    if snap.failed_bos_low: selling_score += 15 + bos["fl_strength"] * 3
    if snap.funding_too_short: selling_score += 15
    if snap.volume_drying_down: selling_score += 10

    if buying_score > selling_score and buying_score >= 25:
        snap.direction = "buying_exhaustion"
        snap.strength = min(100, buying_score)
        snap.summary_ar = f"🛑 إرهاق شرائي ({snap.strength}/100)"
    elif selling_score > buying_score and selling_score >= 25:
        snap.direction = "selling_exhaustion"
        snap.strength = min(100, selling_score)
        snap.summary_ar = f"⚡ إرهاق بيعي ({snap.strength}/100)"
    else:
        snap.direction = "neutral"
        snap.summary_ar = "تدفق متوازن"

    if snap.direction == "buying_exhaustion" and snap.strength >= 40:
        snap.should_close_long = True
        if snap.strength >= 60:
            snap.should_consider_short = True
    elif snap.direction == "selling_exhaustion" and snap.strength >= 40:
        snap.should_close_short = True
        if snap.strength >= 60:
            snap.should_consider_long = True

    return snap
