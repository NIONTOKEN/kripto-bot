"""
Giriş noktası — uvicorn + bot aynı event loop'ta çalışır.
Çalıştırma: python -m app.main
Dashboard:  http://127.0.0.1:8000
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

import aiohttp
import uvicorn

from app.bot import run_bot
from app.config import config
from app.dashboard.server import app as fastapi_app
from app.utils.logger import get_logger

logger = get_logger(__name__)


async def _self_ping_loop() -> None:
    """
    Render ücretsiz plan uyku modunu (15 dakika hareketsizlik) engeller.
    Her 13 dakikada kendi /healthz endpointini çağırır.
    """
    # Servise biraz ısınma süresi ver
    await asyncio.sleep(60)
    service_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    if not service_url:
        # RENDER_EXTERNAL_URL tanımlı değilse localhost dene
        port = config.DASHBOARD_PORT
        service_url = f"http://0.0.0.0:{port}"
    ping_url = f"{service_url}/healthz"
    logger.info(f"Self-ping döngüsü başlatıldı → {ping_url} (her 13 dakika)")
    while True:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get(ping_url) as resp:
                    if resp.status == 200:
                        logger.debug("Self-ping başarılı ✓")
        except Exception as exc:
            logger.debug(f"Self-ping hatası (zararsız): {exc}")
        await asyncio.sleep(780)  # 13 dakika


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

    # ── Bot, Dashboard ve Self-Ping paralel çalıştır ──────────────────────────
    await asyncio.gather(
        server.serve(),
        run_bot(),
        _self_ping_loop(),
        return_exceptions=True,
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
