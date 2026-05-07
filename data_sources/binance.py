"""Async Binance Futures client. Replaces v3 sync requests."""
import asyncio
from typing import Optional
import aiohttp
import pandas as pd
from config.settings import settings
from core.logger import get_logger

log = get_logger(__name__)


class BinanceClient:
    def __init__(self, max_concurrent: int = 20):
        self._session: Optional[aiohttp.ClientSession] = None
        self._sem = asyncio.Semaphore(max_concurrent)

    async def start(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20, connect=6),
                headers={"User-Agent": "ExplosionBot/4.0"},
            )

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get(self, base: str, path: str, params: Optional[dict] = None) -> Optional[dict]:
        if self._session is None: await self.start()
        url = f"{base}{path}"
        async with self._sem:
            try:
                async with self._session.get(url, params=params) as r:
                    if r.status == 200: return await r.json()
                    log.warning("binance_http", url=path, status=r.status)
                    return None
            except asyncio.TimeoutError:
                log.warning("binance_timeout", url=path)
                return None
            except Exception as e:
                log.warning("binance_error", url=path, err=str(e))
                return None

    async def fetch_top_pairs(self, top_n: int = 100, min_vol_usd: float = 500_000) -> list[dict]:
        data = await self._get(settings.binance_rest, "/fapi/v1/ticker/24hr")
        if not data: return []
        out = []
        for d in data:
            sym = d.get("symbol", "")
            if not sym.endswith("USDT") or sym in settings.excluded_pairs: continue
            try:
                vol = float(d.get("quoteVolume", 0))
                if vol < min_vol_usd: continue
                out.append({
                    "sym": sym, "price": float(d.get("lastPrice", 0)),
                    "chg_24h": float(d.get("priceChangePercent", 0)),
                    "vol_24h": vol,
                })
            except (TypeError, ValueError): continue
        out.sort(key=lambda x: x["vol_24h"], reverse=True)
        return out[:top_n] if top_n > 0 else out

    async def fetch_klines(self, symbol: str, interval: str = "5m", limit: int = 80) -> Optional[pd.DataFrame]:
        data = await self._get(settings.binance_rest, "/fapi/v1/klines",
                                {"symbol": symbol, "interval": interval, "limit": limit})
        if not data: return None
        try:
            df = pd.DataFrame(data, columns=["t","o","h","l","c","v","ct","qv","n","bq","bqv","_"])
            for col in ("o","h","l","c","v","qv","bq","bqv"):
                df[col] = pd.to_numeric(df[col], errors="coerce")
            return df
        except Exception as e:
            log.warning("klines_parse_error", symbol=symbol, err=str(e))
            return None

    async def fetch_order_book(self, symbol: str, limit: int = 50) -> Optional[dict]:
        data = await self._get(settings.binance_rest, "/fapi/v1/depth",
                                {"symbol": symbol, "limit": limit})
        if not data: return None
        try:
            return {
                "bids": [[float(p), float(q)] for p, q in data.get("bids", [])],
                "asks": [[float(p), float(q)] for p, q in data.get("asks", [])],
            }
        except Exception: return None

    async def fetch_funding_rate(self, symbol: str) -> Optional[dict]:
        data = await self._get(settings.binance_rest, "/fapi/v1/premiumIndex",
                                {"symbol": symbol})
        if not data: return None
        try:
            return {
                "funding_rate": float(data.get("lastFundingRate", 0)),
                "mark_price": float(data.get("markPrice", 0)),
            }
        except Exception: return None

    async def fetch_funding_history(self, symbol: str, limit: int = 30) -> list[dict]:
        data = await self._get(settings.binance_rest, "/fapi/v1/fundingRate",
                                {"symbol": symbol, "limit": limit})
        if not data: return []
        try:
            return [{"ts": int(d["fundingTime"]), "rate": float(d["fundingRate"])} for d in data]
        except Exception: return []

    async def fetch_open_interest(self, symbol: str) -> Optional[float]:
        """Current open interest for symbol."""
        data = await self._get(settings.binance_rest, "/fapi/v1/openInterest",
                                {"symbol": symbol})
        if not data: return None
        try: return float(data.get("openInterest", 0))
        except (TypeError, ValueError): return None

    async def fetch_oi_history(self, symbol: str, period: str = "5m",
                                limit: int = 12) -> list[dict]:
        """Open interest history. period: 5m/15m/30m/1h/2h/4h/6h/12h/1d"""
        data = await self._get(settings.binance_rest, "/futures/data/openInterestHist",
                                {"symbol": symbol, "period": period, "limit": limit})
        if not data: return []
        try:
            return [{
                "ts": int(d["timestamp"]),
                "oi": float(d["sumOpenInterest"]),
                "oi_value": float(d["sumOpenInterestValue"]),
            } for d in data]
        except Exception: return []

    async def fetch_agg_trades(self, symbol: str, limit: int = 500) -> list[dict]:
        data = await self._get(settings.binance_rest, "/fapi/v1/aggTrades",
                                {"symbol": symbol, "limit": limit})
        if not data: return []
        try:
            return [{
                "ts": int(d["T"]), "price": float(d["p"]),
                "qty": float(d["q"]), "is_buy": not bool(d["m"]),
            } for d in data]
        except Exception: return []


binance = BinanceClient(max_concurrent=20)
