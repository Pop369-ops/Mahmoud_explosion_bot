"""
EXPLOSION_BOT v4 — entry point.
Run: python main.py
"""
import asyncio
import sys
import signal as os_signal
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes,
)

from config.settings import settings
from core.logger import setup_logging, get_logger

setup_logging()
log = get_logger("main")


async def _post_init(app):
    """Runs after the application starts. Initialize async resources."""
    from data_sources.binance import binance
    from data_sources.whale_alert import whale_alert
    from data_sources.external import massive, twelvedata, cmc
    from storage.db import store

    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        log.warning("delete_webhook_failed", err=str(e))

    await binance.start()
    await whale_alert.start()
    await massive.start()
    await twelvedata.start()
    await cmc.start()
    await store.start()

    # Init performance tracker
    try:
        from risk.performance import PerformanceTracker
        perf_tracker = PerformanceTracker(settings.db_path)
        await perf_tracker.init()
    except Exception as e:
        log.warning("perf_init_error", err=str(e))

    # Confirm Binance connection
    try:
        pairs = await binance.fetch_top_pairs(top_n=5, min_vol_usd=1_000_000)
        binance_status = f"✅ ({len(pairs)} pairs found)"
    except Exception as e:
        binance_status = f"❌ ({type(e).__name__})"

    print("=" * 60)
    print("  💥 EXPLOSION_BOT v4 — Running")
    print("=" * 60)
    print(f"  Binance        : {binance_status}")
    print(f"  Whale Alert    : {'✅ enabled' if whale_alert.enabled else '⚠️ disabled (no key)'}")
    print(f"  Massive        : {'✅ enabled' if massive.enabled else '⚠️ disabled (no key)'}")
    print(f"  TwelveData     : {'✅ enabled' if twelvedata.enabled else '⚠️ disabled (no key)'}")
    print(f"  CoinMarketCap  : {'✅ enabled' if cmc.enabled else '⚠️ disabled (no key)'}")
    print(f"  AI Analysis    : {'✅ enabled' if settings.has_ai else '⚠️ disabled (no key)'}")
    print(f"  Top N coins    : {settings.scan_top_n}")
    print(f"  Min confidence : {settings.min_confidence}")
    print(f"  Min sources    : {settings.min_sources_agree}")
    print(f"  Scan interval  : {settings.scan_interval}s")
    print(f"  Monitor every  : {settings.monitor_interval}s")
    print("=" * 60)
    print("  Send /start in Telegram to begin")
    print("=" * 60)

    # Start monitor for tracked trades
    from scanner.orchestrator import monitor_trades_callback
    app.job_queue.run_repeating(
        monitor_trades_callback,
        interval=settings.monitor_interval,
        first=30,
        name="trade_monitor",
    )


async def _post_shutdown(app):
    from data_sources.binance import binance
    from data_sources.whale_alert import whale_alert
    from data_sources.external import massive, twelvedata, cmc
    from storage.db import store

    log.info("shutting_down")
    await binance.close()
    await whale_alert.close()
    await massive.close()
    await twelvedata.close()
    await cmc.close()
    await store.close()


def main():
    if not settings.bot_token or settings.bot_token == "YOUR_TOKEN_HERE":
        print("=" * 60)
        print("  ❌ ERROR: BOT_TOKEN غير موجود")
        print("  أضفه في Railway → Variables → BOT_TOKEN")
        print("=" * 60)
        sys.exit(1)

    from ui.handlers import (
        cmd_start, cmd_help, cmd_menu, cmd_status,
        cmd_settings, cmd_scan, cmd_test,
        cmd_capital, cmd_risk, cmd_perf, cmd_pause, cmd_resume,
        cmd_backtest, cmd_sentiment,
    )
    from ui.callback_handlers import handle_callback

    app = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("test", cmd_test))
    # Risk management
    app.add_handler(CommandHandler("capital", cmd_capital))
    app.add_handler(CommandHandler("risk", cmd_risk))
    app.add_handler(CommandHandler("perf", cmd_perf))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    # Advanced analytics
    app.add_handler(CommandHandler("backtest", cmd_backtest))
    app.add_handler(CommandHandler("sentiment", cmd_sentiment))

    # Callbacks (inline buttons)
    app.add_handler(CallbackQueryHandler(handle_callback))

    log.info("bot_starting")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
