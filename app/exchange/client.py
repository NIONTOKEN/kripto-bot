"""
Binance USDT Perpetual Futures REST istemcisi.
Ham aiohttp kullanılır — tam kontrol için python-binance kullanılmaz.
Tüm parametreler Binance Futures API v1/v2 ile uyumludur.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import aiohttp

from app.config import config
from app.utils.logger import get_logger

logger = get_logger(__name__)


class BinanceClient:
    """Binance Futures REST API async istemcisi."""

    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None
        self.symbol_info: Dict[str, Dict] = {}   # symbol → exchangeInfo entry
        self._qty_precisions: Dict[str, int] = {}
        self._price_precisions: Dict[str, int] = {}
        self._min_qtys: Dict[str, Decimal] = {}
        self._min_notionals: Dict[str, Decimal] = {}
        # Rate limiter: maks 5 eş zamanlı istek, istekler arası min 150ms
        self._rate_sem = asyncio.Semaphore(5)
        self._last_request_time: float = 0.0
        self._min_request_interval = 0.15  # saniye
        self._time_offset_ms: int = 0      # Binance sunucu saat farkı

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(
            headers={"X-MBX-APIKEY": config.BINANCE_API_KEY},
            timeout=aiohttp.ClientTimeout(total=30),
        )
        await self._sync_server_time()
        await self._load_exchange_info()

    async def _sync_server_time(self) -> None:
        """Binance sunucu saati ile yerel saat arasındaki farkı hesapla."""
        try:
            url = f"{config.REST_BASE}/fapi/v1/time"
            async with self._session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    server_time = int(data.get("serverTime", 0))
                    local_time = int(time.time() * 1000)
                    self._time_offset_ms = server_time - local_time
                    logger.info(f"Binance sunucu zaman farkı senkronize edildi: {self._time_offset_ms} ms")
        except Exception as exc:
            logger.warning(f"Zaman senkronizasyon hatası: {exc}")

    async def stop(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ── İmzalama ─────────────────────────────────────────────────────────────

    def _sign(self, params: Dict[str, Any]) -> str:
        # Sunucu zaman farkını ekle ve geniş recvWindow kullan
        synced_ts = int(time.time() * 1000) + self._time_offset_ms
        params["timestamp"] = synced_ts
        if "recvWindow" not in params:
            params["recvWindow"] = 60000
        query_string = urlencode(sorted(params.items()))
        sig = hmac.new(
            config.BINANCE_SECRET_KEY.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{query_string}&signature={sig}"

    # ── İstek ────────────────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        signed: bool = False,
        retries: int = 3,
    ) -> Any:
        if self._session is None:
            raise RuntimeError("BinanceClient.start() çağrılmadı")
        url = f"{config.REST_BASE}{path}"
        if params is None:
            params = {}
        req_params = None if signed else params
        last_exc: Exception = RuntimeError("Unknown")
        for attempt in range(retries):
            # İmzalı isteklerde her denemede taze timestamp üret
            actual_url = url
            if signed:
                qs_with_sig = self._sign(dict(params))
                actual_url = f"{url}?{qs_with_sig}"

            # Rate limiting: istekler arası minimum bekleme
            async with self._rate_sem:
                now = asyncio.get_event_loop().time()
                elapsed = now - self._last_request_time
                if elapsed < self._min_request_interval:
                    await asyncio.sleep(self._min_request_interval - elapsed)
                self._last_request_time = asyncio.get_event_loop().time()

                try:
                    async with self._session.request(method, actual_url, params=req_params) as resp:
                        data = await resp.json(content_type=None)

                        # 429 veya -1003: IP ban → hemen dur, retry etme!
                        if resp.status == 429 or (isinstance(data, dict) and data.get("code") == -1003):
                            ban_msg = data.get("msg", "Rate limit aşıldı") if isinstance(data, dict) else "Rate limit"
                            logger.critical(f"⛔ RATE LIMIT / IP BAN: {ban_msg}")
                            raise RuntimeError(f"Binance -1003: {ban_msg}")

                        if resp.status == 200:
                            return data

                        code = data.get("code", resp.status) if isinstance(data, dict) else resp.status
                        msg = data.get("msg", str(data)) if isinstance(data, dict) else str(data)

                        # -4046: margin type zaten set → ignore
                        if code == -4046:
                            return data

                        # -1021: Timestamp / recvWindow hatası → saati yeniden eşitle ve tekrar dene!
                        if code == -1021:
                            logger.warning("Binance -1021 zaman farkı algılandı, sunucu saati yeniden senkronize ediliyor...")
                            await self._sync_server_time()
                            await asyncio.sleep(0.5)
                            continue

                        # -2015: IP whitelist'te yok → retry etme
                        if code == -2015:
                            raise RuntimeError(f"Binance {code}: IP whitelist'e ekle! {msg}")

                        raise RuntimeError(f"Binance {code}: {msg}")

                except RuntimeError:
                    raise  # RuntimeError direkt yükselt
                except aiohttp.ClientError as exc:
                    last_exc = exc
                    if attempt < retries - 1:
                        wait = 2 ** attempt * 2
                        logger.warning(f"İstek hatası (deneme {attempt+1}/{retries}): {exc}. {wait}s bekleniyor...")
                        await asyncio.sleep(wait)
        raise last_exc

    # ── Exchange Info ─────────────────────────────────────────────────────────

    async def _load_exchange_info(self) -> None:
        data = await self._request("GET", "/fapi/v1/exchangeInfo")
        for sym in data["symbols"]:
            if (
                sym.get("contractType") == "PERPETUAL"
                and sym.get("quoteAsset") == "USDT"
                and sym.get("status") == "TRADING"
            ):
                s = sym["symbol"]
                self.symbol_info[s] = sym

                # LOT_SIZE
                for f in sym.get("filters", []):
                    if f["filterType"] == "LOT_SIZE":
                        step = f["stepSize"].rstrip("0")
                        if "." in step:
                            self._qty_precisions[s] = len(step.split(".")[1])
                        else:
                            self._qty_precisions[s] = 0
                        self._min_qtys[s] = Decimal(f["minQty"])
                    elif f["filterType"] == "MIN_NOTIONAL":
                        self._min_notionals[s] = Decimal(f.get("notional", "5"))

                self._price_precisions[s] = sym.get("pricePrecision", 2)

        logger.info(f"Exchange info: {len(self.symbol_info)} USDT perpetual sembol")

    async def reload_exchange_info(self) -> None:
        """Sembol bilgilerini yenile."""
        self.symbol_info.clear()
        self._qty_precisions.clear()
        self._price_precisions.clear()
        self._min_qtys.clear()
        self._min_notionals.clear()
        await self._load_exchange_info()

    # ── Precision Yardımcıları ────────────────────────────────────────────────

    def get_qty_precision(self, symbol: str) -> int:
        return self._qty_precisions.get(symbol, 3)

    def get_price_precision(self, symbol: str) -> int:
        return self._price_precisions.get(symbol, 2)

    def get_min_qty(self, symbol: str) -> Decimal:
        return self._min_qtys.get(symbol, Decimal("0.001"))

    def get_min_notional(self, symbol: str) -> Decimal:
        return self._min_notionals.get(symbol, Decimal("5"))

    def round_qty(self, symbol: str, qty: Decimal) -> Decimal:
        p = self.get_qty_precision(symbol)
        if p == 0:
            return qty.to_integral_value()
        factor = Decimal(10) ** p
        return (qty * factor).to_integral_value() / factor

    def round_price(self, symbol: str, price: Decimal) -> Decimal:
        p = self.get_price_precision(symbol)
        factor = Decimal(10) ** p
        return (price * factor).to_integral_value() / factor

    # ── Hesap ────────────────────────────────────────────────────────────────

    async def get_balance_usdt(self) -> Decimal:
        """Kullanılabilir serbest USDT bakiyesi."""
        data = await self._request("GET", "/fapi/v2/balance", signed=True)
        for item in data:
            if item["asset"] == "USDT":
                return Decimal(item["availableBalance"])
        raise RuntimeError("USDT bakiyesi bulunamadı")

    async def get_wallet_balance_usdt(self) -> Decimal:
        """Cüzdandaki ana USDT bakiyesi (gerçekleşmiş)."""
        data = await self._request("GET", "/fapi/v2/balance", signed=True)
        for item in data:
            if item["asset"] == "USDT":
                return Decimal(item["balance"])
        raise RuntimeError("USDT bakiyesi bulunamadı")

    async def get_total_balance_usdt(self) -> Decimal:
        """Toplam USDT cüzdan değeri (unrealized PnL dahil net equity)."""
        data = await self._request("GET", "/fapi/v2/balance", signed=True)
        for item in data:
            if item["asset"] == "USDT":
                wallet_bal = Decimal(item.get("balance", "0"))
                unpnl = Decimal(item.get("crossUnPnl", "0"))
                return wallet_bal + unpnl
        raise RuntimeError("USDT bakiyesi bulunamadı")

    async def get_positions(self) -> List[Dict]:
        """Sıfır olmayan pozisyonları döner."""
        data = await self._request("GET", "/fapi/v2/positionRisk", signed=True)
        return [p for p in data if Decimal(p["positionAmt"]) != Decimal("0")]

    async def get_position(self, symbol: str) -> Optional[Dict]:
        """Belirli sembol için pozisyon, yoksa None."""
        data = await self._request(
            "GET", "/fapi/v2/positionRisk", params={"symbol": symbol}, signed=True
        )
        for p in data:
            if Decimal(p["positionAmt"]) != Decimal("0"):
                return p
        return None

    # ── Piyasa Verisi ─────────────────────────────────────────────────────────

    async def get_klines(
        self, symbol: str, interval: str, limit: int = 300
    ) -> List[List]:
        """Geçmiş mum verisi."""
        return await self._request(
            "GET",
            "/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
        )

    async def get_ticker_24h_all(self) -> List[Dict]:
        """Tüm sembollerin 24 saatlik ticker verisi."""
        return await self._request("GET", "/fapi/v1/ticker/24hr")

    async def get_mark_price(self, symbol: str) -> Decimal:
        data = await self._request(
            "GET", "/fapi/v1/premiumIndex", params={"symbol": symbol}
        )
        return Decimal(data["markPrice"])

    async def get_funding_rate(self, symbol: str) -> float:
        """Son 8 saatlik Fonlama Oranı (Funding Rate)."""
        try:
            data = await self._request(
                "GET", "/fapi/v1/premiumIndex", params={"symbol": symbol}
            )
            return float(data.get("lastFundingRate", 0.0))
        except Exception:
            return 0.0

    async def get_open_interest(self, symbol: str) -> float:
        """Açık Pozisyon Hacmi (Open Interest)."""
        try:
            data = await self._request(
                "GET", "/fapi/v1/openInterest", params={"symbol": symbol}
            )
            return float(data.get("openInterest", 0.0))
        except Exception:
            return 0.0

    async def get_order_book(self, symbol: str, limit: int = 50) -> Dict:
        """
        Derinlik (Order Book / Tahta) verisi.
        bids: [[price, qty], ...], asks: [[price, qty], ...]
        """
        return await self._request(
            "GET", "/fapi/v1/depth", params={"symbol": symbol, "limit": limit}
        )

    async def get_book_ticker(self, symbol: str) -> Dict:
        """En iyi anlık alış ve satış fiyat/miktarları."""
        return await self._request(
            "GET", "/fapi/v1/ticker/bookTicker", params={"symbol": symbol}
        )

    # ── Ayarlar ───────────────────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        await self._request(
            "POST",
            "/fapi/v1/leverage",
            params={"symbol": symbol, "leverage": leverage},
            signed=True,
        )

    async def set_isolated_margin(self, symbol: str) -> None:
        """ISOLATED margin tipini ayarla. Zaten set ise sessizce devam et."""
        try:
            await self._request(
                "POST",
                "/fapi/v1/marginType",
                params={"symbol": symbol, "marginType": "ISOLATED"},
                signed=True,
            )
        except RuntimeError as exc:
            if "-4046" in str(exc) or "No need to change" in str(exc):
                return
            raise

    # ── Emirler ──────────────────────────────────────────────────────────────

    async def place_market_order(
        self, symbol: str, side: str, quantity: Decimal
    ) -> Dict:
        """
        MARKET emri aç.
        side: 'BUY' (long aç) veya 'SELL' (short aç)
        """
        p = self.get_qty_precision(symbol)
        return await self._request(
            "POST",
            "/fapi/v1/order",
            params={
                "symbol": symbol,
                "side": side,
                "type": "MARKET",
                "quantity": f"{quantity:.{p}f}",
            },
            signed=True,
        )

    async def place_stop_market(
        self, symbol: str, side: str, stop_price: Decimal, quantity: Optional[Decimal] = None
    ) -> Dict:
        """
        STOP_MARKET emri — pozisyonu korur.
        Önce closePosition=true dener, -4120 dönerse reduceOnly=true ile dener.
        """
        pp = self.get_price_precision(symbol)
        qp = self.get_qty_precision(symbol)

        try:
            params = {
                "symbol": symbol,
                "side": side,
                "type": "STOP_MARKET",
                "stopPrice": f"{stop_price:.{pp}f}",
                "closePosition": "true",
                "workingType": "MARK_PRICE",
            }
            return await self._request("POST", "/fapi/v1/order", params=params, signed=True)
        except Exception as exc:
            if "-4120" in str(exc) and quantity is not None:
                # Multi-Asset veya Algo kısıtlaması -> reduceOnly ile dene
                logger.info(f"[{symbol}] STOP_MARKET closePosition başarısız, reduceOnly deneniyor...")
                params = {
                    "symbol": symbol,
                    "side": side,
                    "type": "STOP_MARKET",
                    "stopPrice": f"{stop_price:.{pp}f}",
                    "quantity": f"{quantity:.{qp}f}",
                    "reduceOnly": "true",
                    "workingType": "MARK_PRICE",
                }
                return await self._request("POST", "/fapi/v1/order", params=params, signed=True)
            raise

    async def place_take_profit_market(
        self, symbol: str, side: str, stop_price: Decimal, quantity: Optional[Decimal] = None
    ) -> Dict:
        """
        TAKE_PROFIT_MARKET emri.
        """
        pp = self.get_price_precision(symbol)
        qp = self.get_qty_precision(symbol)

        try:
            params = {
                "symbol": symbol,
                "side": side,
                "type": "TAKE_PROFIT_MARKET",
                "stopPrice": f"{stop_price:.{pp}f}",
                "closePosition": "true",
                "workingType": "MARK_PRICE",
            }
            return await self._request("POST", "/fapi/v1/order", params=params, signed=True)
        except Exception as exc:
            if "-4120" in str(exc) and quantity is not None:
                params = {
                    "symbol": symbol,
                    "side": side,
                    "type": "TAKE_PROFIT_MARKET",
                    "stopPrice": f"{stop_price:.{pp}f}",
                    "quantity": f"{quantity:.{qp}f}",
                    "reduceOnly": "true",
                    "workingType": "MARK_PRICE",
                }
                return await self._request("POST", "/fapi/v1/order", params=params, signed=True)
            raise

    async def close_position_market(self, symbol: str, side: str, quantity: Decimal) -> Dict:
        """
        Mevcut pozisyonu market emriyle kapat.
        side: LONG pozisyon için 'SELL', SHORT için 'BUY'
        """
        p = self.get_qty_precision(symbol)
        return await self._request(
            "POST",
            "/fapi/v1/order",
            params={
                "symbol": symbol,
                "side": side,
                "type": "MARKET",
                "quantity": f"{quantity:.{p}f}",
                "reduceOnly": "true",
            },
            signed=True,
        )

    async def cancel_order(self, symbol: str, order_id: int) -> Dict:
        return await self._request(
            "DELETE",
            "/fapi/v1/order",
            params={"symbol": symbol, "orderId": order_id},
            signed=True,
        )

    async def cancel_all_open_orders(self, symbol: str) -> Dict:
        return await self._request(
            "DELETE",
            "/fapi/v1/allOpenOrders",
            params={"symbol": symbol},
            signed=True,
        )

    async def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        params: Dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol
        return await self._request("GET", "/fapi/v1/openOrders", params=params, signed=True)

    # ── Listen Key ────────────────────────────────────────────────────────────

    async def create_listen_key(self) -> str:
        data = await self._request("POST", "/fapi/v1/listenKey")
        return data["listenKey"]

    async def keepalive_listen_key(self, listen_key: str) -> None:
        await self._request("PUT", "/fapi/v1/listenKey", params={"listenKey": listen_key})
