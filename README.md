# EXPLOSION_BOT v4

Crypto signal Telegram bot for Binance Futures — rebuilt from v3 with modular architecture, async data fetching, and multi-source confidence scoring.

## What's new vs v3

| | v3 | v4 |
|---|---|---|
| **Signal accuracy** | ~10% (90% false) | Target ~50-65% |
| **Tracking** | Auto (forced) | Manual button |
| **Architecture** | 1 file, 2951 lines | Modular, ~25 files |
| **Data sources** | Binance only | Binance + Whale Alert + (optional) Massive/12data/CMC |
| **Concurrency** | sync `requests` | `asyncio` + `aiohttp` |
| **Scan time (top 100)** | 3-5 min | 8-15 sec |
| **Indicators** | 7 lagging | 6 leading + 4 confirming |
| **Score transparency** | "score: 70/100" | "4/6 sources agreed" + per-source breakdown |
| **State persistence** | None (loses on restart) | SQLite |
| **AI analysis** | None | Claude → Gemini → OpenAI on demand |

## Architecture

```
   ┌─────────────────────────────────────────┐
   │ DATA SOURCES (independent feeds)        │
   │  - Binance Futures (REST)               │
   │  - Whale Alert (on-chain, optional)     │
   │  - Massive/12data/CMC (optional)        │
   └────────────────┬────────────────────────┘
                    ▼
   ┌─────────────────────────────────────────┐
   │ DETECTORS (LEADING — find moves early)  │
   │  - Volume Delta (from aggTrades)        │
   │  - Order Book Imbalance                 │
   │  - Funding Divergence                   │
   │  - Whale Flow                           │
   │  - Squeeze→Breakout                     │
   │  - Pre-Explosion Accumulation           │
   └────────────────┬────────────────────────┘
                    ▼
   ┌─────────────────────────────────────────┐
   │ FILTERS (confirmation)                  │
   │  - Multi-Timeframe (15m + 1h)           │
   │  - BTC Trend                            │
   │  - BTC Correlation                      │
   │  - Liquidity / Risk Gate                │
   └────────────────┬────────────────────────┘
                    ▼
   ┌─────────────────────────────────────────┐
   │ CONFIDENCE ENGINE                       │
   │  Weighted scoring + agreement count     │
   │  → Phase classification + TP/SL (ATR)   │
   └────────────────┬────────────────────────┘
                    ▼
   ┌─────────────────────────────────────────┐
   │ TELEGRAM UI                             │
   │  Manual track button + AI analysis      │
   └─────────────────────────────────────────┘
```

## Setup — Railway deployment

### 1. Push to GitHub
```bash
git init
git add .
git commit -m "v4 initial"
git remote add origin https://github.com/YOUR_USER/explosion_bot_v4.git
git push -u origin main
```

### 2. Create Railway project
- New project → Deploy from GitHub repo
- **Region**: `europe-west4` (us-west2 is blocked by Binance)
- Wait for first deploy

### 3. Add environment variables (Railway → Variables)

**Required:**
- `BOT_TOKEN` — your Telegram bot token from @BotFather

**Highly recommended (improves accuracy ~25%):**
- `WHALE_ALERT_API_KEY` — sign up at https://docs.whale-alert.io/
- `ANTHROPIC_API_KEY` — for AI deep analysis

**Optional (graceful fallback if missing):**
- `MASSIVE_API_KEY`, `TWELVEDATA_API_KEY`, `COINMARKETCAP_API_KEY`
- `GEMINI_API_KEY`, `OPENAI_API_KEY`

**Tuning (have sensible defaults):**
- `SCAN_TOP_N=100` — number of coins to scan per cycle
- `MIN_CONFIDENCE=65` — minimum score to alert
- `MIN_SOURCES_AGREE=4` — min independent sources that must agree
- `ALERT_COOLDOWN=3600` — seconds between alerts for same coin
- `SCAN_INTERVAL=180` — auto-scan frequency in seconds
- `MONITOR_INTERVAL=60` — tracked-trade monitor frequency
- `DEFAULT_MODE=day` — `scalp`, `day`, or `swing`
- `LOG_LEVEL=INFO`

### 4. Verify
- Open the deployment logs in Railway
- You should see the v4 banner with all data sources listed
- Send `/start` in Telegram

## Telegram commands

- `/start` — welcome + main menu
- `/scan` — manual scan now
- `/test SYMBOL` — analyze one coin (e.g. `/test ETH`)
- `/status` — open trades + last scan results
- `/settings` — change mode, confidence threshold, auto-scan
- `/menu` — main menu
- `/help` — usage guide

## Trading modes

| Mode | TF | SL | TP1/2/3 mults | Use case |
|------|-----|-----|---------------|----------|
| `scalp` | 5m+15m | 1.2× ATR | 1.2/2.0/3.0× | Quick in/out, 15min-1h hold |
| `day` (default) | 15m+1h | 1.5× ATR | 1.5/3.0/5.0× | Day trading, 1-8h hold |
| `swing` | 1h+4h | 2.0× ATR | 2.0/4.0/7.0× | Multi-day positions |

## Adding a new detector

```python
# detectors/my_detector.py
from core.models import SourceVerdict, MarketSnapshot

async def detect_my_signal(snap: MarketSnapshot) -> SourceVerdict:
    v = SourceVerdict(name="my_signal", score=50, confidence=0.0)
    # ... your logic ...
    v.score = computed_score
    v.confidence = 0.8
    return v
```

Then register in `scorer/confidence_engine.py` `score_symbol()` and add a weight in `config/settings.py` `source_weights`.

## File structure

```
explosion_bot/
├── main.py                          # entry point
├── config/settings.py               # all env-tunable values
├── core/
│   ├── models.py                    # Signal, Trade, Phase, MarketSnapshot
│   ├── state.py                     # in-memory state manager
│   └── logger.py                    # structlog setup
├── data_sources/
│   ├── binance.py                   # async Binance client
│   ├── whale_alert.py               # whale flow API
│   └── external.py                  # Massive/12data/CMC stubs
├── detectors/                       # leading indicators
│   ├── volume_delta.py
│   ├── order_book.py
│   ├── funding_divergence.py
│   ├── whale_flow.py
│   ├── squeeze_breakout.py
│   └── pre_explosion.py
├── filters/                         # confirmations
│   ├── multi_timeframe.py
│   ├── btc_trend.py
│   ├── btc_correlation.py
│   └── risk_gate.py
├── scorer/confidence_engine.py      # weighted scoring
├── trading/manager.py               # manual track + exit logic
├── ai/analyst.py                    # Claude/Gemini/OpenAI router
├── storage/db.py                    # SQLite persistence
├── ui/
│   ├── alerts.py                    # message templates
│   ├── keyboards.py                 # inline buttons
│   ├── handlers.py                  # /commands
│   └── callback_handlers.py         # button callbacks
└── scanner/orchestrator.py          # main scan loop
```

## Disclaimer

This bot is for educational purposes only. Not financial advice. Always do your own research before trading.
