"""Trade manager — manual track + exit analyzer."""
from core.models import Trade, Signal, Phase, MarketSnapshot, now_riyadh
from core.state import state
from data_sources.binance import binance


async def open_trade_from_signal(chat_id: int, signal: Signal) -> Trade:
    trade = Trade(
        chat_id=chat_id, symbol=signal.symbol,
        entry=signal.entry, sl=signal.sl,
        tp1=signal.tp1, tp2=signal.tp2, tp3=signal.tp3,
        confidence=signal.confidence, phase=signal.phase, mode=signal.mode,
        direction=getattr(signal, 'direction', 'long'),
        peak_price=signal.entry,
    )
    await state.add_trade(trade)
    # Record opening with risk manager
    try:
        from risk.daily_limits import risk_manager
        risk_manager.record_trade_opened(chat_id)
    except Exception:
        pass
    return trade


async def record_trade_close(chat_id: int, trade: Trade,
                                exit_price: float, close_reason: str):
    """Persist closed trade to performance log + update risk state."""
    pnl_pct = (exit_price - trade.entry) / trade.entry * 100 if trade.entry > 0 else 0
    direction = getattr(trade, "direction", "long")
    # For short, invert PnL
    if direction == "short":
        pnl_pct = -pnl_pct

    sl_dist = abs(trade.entry - trade.sl)
    pnl_r = pnl_pct / (sl_dist / trade.entry * 100) if (sl_dist > 0 and trade.entry > 0) else 0

    # Duration
    duration = int((now_riyadh() - trade.opened_at).total_seconds() / 60)

    # Update risk manager
    try:
        from risk.daily_limits import risk_manager
        risk_manager.record_trade_result(chat_id, pnl_pct)
    except Exception:
        pass

    # Persist to performance log (legacy)
    try:
        from risk.performance import PerformanceTracker, TradeRecord
        from config.settings import settings
        tracker = PerformanceTracker(settings.db_path)
        await tracker.init()
        rec = TradeRecord(
            chat_id=chat_id, symbol=trade.symbol,
            direction=direction,
            setup_type=getattr(trade, "setup_type", "unknown"),
            entry=trade.entry, sl=trade.sl, tp1=trade.tp1,
            exit_price=exit_price,
            pnl_pct=round(pnl_pct, 2),
            pnl_r=round(pnl_r, 2),
            confidence=trade.confidence,
            sources_agreed=getattr(trade, "sources_agreed", 0),
            duration_minutes=duration,
            closed_at=now_riyadh(),
            close_reason=close_reason,
        )
        await tracker.record(rec)
    except Exception:
        pass

    # ─── ADVANCED TRACKING: update outcome ──
    try:
        from risk.advanced_tracker import advanced_tracker
        if advanced_tracker is not None:
            # Find the open signal for this symbol
            signal_id = await advanced_tracker.find_open_signal(
                chat_id, trade.symbol
            )
            if signal_id:
                # Determine outcome category
                if pnl_pct > 0:
                    if close_reason in ("TP1", "tp1_hit"):
                        outcome = "win_tp1"
                    elif close_reason in ("TP2", "tp2_hit"):
                        outcome = "win_tp2"
                    elif close_reason in ("TP3", "tp3_hit"):
                        outcome = "win_tp3"
                    elif "trailing" in close_reason.lower():
                        outcome = "win_trailing"
                    else:
                        outcome = "win_other"
                else:
                    if close_reason in ("SL", "sl_hit", "exit_sl"):
                        outcome = "loss_sl"
                    else:
                        outcome = "loss_manual"

                await advanced_tracker.update_outcome(
                    signal_id=signal_id,
                    outcome=outcome,
                    actual_pnl_pct=round(pnl_pct, 2),
                    actual_pnl_r=round(pnl_r, 2),
                    duration_minutes=duration,
                )
    except Exception as e:
        from core.logger import get_logger
        get_logger(__name__).debug("advanced_tracker_close_err", err=str(e))


async def analyze_exit(trade: Trade) -> dict:
    result = {"action": "hold", "price": 0, "reason": "", "signals": []}
    df = await binance.fetch_klines(trade.symbol, "5m", 30)
    if df is None or len(df) < 10:
        return result

    price = float(df["c"].iloc[-1])
    result["price"] = price
    if price > trade.peak_price:
        trade.peak_price = price

    pnl_pct = (price - trade.entry) / trade.entry * 100

    if price <= trade.sl:
        result["action"] = "exit_sl"
        result["reason"] = f"🔴 وصل SL — خسارة {pnl_pct:.2f}%"
        return result

    if price >= trade.tp3:
        result["action"] = "exit_now"
        result["reason"] = f"🏆 TP3 تحقق — ربح {pnl_pct:.2f}%"
        return result

    if not trade.tp2_hit and price >= trade.tp2:
        trade.tp2_hit = True
        result["action"] = "exit_partial"
        result["reason"] = f"💰 TP2 تحقق — اخرج 50% (+{pnl_pct:.2f}%)"
        return result

    if not trade.tp1_hit and price >= trade.tp1:
        trade.tp1_hit = True
        result["action"] = "trail"
        result["reason"] = f"💰 TP1 تحقق — رفع SL لنقطة الدخول (+{pnl_pct:.2f}%)"
        return result

    drawdown_from_peak = (trade.peak_price - price) / trade.peak_price * 100 if trade.peak_price > 0 else 0
    if trade.tp1_hit and drawdown_from_peak > 1.5:
        result["action"] = "exit_now"
        result["reason"] = f"📉 تراجع {drawdown_from_peak:.2f}% من القمة"
        return result

    age_hours = (now_riyadh() - trade.opened_at).total_seconds() / 3600
    if age_hours > 24 and pnl_pct < 0.5:
        result["action"] = "exit_partial"
        result["reason"] = f"⏰ مرت 24h بدون حركة (+{pnl_pct:.2f}%)"
        return result

    c = df["c"].astype(float)
    last_5_chg = (c.iloc[-1] - c.iloc[-5]) / c.iloc[-5] * 100
    if last_5_chg < -2.5 and pnl_pct > 2:
        result["action"] = "exit_partial"
        result["reason"] = f"⚠️ هبوط {last_5_chg:.2f}% في 25 دقيقة"
        return result

    return result
