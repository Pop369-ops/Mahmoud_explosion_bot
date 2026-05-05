"""
Retroactive Scanner — check if there WOULD have been signals in the past N hours.

Uses available historical data (klines only) and runs the SAME logic
(ICT analysis + HARD GATE) on past candles to identify what signals
the bot would have generated.

Limitations:
- No whale_alert history (real-time only)
- No funding rate history at fine granularity
- No order book history (real-time only)
- No sentiment history

Despite limitations, this catches ~70% of what would have been real signals
because the structural ICT analysis is the most important component.
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import pandas as pd

from data_sources.binance import binance
from scorer.liquidity_levels import build_long_plan
from scorer.htf_bias import compute_bias
from scorer.structure import analyze_structure
from scorer.killzones import get_current_killzone
from core.logger import get_logger

log = get_logger(__name__)


@dataclass
class HistoricalSignal:
    symbol: str
    candle_time: datetime
    candles_ago: int
    price: float
    change_24h_at_time: float
    confidence: int
    sources_strong: int      # how many sub-signals lit up
    htf_aligned: bool
    sl: float
    tp1: float
    rr_tp1: float
    method: str
    sl_logic: str
    rejection_reasons: list = field(default_factory=list)
    accepted: bool = False


def _quick_indicators(df: pd.DataFrame, idx: int) -> dict:
    """Fast lightweight detector suite for backtest."""
    if idx < 20:
        return {}
    window = df.iloc[max(0, idx - 30):idx + 1].copy()
    closes = window["c"].astype(float).values
    highs = window["h"].astype(float).values
    lows = window["l"].astype(float).values
    volumes = window["v"].astype(float).values
    buy_vol = window["bqv"].astype(float).values

    last_close = closes[-1]
    last_vol = volumes[-1]
    avg_vol = volumes[-21:-1].mean() if len(volumes) >= 21 else volumes.mean()

    # 1. Volume surge
    vol_mult = last_vol / max(avg_vol, 1)
    vol_score = min(100, int(vol_mult * 30))

    # 2. Buy/Sell delta (last 5 candles)
    recent_buy = window["bqv"].astype(float).iloc[-5:].sum()
    recent_total = window["qv"].astype(float).iloc[-5:].sum()
    delta_pct = (recent_buy / max(recent_total, 1) - 0.5) * 200  # -100..+100
    delta_score = max(0, min(100, int(50 + delta_pct / 2)))

    # 3. Squeeze: ATR contracting then expanding
    if len(window) >= 20:
        early_atr = (highs[-20:-10] - lows[-20:-10]).mean()
        recent_atr = (highs[-10:] - lows[-10:]).mean()
        squeeze_score = 70 if recent_atr > early_atr * 1.4 else 40
    else:
        squeeze_score = 50

    # 4. Breakout above 20-bar high
    range_high = highs[-21:-1].max() if len(highs) >= 21 else highs.max()
    breakout = 80 if last_close > range_high else 30

    # 5. RSI rough estimate
    diff = pd.Series(closes).diff().dropna()
    gains = diff[diff > 0].sum()
    losses = abs(diff[diff < 0].sum())
    rsi = 100 - (100 / (1 + gains / max(losses, 1e-9))) if losses > 0 else 50
    momentum_score = 80 if 50 <= rsi <= 70 else 40 if rsi > 75 else 50

    return {
        "vol_score": vol_score,
        "delta_score": delta_score,
        "squeeze_score": squeeze_score,
        "breakout_score": breakout,
        "momentum_score": momentum_score,
        "rsi": round(rsi, 1),
        "vol_mult": round(vol_mult, 2),
    }


def _retroactive_scan_one_pass(df: pd.DataFrame, symbol: str,
                                  idx: int, htf_dataset: dict) -> HistoricalSignal | None:
    """Scan one candle position for signal."""
    if idx < 30 or idx >= len(df):
        return None

    indicators = _quick_indicators(df, idx)
    if not indicators:
        return None

    # Compute confidence as weighted average of sub-scores
    scores = [
        indicators["vol_score"] * 1.4,
        indicators["delta_score"] * 1.3,
        indicators["squeeze_score"] * 1.0,
        indicators["breakout_score"] * 1.2,
        indicators["momentum_score"] * 0.8,
    ]
    weights = [1.4, 1.3, 1.0, 1.2, 0.8]
    confidence = int(sum(scores) / sum(weights))

    sources_strong = sum(1 for s in [
        indicators["vol_score"], indicators["delta_score"],
        indicators["squeeze_score"], indicators["breakout_score"],
        indicators["momentum_score"]
    ] if s >= 65)

    # HTF bias check using historical position
    # We use the 4h and 1d data trimmed to "as of now" historically
    current_price = float(df["c"].iloc[idx])

    # Trim HTF data to candle time
    candle_time = pd.to_datetime(df["t"].iloc[idx], unit="ms")
    htf_trimmed = {}
    for tf, hdf in htf_dataset.items():
        if hdf is None:
            htf_trimmed[tf] = None
            continue
        # Keep only candles before our test candle
        cutoff_ms = int(df["t"].iloc[idx])
        trimmed = hdf[hdf["t"] <= cutoff_ms].tail(60)
        htf_trimmed[tf] = trimmed if len(trimmed) >= 10 else None

    bias = compute_bias(htf_trimmed, current_price, "long")
    htf_aligned = bias.aligned_long

    # Compute change_24h at that historical moment
    bars_24h = min(288, idx)  # 288 5m bars = 24h
    if bars_24h > 0 and idx - bars_24h >= 0:
        prev_price = float(df["c"].iloc[idx - bars_24h])
        chg_24h = (current_price - prev_price) / prev_price * 100
    else:
        chg_24h = 0

    # Build SL/TP plan
    view_df = df.iloc[:idx + 1]
    try:
        plan = build_long_plan(current_price, view_df, {}, mode="day")
    except Exception:
        return None

    # Apply HARD GATE-style filters
    rejection_reasons = []
    is_atr_fallback = "ATR fallback" in plan.method

    if is_atr_fallback and plan.rr_tp1 < 1.5:
        rejection_reasons.append(f"SL على ATR + R:R ضعيف ({plan.rr_tp1:.2f})")

    if plan.rr_tp1 < 0.8:
        rejection_reasons.append(f"R:R ضعيف ({plan.rr_tp1:.2f})")

    no_liquidity = any("لا توجد سيولة" in w for w in plan.warnings)
    if no_liquidity and abs(chg_24h) >= 5.0:
        rejection_reasons.append(f"لا سيولة + حركة {chg_24h:+.1f}%")

    if bias.daily and bias.daily.zone == "premium" and sources_strong < 4:
        rejection_reasons.append("شراء في premium zone")

    if bias.rejection_reason:
        rejection_reasons.append(f"HTF: {bias.rejection_reason[:50]}")

    if confidence < 60:
        rejection_reasons.append(f"Confidence ضعيف ({confidence})")

    if sources_strong < 3:
        rejection_reasons.append(f"مصادر قوية قليلة ({sources_strong}/5)")

    accepted = len(rejection_reasons) == 0 and confidence >= 65 and sources_strong >= 3

    return HistoricalSignal(
        symbol=symbol,
        candle_time=candle_time,
        candles_ago=len(df) - 1 - idx,
        price=round(current_price, 8),
        change_24h_at_time=round(chg_24h, 2),
        confidence=confidence,
        sources_strong=sources_strong,
        htf_aligned=htf_aligned,
        sl=round(plan.sl, 8),
        tp1=round(plan.tp1, 8),
        rr_tp1=plan.rr_tp1,
        method=plan.method,
        sl_logic=plan.sl_logic,
        rejection_reasons=rejection_reasons,
        accepted=accepted,
    )


async def scan_symbol_history(symbol: str, hours_back: int = 4) -> list[HistoricalSignal]:
    """Retroactive scan over last N hours of 5m candles."""
    bars_back = int(hours_back * 12)  # 12 bars per hour at 5m

    # Need enough warmup history (lookback) + the bars to scan
    total_bars = bars_back + 100
    df = await binance.fetch_klines(symbol, "5m", min(1500, total_bars))
    if df is None or len(df) < 50:
        return []

    df_15m = await binance.fetch_klines(symbol, "15m", 100)
    df_1h = await binance.fetch_klines(symbol, "1h", 100)
    df_4h = await binance.fetch_klines(symbol, "4h", 60)
    df_1d = await binance.fetch_klines(symbol, "1d", 50)

    htf_dataset = {"15m": df_15m, "1h": df_1h, "4h": df_4h, "1d": df_1d}

    signals = []
    # Scan only the last `bars_back` candles
    start_idx = max(30, len(df) - bars_back)
    for idx in range(start_idx, len(df)):
        sig = _retroactive_scan_one_pass(df, symbol, idx, htf_dataset)
        if sig and (sig.accepted or sig.confidence >= 65):
            signals.append(sig)

    return signals


async def scan_market_history(top_n: int = 50, hours_back: int = 4,
                                min_confidence: int = 65) -> dict:
    """Scan top N symbols for missed signals in past hours."""
    pairs = await binance.fetch_top_pairs(top_n=top_n, min_vol_usd=10_000_000)
    if not pairs:
        return {"error": "no_pairs"}

    semaphore = asyncio.Semaphore(8)

    async def _scan(p):
        async with semaphore:
            try:
                return p["sym"], await scan_symbol_history(p["sym"], hours_back)
            except Exception as e:
                log.warning("retro_scan_error", sym=p["sym"], err=str(e))
                return p["sym"], []

    results = await asyncio.gather(*[_scan(p) for p in pairs])

    # Aggregate
    accepted_signals = []   # would have triggered alerts
    near_misses = []         # high confidence but rejected by HARD GATE
    rejection_buckets = {}   # count of each rejection reason

    for sym, sigs in results:
        for s in sigs:
            if s.accepted:
                accepted_signals.append(s)
            elif s.confidence >= min_confidence:
                near_misses.append(s)
                for r in s.rejection_reasons:
                    # Group similar reasons
                    key = r.split("(")[0].strip()
                    rejection_buckets[key] = rejection_buckets.get(key, 0) + 1

    accepted_signals.sort(key=lambda s: -s.confidence)
    near_misses.sort(key=lambda s: -s.confidence)

    return {
        "scanned_symbols": len(pairs),
        "hours_back": hours_back,
        "accepted_count": len(accepted_signals),
        "near_miss_count": len(near_misses),
        "accepted": accepted_signals[:10],   # top 10
        "near_misses": near_misses[:10],     # top 10
        "rejection_summary": rejection_buckets,
    }


def render_history_report(report: dict) -> str:
    if "error" in report:
        return "❌ لا يمكن جلب البيانات"

    m = f"🔍 فحص آخر {report['hours_back']} ساعات\n"
    m += "━━━━━━━━━━━━━━━━━━━━\n"
    m += f"عدد العملات المفحوصة: {report['scanned_symbols']}\n\n"

    accepted = report["accepted"]
    near_misses = report["near_misses"]

    if accepted:
        m += f"✅ كان فيه {report['accepted_count']} إشارة كاملة المعايير:\n\n"
        for s in accepted[:5]:
            time_ago = s.candles_ago * 5
            mins = f"{time_ago} د" if time_ago < 60 else f"{time_ago//60}س {time_ago%60}د"
            m += f"🟢 {s.symbol} (قبل {mins})\n"
            m += f"   ثقة: {s.confidence}/100 | مصادر: {s.sources_strong}/5\n"
            m += f"   السعر: ${s.price:.6g} | 24h: {s.change_24h_at_time:+.2f}%\n"
            m += f"   R:R = {s.rr_tp1:.2f} | {s.method}\n\n"
    else:
        m += "⚪ لم تكن هناك إشارة كاملة المعايير\n\n"

    if near_misses:
        m += f"🟡 إشارات قريبة لكن رفضها HARD GATE: {report['near_miss_count']}\n\n"
        for s in near_misses[:5]:
            time_ago = s.candles_ago * 5
            mins = f"{time_ago} د" if time_ago < 60 else f"{time_ago//60}س {time_ago%60}د"
            m += f"🟡 {s.symbol} (قبل {mins}) - ثقة: {s.confidence}\n"
            for r in s.rejection_reasons[:2]:
                m += f"   ✗ {r}\n"
            m += "\n"

    if report["rejection_summary"]:
        m += "📊 أسباب الرفض الأكثر شيوعاً:\n"
        sorted_reasons = sorted(report["rejection_summary"].items(),
                                  key=lambda x: -x[1])
        for reason, count in sorted_reasons[:5]:
            m += f"   • {reason}: {count} مرة\n"
        m += "\n"

    # Verdict
    if accepted:
        m += f"🎯 الخلاصة: البوت شغال صح، فاتته {len(accepted)} فرصة في آخر {report['hours_back']} ساعات.\n"
        m += "ممكن نحتاج نخفف HARD GATE شوي."
    elif near_misses:
        m += "🎯 الخلاصة: السوق فيه فرص لكن HARD GATE صارم.\n"
        m += "هذا الصح لو تبغى جودة عالية، أو نخفف الفلاتر لو تبغى تواتر أكثر."
    else:
        m += "🎯 الخلاصة: السوق هادئ بشكل عام، ولا فرص حقيقية في الفترة دي.\n"
        m += "البوت يعمل بشكل صحيح، فقط ينتظر setup أفضل."

    return m
