"""Runtime state — trades, cooldowns, user prefs, pending signals."""
import asyncio, time
from typing import Optional
from core.models import Trade, Mode
from core.logger import get_logger

log = get_logger(__name__)


class StateManager:
    def __init__(self):
        self._trades: dict[int, dict[str, Trade]] = {}
        self._cooldowns: dict[int, dict[str, float]] = {}
        self._user_cfg: dict[int, dict] = {}
        self._last_results: dict[int, list] = {}
        self._pending_signals: dict[int, dict[str, dict]] = {}
        self._capital_reminded: dict[int, float] = {}  # chat_id -> last reminder timestamp
        self._lock = asyncio.Lock()

    def should_remind_capital(self, chat_id: int, cooldown_hours: int = 6) -> bool:
        """Has the capital reminder NOT been sent in last N hours?"""
        last = self._capital_reminded.get(chat_id, 0)
        return (time.time() - last) > (cooldown_hours * 3600)

    def mark_capital_reminded(self, chat_id: int):
        self._capital_reminded[chat_id] = time.time()

    async def add_trade(self, trade: Trade):
        async with self._lock:
            self._trades.setdefault(trade.chat_id, {})[trade.symbol] = trade
            log.info("trade_added", chat=trade.chat_id, symbol=trade.symbol)

    async def remove_trade(self, chat_id: int, symbol: str) -> Optional[Trade]:
        async with self._lock:
            trade = self._trades.get(chat_id, {}).pop(symbol, None)
            if trade:
                log.info("trade_removed", chat=chat_id, symbol=symbol)
            return trade

    def get_trade(self, chat_id: int, symbol: str) -> Optional[Trade]:
        return self._trades.get(chat_id, {}).get(symbol)

    def get_trades(self, chat_id: int) -> dict[str, Trade]:
        return dict(self._trades.get(chat_id, {}))

    def has_trade(self, chat_id: int, symbol: str) -> bool:
        return symbol in self._trades.get(chat_id, {})

    def in_cooldown(self, chat_id: int, symbol: str, cooldown_sec: int) -> bool:
        last = self._cooldowns.get(chat_id, {}).get(symbol, 0)
        return (time.time() - last) < cooldown_sec

    def mark_alerted(self, chat_id: int, symbol: str):
        self._cooldowns.setdefault(chat_id, {})[symbol] = time.time()

    async def add_pending(self, chat_id: int, symbol: str, signal_dict: dict):
        async with self._lock:
            self._pending_signals.setdefault(chat_id, {})[symbol] = {
                **signal_dict, "created_at": time.time(),
            }

    def get_pending(self, chat_id: int, symbol: str) -> Optional[dict]:
        return self._pending_signals.get(chat_id, {}).get(symbol)

    async def remove_pending(self, chat_id: int, symbol: str):
        async with self._lock:
            self._pending_signals.get(chat_id, {}).pop(symbol, None)

    async def cleanup_pending(self, max_age_sec: int = 7200):
        async with self._lock:
            now = time.time()
            for chat_id in list(self._pending_signals.keys()):
                for sym in list(self._pending_signals[chat_id].keys()):
                    if now - self._pending_signals[chat_id][sym].get("created_at", 0) > max_age_sec:
                        self._pending_signals[chat_id].pop(sym)

    def get_user_cfg(self, chat_id: int) -> dict:
        from config.settings import settings
        if chat_id not in self._user_cfg:
            self._user_cfg[chat_id] = {
                "min_confidence": settings.min_confidence,
                "min_sources": settings.min_sources_agree,
                "mode": Mode(settings.default_mode),
                "auto_scan": False,
                "ai_enabled": settings.has_ai,
            }
        return self._user_cfg[chat_id]

    def update_user_cfg(self, chat_id: int, **kwargs):
        cfg = self.get_user_cfg(chat_id)
        cfg.update(kwargs)

    def set_last_results(self, chat_id: int, results: list):
        self._last_results[chat_id] = results

    def get_last_results(self, chat_id: int) -> list:
        return self._last_results.get(chat_id, [])


state = StateManager()
