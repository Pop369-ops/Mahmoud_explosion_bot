"""
Options Flow Analyzer — Deribit BTC/ETH options data.

Free public API (no key needed). Useful indicators:
- Put/Call open interest ratio  → fear vs greed in derivatives
- Max Pain — price level where most options expire worthless (price magnet)
- Put/Call volume — recent flow direction

For altcoins, options data isn't widely available. We use BTC/ETH options as
"market proxy" — when BTC options skew bearish, alts often follow.
"""
import asyncio
import aiohttp
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timezone
from core.logger import get_logger

log = get_logger(__name__)


@dataclass
class OptionsSnapshot:
    underlying: str             # "BTC" or "ETH"
    put_call_oi_ratio: float = 1.0     # >1 = more puts (bearish), <1 = more calls (bullish)
    put_call_volume_ratio: float = 1.0
    max_pain_price: float = 0.0
    current_price: float = 0.0
    distance_to_max_pain_pct: float = 0.0
    sentiment: str = "neutral"   # "fearful" | "complacent" | "neutral"
    advisory: str = ""
    expiry_label: str = ""
    raw_count: int = 0


_DERIBIT_BASE = "https://www.deribit.com/api/v2/public"


async def fetch_deribit_summary(currency: str = "BTC") -> Optional[list]:
    """Get all option instrument summaries for a currency."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.get(
                f"{_DERIBIT_BASE}/get_book_summary_by_currency",
                params={"currency": currency, "kind": "option"},
            ) as r:
                if r.status != 200:
                    return None
                data = await r.json()
                return data.get("result", [])
    except Exception as e:
        log.warning("deribit_fetch_error", err=str(e))
        return None


async def fetch_underlying_price(currency: str = "BTC") -> Optional[float]:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.get(
                f"{_DERIBIT_BASE}/get_index_price",
                params={"index_name": f"{currency.lower()}_usd"},
            ) as r:
                if r.status != 200:
                    return None
                data = await r.json()
                return float(data.get("result", {}).get("index_price", 0))
    except Exception as e:
        log.warning("deribit_price_error", err=str(e))
        return None


def _parse_instrument(name: str) -> Optional[dict]:
    """Parse 'BTC-26DEC25-100000-C' → {expiry, strike, type}"""
    parts = name.split("-")
    if len(parts) < 4:
        return None
    try:
        expiry_str = parts[1]
        strike = float(parts[2])
        opt_type = parts[3].upper()  # 'C' or 'P'
        return {"expiry": expiry_str, "strike": strike, "type": opt_type}
    except (ValueError, IndexError):
        return None


def _compute_max_pain(summaries: list, expiry_filter: Optional[str] = None) -> float:
    """Strike where total option value is minimized (= max pain for holders)."""
    by_strike = {}
    for opt in summaries:
        info = _parse_instrument(opt.get("instrument_name", ""))
        if info is None:
            continue
        if expiry_filter and info["expiry"] != expiry_filter:
            continue
        strike = info["strike"]
        oi = opt.get("open_interest", 0) or 0
        if strike not in by_strike:
            by_strike[strike] = {"call_oi": 0, "put_oi": 0}
        if info["type"] == "C":
            by_strike[strike]["call_oi"] += oi
        else:
            by_strike[strike]["put_oi"] += oi

    if not by_strike:
        return 0.0

    strikes = sorted(by_strike.keys())
    min_pain_strike = strikes[0]
    min_pain_value = float("inf")

    for test_strike in strikes:
        total_pain = 0.0
        for s, data in by_strike.items():
            if s > test_strike:
                # ITM puts (would lose for option seller)
                total_pain += data["put_oi"] * (s - test_strike)
            elif s < test_strike:
                # ITM calls
                total_pain += data["call_oi"] * (test_strike - s)
        if total_pain < min_pain_value:
            min_pain_value = total_pain
            min_pain_strike = test_strike
    return min_pain_strike


def _find_nearest_expiry(summaries: list) -> Optional[str]:
    """Find the next monthly expiry from list."""
    expiries = set()
    for opt in summaries:
        info = _parse_instrument(opt.get("instrument_name", ""))
        if info:
            expiries.add(info["expiry"])
    if not expiries:
        return None
    # Parse and sort by date — Deribit format like "26DEC25"
    parsed = []
    for e in expiries:
        try:
            dt = datetime.strptime(e, "%d%b%y")
            parsed.append((dt, e))
        except ValueError:
            continue
    if not parsed:
        return None
    parsed.sort(key=lambda x: x[0])
    now = datetime.now()
    # First expiry that's at least 7 days out (skip weekly noise)
    for dt, e in parsed:
        if (dt - now).days >= 7:
            return e
    return parsed[0][1]


async def analyze_options(currency: str = "BTC") -> OptionsSnapshot:
    """Analyze BTC or ETH options market."""
    snap = OptionsSnapshot(underlying=currency)

    summaries, price = await asyncio.gather(
        fetch_deribit_summary(currency),
        fetch_underlying_price(currency),
        return_exceptions=True,
    )
    if not isinstance(summaries, list) or not summaries:
        return snap
    if isinstance(price, (int, float)):
        snap.current_price = price

    snap.raw_count = len(summaries)

    # Aggregate OI
    call_oi = put_oi = 0
    call_vol = put_vol = 0
    for opt in summaries:
        info = _parse_instrument(opt.get("instrument_name", ""))
        if not info:
            continue
        oi = opt.get("open_interest", 0) or 0
        vol = opt.get("volume", 0) or 0
        if info["type"] == "C":
            call_oi += oi
            call_vol += vol
        else:
            put_oi += oi
            put_vol += vol

    if call_oi > 0:
        snap.put_call_oi_ratio = round(put_oi / call_oi, 2)
    if call_vol > 0:
        snap.put_call_volume_ratio = round(put_vol / call_vol, 2)

    # Max pain on next monthly expiry
    nearest_expiry = _find_nearest_expiry(summaries)
    if nearest_expiry:
        snap.expiry_label = nearest_expiry
        snap.max_pain_price = _compute_max_pain(summaries, nearest_expiry)
        if snap.current_price > 0 and snap.max_pain_price > 0:
            snap.distance_to_max_pain_pct = round(
                (snap.max_pain_price - snap.current_price) / snap.current_price * 100, 2
            )

    # ─── Sentiment classification ────────────────────────
    if snap.put_call_oi_ratio >= 1.3:
        snap.sentiment = "fearful"
        snap.advisory = (
            f"😰 P/C OI = {snap.put_call_oi_ratio} — متداولو الخيارات يحوّطون "
            f"للهبوط (إشارة contrarian صعودية محتملة)"
        )
    elif snap.put_call_oi_ratio <= 0.6:
        snap.sentiment = "complacent"
        snap.advisory = (
            f"😎 P/C OI = {snap.put_call_oi_ratio} — calls كثيرة، "
            f"تفاؤل مفرط (إشارة contrarian هبوطية محتملة)"
        )
    else:
        snap.sentiment = "neutral"
        snap.advisory = f"😐 P/C OI = {snap.put_call_oi_ratio} (متعادل)"

    # Max pain advisory
    if abs(snap.distance_to_max_pain_pct) > 3 and snap.max_pain_price > 0:
        direction = "أعلى" if snap.distance_to_max_pain_pct > 0 else "أقل"
        snap.advisory += (
            f"\n🎯 Max Pain @ ${snap.max_pain_price:,.0f} "
            f"({direction} بـ {abs(snap.distance_to_max_pain_pct):.1f}% من السعر) "
            f"— احتمال انجذاب السعر له قبل {snap.expiry_label}"
        )
    return snap
