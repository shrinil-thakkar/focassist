"""
Backend entrypoint.
Starts the FastAPI HTTP server (for agent communication) and the Telegram bot
polling loop in the same asyncio event loop via uvicorn + python-telegram-bot.
"""
import asyncio
import logging
import os
import sys
import threading

import uvicorn

import backend.db as db
from backend.api import app as fastapi_app
from backend.bot import build_app, send_to_owner
from backend import scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("focassist.backend")

API_HOST = os.environ.get("FOCASSIST_API_HOST", "0.0.0.0")
API_PORT = int(os.environ.get("FOCASSIST_API_PORT", "8000"))


async def _run_bot(tg_app) -> None:
    """Run the Telegram bot in polling mode inside the asyncio event loop."""
    await tg_app.initialize()
    await tg_app.start()

    async def _send(text, parse_mode="Markdown"):
        await send_to_owner(tg_app.bot, text, parse_mode)

    scheduler.start(_send)

    await tg_app.updater.start_polling(drop_pending_updates=True)
    log.info("Telegram bot polling started.")

    # Block until shutdown signal
    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()


def _run_api() -> None:
    """Run FastAPI in a thread (uvicorn blocks)."""
    config = uvicorn.Config(fastapi_app, host=API_HOST, port=API_PORT,
                            log_level="info", loop="none")
    server = uvicorn.Server(config)
    # Use a new event loop for the thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(server.serve())


async def main() -> None:
    db.init_db()
    log.info("Database initialized.")

    # Run API in a background thread
    api_thread = threading.Thread(target=_run_api, daemon=True)
    api_thread.start()
    log.info("API server thread started on %s:%d", API_HOST, API_PORT)

    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not telegram_token:
        log.warning("TELEGRAM_BOT_TOKEN not set — running API-only mode (no bot/nudges).")
        # Keep the process alive so the API thread keeps serving
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        return

    tg_app = build_app()
    await _run_bot(tg_app)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down.")
