"""BTC correlation filter."""
import pandas as pd
from core.models import SourceVerdict, MarketSnapshot
from data_sources.binance import binance


async def filter_btc_correlation(snap: MarketSnapshot, direction: str = "long") -> SourceVerdict:
    v = SourceVerdict(name="btc_correlation", score=50, confidence=0.0)

    df_btc = await binance.fetch_klines("BTCUSDT", "5m", 30)
    df_sym = snap.klines
    if df_btc is None or df_sym is None or len(df_btc) < 25 or len(df_sym) < 25:
        return v

    btc_returns = df_btc["c"].astype(float).pct_change().tail(20)
    sym_returns = df_sym["c"].astype(float).pct_change().tail(20)
    if len(btc_returns) < 15 or len(sym_returns) < 15:
        return v

    correlation = btc_returns.corr(sym_returns)
    if pd.isna(correlation): correlation = 0

    btc_chg = (df_btc["c"].iloc[-1] - df_btc["c"].iloc[-12]) / df_btc["c"].iloc[-12] * 100
    sym_chg = (df_sym["c"].iloc[-1] - df_sym["c"].iloc[-12]) / df_sym["c"].iloc[-12] * 100

    score = 50
    reasons = []

    if direction == "long":
        if correlation > 0.6 and btc_chg > 0:
            score = 75
            reasons.append(f"✅ متماشية مع BTC الصاعد (corr={correlation:.2f})")
        elif correlation > 0.3 and btc_chg > 0:
            score = 65
        elif btc_chg < -0.5 and sym_chg > 0:
            score = 35
            reasons.append("⚠️ ترتفع رغم هبوط BTC — تحقق من السيولة")
        else:
            score = 55

    v.score = score
    v.reasons = reasons
    v.confidence = 0.7
    v.raw = {"correlation": correlation, "btc_chg_1h": btc_chg, "sym_chg_1h": sym_chg}
    return v
