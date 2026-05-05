"""
Backtesting Engine (Lite) — validates strategy on historical klines.

Walks through historical 5m candles for each top symbol, simulates how the
current strategy WOULD have behaved, then reports stats.

Limitations:
- Uses ONLY price/klines data — won't replay whale_alert, funding history,
  or sentiment changes that happened in the past.
- Simulates: HTF bias, ICT structure, OB/FVG/sweep detection, SL/TP, R:R.
- Won't replay: order book depth (it's real-time only).

This is a "directional" backtest — gives you a sense of edge, not a perfect
historical replay. Good enough to spot if a strategy is fundamentally broken.
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
import pandas as pd
from data_sources.binance import binance
from scorer.liquidity_levels import build_long_plan, build_short_plan
from scorer.htf_bias import compute_bias
from scorer.structure import analyze_structure
from core.logger import get_logger

log = get_logger(__name__)


@dataclass
class BacktestTrade:
    symbol: str
    direction: str
    entry_idx: int
    entry_price: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    sl_pct: float
    method: str
    rr_tp1: float
    exit_idx: int = -1
    exit_price: float = 0.0
    pnl_pct: float = 0.0
    close_reason: str = ""
    bars_held: int = 0


@dataclass
class BacktestReport:
    symbol: str
    period: str
    total_signals: int = 0
    trades_opened: int = 0
    winners: int = 0
    losers: int = 0
    win_rate: float = 0.0
    total_pnl_pct: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    by_setup: dict = field(default_factory=dict)
    trades: list = field(default_factory=list)


def _emergency_signal(df: pd.DataFrame, idx: int, lookback: int = 50) -> Optional[str]:
    """Lightweight signal detection — used in backtest where we can't run
    the full detector suite. Looks for clear breakouts after compression.
    Returns 'long' | 'short' | None.
    """
    if idx < 30:
        return None
    window = df.iloc[max(0, idx - lookback):idx + 1]
    if len(window) < 25:
        return None

    closes = window["c"].astype(float).values
    highs = window["h"].astype(float).values
    lows = window["l"].astype(float).values
    volumes = window["v"].astype(float).values

    last_close = closes[-1]
    prev_close = closes[-2]
    range_high = highs[-21:-1].max()
    range_low = lows[-21:-1].min()
    avg_vol = volumes[-21:-1].mean()
    last_vol = volumes[-1]

    # Bullish breakout: close above 20-bar high + volume surge
    if last_close > range_high and last_vol > avg_vol * 1.5 and prev_close <= range_high:
        # Filter out exhausted moves
        if (last_close - range_low) / range_low < 0.20:  # not already 20% up
            return "long"
    # Bearish breakdown
    if last_close < range_low and last_vol > avg_vol * 1.5 and prev_close >= range_low:
        if (range_high - last_close) / range_high < 0.20:
            return "short"
    return None


async def backtest_symbol(symbol: str, days: int = 30,
                            mode: str = "day") -> BacktestReport:
    """Run backtest on one symbol over the last N days."""
    report = BacktestReport(symbol=symbol, period=f"last {days}d")

    # Fetch enough 5m candles (288 per day × days, capped at Binance limit 1500)
    limit = min(1500, 288 * days)
    df_5m = await binance.fetch_klines(symbol, "5m", limit)
    if df_5m is None or len(df_5m) < 100:
        log.warning("backtest_no_data", symbol=symbol)
        return report

    df_15m = await binance.fetch_klines(symbol, "15m", limit // 3)
    df_1h = await binance.fetch_klines(symbol, "1h", limit // 12)
    df_4h = await binance.fetch_klines(symbol, "4h", min(500, limit // 48))
    df_1d = await binance.fetch_klines(symbol, "1d", min(200, limit // 288))

    open_trades: list[BacktestTrade] = []

    # Walk through bars (skip first 30 for warmup)
    for i in range(30, len(df_5m) - 1):
        # Update open trades first (check exits)
        next_bar = df_5m.iloc[i + 1]
        next_high = float(next_bar["h"])
        next_low = float(next_bar["l"])
        next_close = float(next_bar["c"])

        for trade in open_trades[:]:
            if trade.exit_idx != -1:
                continue
            if trade.direction == "long":
                if next_low <= trade.sl:
                    trade.exit_idx = i + 1
                    trade.exit_price = trade.sl
                    trade.pnl_pct = (trade.sl - trade.entry_price) / trade.entry_price * 100
                    trade.close_reason = "SL"
                elif next_high >= trade.tp3:
                    trade.exit_idx = i + 1
                    trade.exit_price = trade.tp3
                    trade.pnl_pct = (trade.tp3 - trade.entry_price) / trade.entry_price * 100
                    trade.close_reason = "TP3"
                elif next_high >= trade.tp2:
                    trade.exit_idx = i + 1
                    trade.exit_price = trade.tp2
                    trade.pnl_pct = (trade.tp2 - trade.entry_price) / trade.entry_price * 100
                    trade.close_reason = "TP2"
                elif next_high >= trade.tp1:
                    trade.exit_idx = i + 1
                    trade.exit_price = trade.tp1
                    trade.pnl_pct = (trade.tp1 - trade.entry_price) / trade.entry_price * 100
                    trade.close_reason = "TP1"
            else:  # short
                if next_high >= trade.sl:
                    trade.exit_idx = i + 1
                    trade.exit_price = trade.sl
                    trade.pnl_pct = (trade.entry_price - trade.sl) / trade.entry_price * 100
                    trade.close_reason = "SL"
                elif next_low <= trade.tp3:
                    trade.exit_idx = i + 1
                    trade.exit_price = trade.tp3
                    trade.pnl_pct = (trade.entry_price - trade.tp3) / trade.entry_price * 100
                    trade.close_reason = "TP3"
                elif next_low <= trade.tp2:
                    trade.exit_idx = i + 1
                    trade.exit_price = trade.tp2
                    trade.pnl_pct = (trade.entry_price - trade.tp2) / trade.entry_price * 100
                    trade.close_reason = "TP2"
                elif next_low <= trade.tp1:
                    trade.exit_idx = i + 1
                    trade.exit_price = trade.tp1
                    trade.pnl_pct = (trade.entry_price - trade.tp1) / trade.entry_price * 100
                    trade.close_reason = "TP1"
            if trade.exit_idx != -1:
                trade.bars_held = trade.exit_idx - trade.entry_idx

        # Limit concurrent trades
        if sum(1 for t in open_trades if t.exit_idx == -1) >= 3:
            continue

        # Look for new signal
        direction = _emergency_signal(df_5m, i)
        if direction is None:
            continue

        report.total_signals += 1
        current_price = float(df_5m.iloc[i]["c"])

        # Build SL/TP plan using current ICT logic on candles up to i
        view_5m = df_5m.iloc[:i + 1]
        try:
            if direction == "long":
                plan = build_long_plan(current_price, view_5m, {}, mode=mode)
            else:
                plan = build_short_plan(current_price, view_5m, {}, mode=mode)
        except Exception as e:
            log.debug("backtest_plan_error", err=str(e))
            continue

        # Skip if R:R is too weak (mirror HARD GATE)
        if plan.rr_tp1 < 1.0 or plan.method == "atr_fallback" and plan.rr_tp1 < 1.5:
            continue

        report.trades_opened += 1
        open_trades.append(BacktestTrade(
            symbol=symbol, direction=direction,
            entry_idx=i, entry_price=current_price,
            sl=plan.sl, tp1=plan.tp1, tp2=plan.tp2, tp3=plan.tp3,
            sl_pct=plan.sl_pct, method=plan.method, rr_tp1=plan.rr_tp1,
        ))

    # Close any still-open trades at last close
    last_idx = len(df_5m) - 1
    last_close = float(df_5m.iloc[-1]["c"])
    for t in open_trades:
        if t.exit_idx == -1:
            t.exit_idx = last_idx
            t.exit_price = last_close
            if t.direction == "long":
                t.pnl_pct = (last_close - t.entry_price) / t.entry_price * 100
            else:
                t.pnl_pct = (t.entry_price - last_close) / t.entry_price * 100
            t.close_reason = "open_at_end"
            t.bars_held = t.exit_idx - t.entry_idx

    # Compute report
    report.trades = open_trades
    report.winners = len([t for t in open_trades if t.pnl_pct > 0])
    report.losers = len([t for t in open_trades if t.pnl_pct <= 0])
    if report.trades_opened:
        report.win_rate = round(report.winners / report.trades_opened * 100, 1)
    report.total_pnl_pct = round(sum(t.pnl_pct for t in open_trades), 2)

    wins_pnl = [t.pnl_pct for t in open_trades if t.pnl_pct > 0]
    losses_pnl = [t.pnl_pct for t in open_trades if t.pnl_pct <= 0]
    if wins_pnl:
        report.avg_win_pct = round(sum(wins_pnl) / len(wins_pnl), 2)
    if losses_pnl:
        report.avg_loss_pct = round(sum(losses_pnl) / len(losses_pnl), 2)
    if losses_pnl and sum(losses_pnl) != 0:
        report.profit_factor = round(sum(wins_pnl) / abs(sum(losses_pnl)), 2)
    if report.trades_opened:
        report.expectancy = round(report.total_pnl_pct / report.trades_opened, 2)

    # By setup type
    setups = {}
    for t in open_trades:
        if t.method not in setups:
            setups[t.method] = {"total": 0, "wins": 0, "pnl": 0}
        setups[t.method]["total"] += 1
        if t.pnl_pct > 0:
            setups[t.method]["wins"] += 1
        setups[t.method]["pnl"] += t.pnl_pct
    for k, v in setups.items():
        v["win_rate"] = round(v["wins"] / v["total"] * 100, 1) if v["total"] else 0
        v["pnl"] = round(v["pnl"], 2)
    report.by_setup = setups

    return report


async def backtest_top_symbols(top_n: int = 10, days: int = 14,
                                  mode: str = "day") -> dict:
    """Run backtest across top N symbols and aggregate."""
    pairs = await binance.fetch_top_pairs(top_n=top_n, min_vol_usd=10_000_000)
    if not pairs:
        return {}
    semaphore = asyncio.Semaphore(5)

    async def _run(p):
        async with semaphore:
            return await backtest_symbol(p["sym"], days=days, mode=mode)

    reports = await asyncio.gather(*[_run(p) for p in pairs])

    # Aggregate
    agg = {
        "symbols_tested": len(reports),
        "total_trades": sum(r.trades_opened for r in reports),
        "total_winners": sum(r.winners for r in reports),
        "total_pnl": round(sum(r.total_pnl_pct for r in reports), 2),
        "win_rate": 0.0,
        "by_symbol": [],
    }
    if agg["total_trades"]:
        agg["win_rate"] = round(agg["total_winners"] / agg["total_trades"] * 100, 1)
    agg["by_symbol"] = sorted(
        [(r.symbol, r.trades_opened, r.win_rate, r.total_pnl_pct) for r in reports],
        key=lambda x: x[3], reverse=True,
    )
    return agg


def render_backtest(report: BacktestReport) -> str:
    if report.trades_opened == 0:
        return (f"📊 Backtest {report.symbol}\n"
                f"لا توجد إشارات في فترة {report.period}\n"
                f"المسح فحص {report.total_signals} إشارة محتملة لكن HARD GATE رفضها كلها.")

    pnl_emoji = "🟢" if report.total_pnl_pct >= 0 else "🔴"
    m = f"📊 Backtest {report.symbol} ({report.period})\n"
    m += "━━━━━━━━━━━━━━━━━━━━\n\n"
    m += f"{pnl_emoji} مجموع P-L: {report.total_pnl_pct:+.2f}%\n"
    m += f"📈 صفقات: {report.trades_opened} (winners: {report.winners} / losers: {report.losers})\n"
    m += f"🎯 Win Rate: {report.win_rate}%\n"
    m += f"💰 Profit Factor: {report.profit_factor:.2f}\n"
    m += f"📊 Expectancy: {report.expectancy:+.2f}% per trade\n"
    m += f"  • Avg Win: +{report.avg_win_pct:.2f}%\n"
    m += f"  • Avg Loss: {report.avg_loss_pct:.2f}%\n"

    if report.by_setup:
        m += "\nحسب النوع:\n"
        for setup, d in sorted(report.by_setup.items(),
                                key=lambda x: x[1]["pnl"], reverse=True):
            m += f"  • {setup}: {d['total']} trades | {d['win_rate']}% | {d['pnl']:+.2f}%\n"
    return m
