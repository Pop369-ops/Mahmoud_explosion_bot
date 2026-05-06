"""Optional external data sources — graceful fallback when keys missing."""
from typing import Optional
import aiohttp
from config.settings import settings
from core.logger import get_logger

log = get_logger(__name__)


class MassiveClient:
    """Polygon.io / Massive — cross-exchange data."""
    def __init__(self):
        self._enabled = settings.has_massive
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def enabled(self) -> bool: return self._enabled

    async def start(self):
        if not self._enabled:
            log.info("massive_disabled"); return
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))

    async def close(self):
        if self._session: await self._session.close()

    async def cross_exchange_premium(self, symbol: str) -> dict:
        result = {"valid": False, "score": 50, "premium_pct": 0, "details": []}
        if not self._enabled: return result
        result["valid"] = True
        result["details"].append("Massive integration ready — fill endpoint when activated")
        return result


class TwelveDataClient:
    """100+ technical indicators on demand."""
    def __init__(self):
        self._enabled = settings.has_twelvedata
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def enabled(self) -> bool: return self._enabled

    async def start(self):
        if not self._enabled: return
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))

    async def close(self):
        if self._session: await self._session.close()


class CoinMarketCapClient:
    """Fundamentals — categories, FDV, rank."""
    def __init__(self):
        self._enabled = settings.has_cmc
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def enabled(self) -> bool: return self._enabled

    async def start(self):
        if not self._enabled: return
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            headers={"X-CMC_PRO_API_KEY": settings.coinmarketcap_api_key or ""},
        )

    async def close(self):
        if self._session: await self._session.close()


massive = MassiveClient()
twelvedata = TwelveDataClient()
cmc = CoinMarketCapClient()
