"""SQLite storage — persists trades and signal history."""
import json
import aiosqlite
from typing import Optional
from core.models import Trade, now_riyadh
from config.settings import settings
from core.logger import get_logger

log = get_logger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    entry REAL, sl REAL, tp1 REAL, tp2 REAL, tp3 REAL,
    confidence INTEGER, phase TEXT, mode TEXT,
    opened_at TEXT, closed_at TEXT,
    peak_price REAL DEFAULT 0,
    tp1_hit INTEGER DEFAULT 0,
    tp2_hit INTEGER DEFAULT 0,
    closed INTEGER DEFAULT 0,
    close_reason TEXT, close_price REAL,
    UNIQUE(chat_id, symbol, opened_at)
);
CREATE TABLE IF NOT EXISTS signals_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER, symbol TEXT,
    phase TEXT, confidence INTEGER, sources_agreed INTEGER,
    price REAL, raw TEXT, created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_chat ON trades(chat_id, closed);
CREATE INDEX IF NOT EXISTS idx_signals_chat ON signals_log(chat_id, created_at);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        self._db: Optional[aiosqlite.Connection] = None

    async def start(self):
        self._db = await aiosqlite.connect(self.path)
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        log.info("storage_ready", path=self.path)

    async def close(self):
        if self._db:
            await self._db.close()

    async def save_trade(self, t: Trade):
        if not self._db: return
        await self._db.execute(
            """INSERT OR REPLACE INTO trades
            (chat_id, symbol, entry, sl, tp1, tp2, tp3, confidence, phase, mode,
             opened_at, peak_price, tp1_hit, tp2_hit, closed, close_reason, close_price)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (t.chat_id, t.symbol, t.entry, t.sl, t.tp1, t.tp2, t.tp3,
             t.confidence, t.phase.value, t.mode.value,
             t.opened_at.isoformat(), t.peak_price,
             int(t.tp1_hit), int(t.tp2_hit), int(t.closed),
             t.close_reason, t.close_price),
        )
        await self._db.commit()

    async def log_signal(self, chat_id: int, symbol: str, phase: str,
                          confidence: int, sources_agreed: int,
                          price: float, raw: dict):
        if not self._db: return
        await self._db.execute(
            """INSERT INTO signals_log
            (chat_id, symbol, phase, confidence, sources_agreed, price, raw, created_at)
            VALUES (?,?,?,?,?,?,?,?)""",
            (chat_id, symbol, phase, confidence, sources_agreed, price,
             json.dumps(raw)[:5000], now_riyadh().isoformat()),
        )
        await self._db.commit()


store = Store(settings.db_path)
