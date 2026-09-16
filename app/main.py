"""
Giriş noktası — uvicorn + bot aynı event loop'ta çalışır.
Çalıştırma: python -m app.main
Dashboard:  http://127.0.0.1:8000
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

import uvicorn

from app.bot import run_bot
from app.config import config
from app.dashboard.server import app as fastapi_app
from app.utils.logger import get_logger

logger = get_logger(__name__)


async def main() -> None:
    """Uvicorn server + trading bot aynı event loop'ta başlatılır."""

    # ── uvicorn config ────────────────────────────────────────────────────────
    uv_config = uvicorn.Config(
        app=fastapi_app,
        host=config.DASHBOARD_HOST,
        port=config.DASHBOARD_PORT,
        log_level="warning",   # uvicorn loglarını sustur
        access_log=False,
    )
    server = uvicorn.Server(uv_config)

    # ── Graceful shutdown ─────────────────────────────────────────────────────
    loop = asyncio.get_running_loop()

    def _signal_handler() -> None:
        logger.info("Kapatma sinyali alındı...")
        server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except (NotImplementedError, RuntimeError):
            # Windows'ta SIGTERM çalışmaz
            pass

    logger.info(f"Dashboard: http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}")

    # ── Bot ve Dashboard paralel çalıştır ─────────────────────────────────────
    await asyncio.gather(
        server.serve(),
        run_bot(),
        return_exceptions=True,
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
