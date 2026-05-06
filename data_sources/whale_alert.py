"""Whale Alert API client. Independent data source #2."""
import time
from typing import Optional
import aiohttp
from config.settings import settings
from core.logger import get_logger

log = get_logger(__name__)

SYMBOL_TO_ASSET = {
    "BTCUSDT": "btc", "ETHUSDT": "eth", "SOLUSDT": "sol", "XRPUSDT": "xrp",
    "ADAUSDT": "ada", "BNBUSDT": "bnb", "DOGEUSDT": "doge", "DOTUSDT": "dot",
    "LINKUSDT": "link", "MATICUSDT": "matic", "TRXUSDT": "trx", "AVAXUSDT": "avax",
    "LTCUSDT": "ltc", "ATOMUSDT": "atom", "UNIUSDT": "uni", "ETCUSDT": "etc",
    "FILUSDT": "fil", "ARBUSDT": "arb", "OPUSDT": "op", "NEARUSDT": "near",
    "APTUSDT": "apt", "HBARUSDT": "hbar", "ICPUSDT": "icp", "VETUSDT": "vet",
    "PEPEUSDT": "pepe", "SHIBUSDT": "shib", "ALGOUSDT": "algo",
}


class WhaleAlertClient:
    BASE = "https://api.whale-alert.io/v1"

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._enabled = settings.has_whale_alert
        self._cache: dict[str, tuple[float, list[dict]]] = {}
        self._cache_ttl = 300

    @property
    def enabled(self) -> bool: return self._enabled

    async def start(self):
        if not self._enabled:
            log.info("whale_alert_disabled")
            return
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get(self, path: str, params: dict) -> Optional[dict]:
        if not self._enabled or self._session is None: return None
        url = f"{self.BASE}{path}"
        params = {**params, "api_key": settings.whale_alert_api_key}
        try:
            async with self._session.get(url, params=params) as r:
                if r.status == 200: return await r.json()
                if r.status == 429: log.warning("whale_alert_rate_limit"); return None
                log.warning("whale_alert_http", status=r.status)
                return None
        except Exception as e:
            log.warning("whale_alert_error", err=str(e))
            return None

    async def fetch_recent(self, asset: str, min_value_usd: int = 1_000_000) -> list[dict]:
        if not self._enabled: return []
        cached = self._cache.get(asset)
        if cached and (time.time() - cached[0]) < self._cache_ttl:
            return cached[1]
        start_ts = int(time.time()) - 3600
        data = await self._get("/transactions", {
            "start": start_ts, "currency": asset,
            "min_value": min_value_usd, "limit": 100,
        })
        txs = data.get("transactions", []) if data else []
        self._cache[asset] = (time.time(), txs)
        return txs

    async def analyze_flow(self, symbol: str) -> dict:
        result = {"valid": False, "score": 0, "net_inflow_usd": 0,
                  "net_outflow_exch_usd": 0, "tx_count": 0, "details": []}
        if not self._enabled: return result

        asset = SYMBOL_TO_ASSET.get(symbol)
        if not asset: return result

        txs = await self.fetch_recent(asset, min_value_usd=500_000)
        if not txs:
            result["valid"] = True
            return result

        net_outflow = 0.0
        net_inflow = 0.0
        whale_buys = 0
        whale_sells = 0
        for tx in txs:
            try:
                amount_usd = float(tx.get("amount_usd", 0))
                from_owner = tx.get("from", {}).get("owner_type", "")
                to_owner = tx.get("to", {}).get("owner_type", "")
                if from_owner == "exchange" and to_owner == "wallet":
                    net_outflow += amount_usd; whale_buys += 1
                elif from_owner == "wallet" and to_owner == "exchange":
                    net_inflow += amount_usd; whale_sells += 1
            except (TypeError, ValueError): continue

        net_balance = net_outflow - net_inflow
        result["valid"] = True
        result["tx_count"] = len(txs)
        result["net_inflow_usd"] = net_inflow
        result["net_outflow_exch_usd"] = net_outflow

        if net_balance > 50_000_000:
            result["score"] = 95
            result["details"].append(f"🐋 خروج {net_balance/1e6:.0f}M$ من البورصات (تراكم ضخم)")
        elif net_balance > 10_000_000:
            result["score"] = 80
            result["details"].append(f"🐋 خروج {net_balance/1e6:.0f}M$ من البورصات")
        elif net_balance > 2_000_000:
            result["score"] = 65
            result["details"].append(f"🐋 خروج صافٍ {net_balance/1e6:.1f}M$")
        elif net_balance > -2_000_000:
            result["score"] = 50
            if whale_buys + whale_sells > 0:
                result["details"].append(f"🐋 نشاط حيتان متوازن ({whale_buys}/{whale_sells})")
        elif net_balance > -10_000_000:
            result["score"] = 30
            result["details"].append(f"⚠️ دخول {abs(net_balance)/1e6:.1f}M$ للبورصات")
        else:
            result["score"] = 10
            result["details"].append(f"🚨 دخول {abs(net_balance)/1e6:.0f}M$ للبورصات (توزيع)")
        return result


whale_alert = WhaleAlertClient()
