"""
Binance Futures WebSocket yöneticisi.
Kline stream'leri ve user data stream'ini yönetir.
Her sembol için gerçek zamanlı kline verisi alınır.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Callable, Dict, List, Optional

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from app.config import config
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Tip: callback(symbol: str, kline: dict) → None (async)
KlineCallback = Callable[[str, dict], asyncio.Future]


class MarketDataStore:
    """
    Her sembol için son N mumun OHLCV verisini bellekte tutar.
    WebSocket'ten gelen kapanmış mumlar buraya eklenir.
    """

    def __init__(self, max_candles: int = 350) -> None:
        self.max_candles = max_candles
        # symbol → list of dicts: {open, high, low, close, volume, close_time}
        self._data: Dict[str, List[Dict]] = {}
        self._last_update: Dict[str, float] = {}

    def update(self, symbol: str, kline: dict) -> None:
        """Kapanmış mum verisini ekle."""
        if symbol not in self._data:
            self._data[symbol] = []
        candles = self._data[symbol]
        candle = {
            "open_time": kline["t"],
            "open": float(kline["o"]),
            "high": float(kline["h"]),
            "low": float(kline["l"]),
            "close": float(kline["c"]),
            "volume": float(kline["v"]),
            "close_time": kline["T"],
            "quote_volume": float(kline["q"]),
            "num_trades": kline["n"],
        }
        # Aynı open_time'a sahip mum varsa güncelle
        if candles and candles[-1]["open_time"] == candle["open_time"]:
            candles[-1] = candle
        else:
            candles.append(candle)
            if len(candles) > self.max_candles:
                candles.pop(0)
        self._last_update[symbol] = time.time()

    def seed(self, symbol: str, candles: List[Dict]) -> None:
        """REST'ten alınan geçmiş verileri yükle."""
        self._data[symbol] = candles[-self.max_candles :]
        self._last_update[symbol] = time.time()

    def get(self, symbol: str) -> List[Dict]:
        return self._data.get(symbol, [])

    def is_stale(self, symbol: str) -> bool:
        last = self._last_update.get(symbol, 0)
        return (time.time() - last) > config.STALE_DATA_SECONDS

    def symbols(self) -> List[str]:
        return list(self._data.keys())


class WebSocketManager:
    """
    Birden fazla kline stream'ini tek bağlantıda yönetir.
    Binance combined stream: wss://fstream.binance.com/stream?streams=...
    """

    _MAX_STREAMS_PER_CONN = 200  # Binance limiti

    def __init__(
        self,
        market_store: MarketDataStore,
        on_kline_close: KlineCallback,
    ) -> None:
        self.store = market_store
        self.on_kline_close = on_kline_close
        self._symbols: List[str] = []
        self._interval: str = config.PRIMARY_TF
        self._tasks: List[asyncio.Task] = []
        self._running = False

    def set_symbols(self, symbols: List[str], interval: str) -> None:
        self._symbols = symbols
        self._interval = interval

    async def start(self) -> None:
        self._running = True
        # Büyük sembol listelerini gruplara böl
        chunks = [
            self._symbols[i : i + self._MAX_STREAMS_PER_CONN]
            for i in range(0, len(self._symbols), self._MAX_STREAMS_PER_CONN)
        ]
        for chunk in chunks:
            task = asyncio.create_task(self._run_chunk(chunk))
            self._tasks.append(task)
        logger.info(f"WebSocket: {len(self._symbols)} sembol, {len(chunks)} bağlantı")

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _run_chunk(self, symbols: List[str]) -> None:
        streams = "/".join(
            f"{s.lower()}@kline_{self._interval}" for s in symbols
        )
        url = f"{config.WS_BASE}/stream?streams={streams}"

        while self._running:
            try:
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=10,
                    max_size=2**20,
                ) as ws:
                    logger.info(f"WS bağlandı: {len(symbols)} sembol")
                    async for raw_msg in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw_msg)
                            await self._handle(msg)
                        except Exception as exc:
                            logger.warning(f"WS mesaj hatası: {exc}")

            except ConnectionClosed as exc:
                if self._running:
                    logger.warning(f"WS kapandı ({exc.code}), 5sn sonra yeniden bağlanılıyor")
                    await asyncio.sleep(5)
            except WebSocketException as exc:
                if self._running:
                    logger.warning(f"WS hatası: {exc}, 5sn sonra yeniden bağlanılıyor")
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if self._running:
                    logger.error(f"WS beklenmedik hata: {exc}")
                    await asyncio.sleep(10)

    async def _handle(self, msg: dict) -> None:
        data = msg.get("data", msg)
        if data.get("e") != "kline":
            return
        kline = data["k"]
        symbol = data["s"]

        # Her mumda store'u güncelle (kapanmamış mum bile)
        self.store.update(symbol, kline)

        # Sadece kapanmış mumda callback çağır
        if kline.get("x"):
            try:
                await self.on_kline_close(symbol, kline)
            except Exception as exc:
                logger.error(f"on_kline_close hatası [{symbol}]: {exc}")


class UserDataStream:
    """
    Binance user data stream — emir güncellemelerini dinler.
    Listen key her LISTEN_KEY_REFRESH_MINUTES dakikada yenilenir.
    """

    def __init__(
        self,
        client,  # BinanceClient
        on_order_update: Callable[[dict], asyncio.Future],
    ) -> None:
        self._client = client
        self.on_order_update = on_order_update
        self._listen_key: Optional[str] = None
        self._task: Optional[asyncio.Task] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> None:
        self._running = True
        self._listen_key = await self._client.create_listen_key()
        self._task = asyncio.create_task(self._run())
        self._keepalive_task = asyncio.create_task(self._keepalive())
        logger.info("User data stream başlatıldı")

    async def stop(self) -> None:
        self._running = False
        for t in [self._task, self._keepalive_task]:
            if t:
                t.cancel()
        await asyncio.gather(self._task, self._keepalive_task, return_exceptions=True)

    async def _keepalive(self) -> None:
        interval = config.LISTEN_KEY_REFRESH_MINUTES * 60
        while self._running:
            await asyncio.sleep(interval)
            if not self._running:
                break
            try:
                await self._client.keepalive_listen_key(self._listen_key)
                logger.debug("Listen key yenilendi")
            except Exception as exc:
                logger.warning(f"Listen key yenileme hatası: {exc}")
                try:
                    self._listen_key = await self._client.create_listen_key()
                except Exception as exc2:
                    logger.error(f"Yeni listen key alınamadı: {exc2}")

    async def _run(self) -> None:
        while self._running:
            url = f"{config.WS_BASE}/ws/{self._listen_key}"
            try:
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=10
                ) as ws:
                    logger.info("User data stream bağlandı")
                    async for raw_msg in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw_msg)
                            if msg.get("e") == "ORDER_TRADE_UPDATE":
                                await self.on_order_update(msg)
                        except Exception as exc:
                            logger.warning(f"User stream mesaj hatası: {exc}")
            except ConnectionClosed:
                if self._running:
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if self._running:
                    logger.error(f"User stream hatası: {exc}")
                    await asyncio.sleep(10)
