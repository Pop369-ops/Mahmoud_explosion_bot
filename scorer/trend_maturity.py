"""
Trend Maturity Calculator — measures how 'extended' a trend is.

Used by Gates 16-18 in confidence_engine to reject signals on overextended moves.

Provides:
  - calculate_rsi(): Wilder's RSI
  - calculate_ema(): exponential moving average
  - calculate_price_ema_distance_pct(): how far price is above/below EMA21
  - count_consecutive_trend_bars(): streak of bullish/bearish closes
  - calculate_macd(): MACD line, signal line, histogram
  - calculate_bollinger(): upper, middle, lower bands
"""
import pandas as pd
import numpy as np
from dataclasses import dataclass
from typing import Optional


@dataclass
class TrendMaturityReport:
    rsi_1h: float
    rsi_4h: float
    rsi_1d: float
    rsi_6_1d: float            # short-period RSI for daily (sensitive)
    ema_distance_1h_pct: float # price vs EMA21 on 1h
    ema_distance_4h_pct: float
    consecutive_green_1h: int  # bullish 1h candles in a row
    consecutive_green_4h: int
    consecutive_green_1d: int
    is_overextended_long: bool
    is_overextended_short: bool
    reasons: list[str]


def calculate_rsi(closes: pd.Series, period: int = 14) -> float:
    """Wilder's RSI calculation. Returns latest value."""
    if len(closes) < period + 1:
        return 50.0
    delta = closes.diff().dropna()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-9)
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])


def calculate_ema(closes: pd.Series, period: int) -> float:
    """Latest EMA value."""
    if len(closes) < period:
        return float(closes.iloc[-1]) if len(closes) > 0 else 0.0
    ema = closes.ewm(span=period, adjust=False).mean()
    return float(ema.iloc[-1])


def calculate_ema_series(closes: pd.Series, period: int) -> pd.Series:
    """Full EMA series."""
    return closes.ewm(span=period, adjust=False).mean()


def calculate_price_ema_distance_pct(df: pd.DataFrame, period: int = 21) -> float:
    """How far is current price above/below the EMA, as % of price."""
    if df is None or len(df) < period + 1:
        return 0.0
    closes = df["c"].astype(float)
    ema = calculate_ema(closes, period)
    if ema <= 0:
        return 0.0
    current = float(closes.iloc[-1])
    return ((current - ema) / ema) * 100


def count_consecutive_trend_bars(df: pd.DataFrame, direction: str = "long",
                                    max_lookback: int = 30) -> int:
    """Count consecutive bullish (or bearish) closes."""
    if df is None or len(df) < 2:
        return 0
    closes = df["c"].astype(float)
    opens = df["o"].astype(float)
    count = 0
    for i in range(1, min(max_lookback, len(df)) + 1):
        c = float(closes.iloc[-i])
        o = float(opens.iloc[-i])
        if direction == "long" and c > o:
            count += 1
        elif direction == "short" and c < o:
            count += 1
        else:
            break
    return count


def calculate_macd(closes: pd.Series, fast: int = 12, slow: int = 26,
                    signal: int = 9) -> dict:
    """Returns dict with macd_line, signal_line, histogram (latest values + series)."""
    if len(closes) < slow + signal:
        return {
            "macd_line": 0.0, "signal_line": 0.0, "histogram": 0.0,
            "histogram_series": pd.Series(dtype=float),
            "macd_series": pd.Series(dtype=float),
        }
    ema_fast = closes.ewm(span=fast, adjust=False).mean()
    ema_slow = closes.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return {
        "macd_line": float(macd_line.iloc[-1]),
        "signal_line": float(signal_line.iloc[-1]),
        "histogram": float(histogram.iloc[-1]),
        "histogram_series": histogram,
        "macd_series": macd_line,
    }


def calculate_bollinger(closes: pd.Series, period: int = 20,
                          std_mult: float = 2.0) -> dict:
    """Returns upper, middle, lower bands (latest + series)."""
    if len(closes) < period:
        return {
            "upper": 0.0, "middle": 0.0, "lower": 0.0,
            "upper_series": pd.Series(dtype=float),
            "lower_series": pd.Series(dtype=float),
            "middle_series": pd.Series(dtype=float),
        }
    middle = closes.rolling(window=period).mean()
    std = closes.rolling(window=period).std()
    upper = middle + std_mult * std
    lower = middle - std_mult * std
    return {
        "upper": float(upper.iloc[-1]),
        "middle": float(middle.iloc[-1]),
        "lower": float(lower.iloc[-1]),
        "upper_series": upper,
        "lower_series": lower,
        "middle_series": middle,
    }


def calculate_stoch_rsi(closes: pd.Series, rsi_period: int = 14,
                          stoch_period: int = 14) -> float:
    """Stochastic RSI — 0..100. > 80 = overbought, < 20 = oversold."""
    if len(closes) < rsi_period + stoch_period:
        return 50.0
    delta = closes.diff().dropna()
    gain = delta.where(delta > 0, 0).ewm(alpha=1/rsi_period, adjust=False).mean()
    loss = -delta.where(delta < 0, 0).ewm(alpha=1/rsi_period, adjust=False).mean()
    rs = gain / loss.replace(0, 1e-9)
    rsi = 100 - (100 / (1 + rs))
    rsi_min = rsi.rolling(window=stoch_period).min()
    rsi_max = rsi.rolling(window=stoch_period).max()
    stoch_rsi = ((rsi - rsi_min) / (rsi_max - rsi_min).replace(0, 1e-9)) * 100
    return float(stoch_rsi.iloc[-1])


def calculate_obv(df: pd.DataFrame) -> pd.Series:
    """On-Balance Volume — cumulative volume direction."""
    closes = df["c"].astype(float)
    volume = df["v"].astype(float)
    direction = np.sign(closes.diff().fillna(0))
    obv = (direction * volume).cumsum()
    return obv


def calculate_mfi(df: pd.DataFrame, period: int = 14) -> float:
    """Money Flow Index — RSI weighted by volume."""
    if df is None or len(df) < period + 1:
        return 50.0
    high = df["h"].astype(float)
    low = df["l"].astype(float)
    close = df["c"].astype(float)
    volume = df["v"].astype(float)
    typical = (high + low + close) / 3
    money_flow = typical * volume
    positive = money_flow.where(typical > typical.shift(1), 0)
    negative = money_flow.where(typical < typical.shift(1), 0)
    pos_sum = positive.rolling(window=period).sum()
    neg_sum = negative.rolling(window=period).sum()
    mfr = pos_sum / neg_sum.replace(0, 1e-9)
    mfi = 100 - (100 / (1 + mfr))
    return float(mfi.iloc[-1])


def calculate_vwap(df: pd.DataFrame, lookback_bars: int = 24) -> float:
    """Volume-Weighted Average Price. lookback_bars = recent N candles."""
    if df is None or len(df) < lookback_bars:
        return 0.0
    work = df.tail(lookback_bars)
    high = work["h"].astype(float)
    low = work["l"].astype(float)
    close = work["c"].astype(float)
    volume = work["v"].astype(float)
    typical = (high + low + close) / 3
    vwap_num = (typical * volume).sum()
    vwap_den = volume.sum()
    if vwap_den <= 0:
        return float(close.iloc[-1])
    return float(vwap_num / vwap_den)


def analyze_trend_maturity(snap) -> TrendMaturityReport:
    """Compute full maturity report from a MarketSnapshot."""
    reasons = []

    # RSI on multiple timeframes
    rsi_1h = 50.0
    rsi_4h = 50.0
    rsi_1d = 50.0
    rsi_6_1d = 50.0

    if snap.klines_1h is not None and len(snap.klines_1h) >= 15:
        rsi_1h = calculate_rsi(snap.klines_1h["c"].astype(float), 14)
    if snap.klines_4h is not None and len(snap.klines_4h) >= 15:
        rsi_4h = calculate_rsi(snap.klines_4h["c"].astype(float), 14)
    if snap.klines_1d is not None and len(snap.klines_1d) >= 15:
        rsi_1d = calculate_rsi(snap.klines_1d["c"].astype(float), 14)
        rsi_6_1d = calculate_rsi(snap.klines_1d["c"].astype(float), 6)

    # EMA distance
    ema_dist_1h = calculate_price_ema_distance_pct(snap.klines_1h, 21)
    ema_dist_4h = calculate_price_ema_distance_pct(snap.klines_4h, 21)

    # Consecutive bars (long direction)
    cons_1h = count_consecutive_trend_bars(snap.klines_1h, "long")
    cons_4h = count_consecutive_trend_bars(snap.klines_4h, "long")
    cons_1d = count_consecutive_trend_bars(snap.klines_1d, "long")

    # Overextension judgment for LONG
    is_over_long = False
    if rsi_1d > 75 or rsi_6_1d > 85:
        is_over_long = True
        reasons.append(f"RSI 1d ذروة شراء ({rsi_1d:.0f}/{rsi_6_1d:.0f})")
    if rsi_1h > 75 and rsi_4h > 70:
        is_over_long = True
        reasons.append(f"RSI multi-TF ذروة شراء ({rsi_1h:.0f}/{rsi_4h:.0f})")
    if ema_dist_4h > 15:
        is_over_long = True
        reasons.append(f"السعر متمدد +{ema_dist_4h:.1f}% فوق EMA21 (4h)")
    if cons_1d >= 5:
        is_over_long = True
        reasons.append(f"{cons_1d} شموع يومية صاعدة بلا تصحيح")
    if cons_4h >= 7:
        is_over_long = True
        reasons.append(f"{cons_4h} شموع 4h صاعدة")

    # Overextension judgment for SHORT (mirror)
    is_over_short = False
    if rsi_1d < 25 or rsi_6_1d < 15:
        is_over_short = True
    if rsi_1h < 25 and rsi_4h < 30:
        is_over_short = True
    if ema_dist_4h < -15:
        is_over_short = True

    cons_red_1d = count_consecutive_trend_bars(snap.klines_1d, "short")
    if cons_red_1d >= 5:
        is_over_short = True

    return TrendMaturityReport(
        rsi_1h=rsi_1h, rsi_4h=rsi_4h, rsi_1d=rsi_1d, rsi_6_1d=rsi_6_1d,
        ema_distance_1h_pct=ema_dist_1h,
        ema_distance_4h_pct=ema_dist_4h,
        consecutive_green_1h=cons_1h,
        consecutive_green_4h=cons_4h,
        consecutive_green_1d=cons_1d,
        is_overextended_long=is_over_long,
        is_overextended_short=is_over_short,
        reasons=reasons,
    )
