"""
Risk yöneticisi.
- Günlük kayıp limiti kontrolü
- Maksimum açık pozisyon sayısı
- ATR tabanlı pozisyon boyutu hesaplama
- Min notional ve min quantity doğrulaması
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Tuple

from app.config import config
from app.exchange.client import BinanceClient
from app.strategy.indicators import IndicatorResult
from app.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class RiskCheck:
    allowed: bool
    reason: str = ""


class RiskManager:
    def __init__(self, client: BinanceClient) -> None:
        self._client = client
        self._starting_balance: Optional[Decimal] = None
        self._day_start: float = 0.0

    async def refresh_daily_baseline(self, balance: Decimal) -> None:
        """Gün başında bakiyeyi kaydet. Bot her başlatıldığında çağrılır."""
        self._starting_balance = balance
        self._day_start = time.time()
        logger.info(f"Günlük başlangıç bakiyesi: {balance:.2f} USDT")

    async def check_can_open(
        self,
        symbol: str,
        open_position_count: int,
        current_balance: Decimal,
    ) -> RiskCheck:
        """
        Yeni pozisyon açılabilir mi?
        Tüm güvenlik kuralları burada kontrol edilir.
        """
        # 1. Minimum bakiye
        if current_balance < config.MIN_BALANCE_USDT:
            return RiskCheck(False, f"Bakiye çok düşük: {current_balance:.2f} < {config.MIN_BALANCE_USDT}")

        # 2. Maksimum açık pozisyon
        if open_position_count >= config.MAX_OPEN_POSITIONS:
            return RiskCheck(False, f"Maks açık pozisyon: {open_position_count}/{config.MAX_OPEN_POSITIONS}")

        # 3. Kaldıraç limiti
        if config.LEVERAGE > config.MAX_LEVERAGE:
            return RiskCheck(False, f"Kaldıraç limiti aşıldı: {config.LEVERAGE} > {config.MAX_LEVERAGE}")

        # 4. Günlük kayıp limiti
        if self._starting_balance and self._starting_balance > 0:
            daily_loss_limit = self._starting_balance * config.DAILY_LOSS_LIMIT_PCT / Decimal("100")
            daily_loss = self._starting_balance - current_balance
            if daily_loss > daily_loss_limit:
                return RiskCheck(
                    False,
                    f"Günlük kayıp limiti: {daily_loss:.2f} > {daily_loss_limit:.2f} USDT"
                )

        return RiskCheck(True)

    def calculate_position_size(
        self,
        symbol: str,
        ind: IndicatorResult,
        balance: Decimal,
        direction: str,
    ) -> Tuple[Optional[Decimal], Optional[Decimal], Optional[Decimal]]:
        """
        ATR tabanlı pozisyon boyutu hesapla.

        Returns: (quantity, sl_price, tp_price) veya (None, None, None)

        Yöntem:
        - Risk miktarı = balance × RISK_PER_TRADE_PCT / 100
        - SL mesafesi = ATR × SL_ATR_MULTIPLIER
        - Notional = Risk / (SL_mesafesi / fiyat) / kaldıraç
        - Quantity = Notional / fiyat
        """
        try:
            price = Decimal(str(ind.close))
            atr = Decimal(str(ind.atr))

            if price <= 0 or atr <= 0:
                logger.warning(f"[{symbol}] Geçersiz fiyat veya ATR")
                return None, None, None

            # Risk miktarı (USDT)
            risk_usdt = balance * config.RISK_PER_TRADE_PCT / Decimal("100")

            # SL/TP mesafeleri
            sl_distance = atr * config.SL_ATR_MULTIPLIER
            tp_distance = atr * config.TP_ATR_MULTIPLIER

            # SL/TP fiyatları
            if direction == "LONG":
                sl_price = price - sl_distance
                tp_price = price + tp_distance
                if sl_price <= 0:
                    return None, None, None
            else:  # SHORT
                sl_price = price + sl_distance
                tp_price = price - tp_distance
                if tp_price <= 0:
                    return None, None, None

            # Pozisyon büyüklüğü hesabı
            # Kayıp = qty × sl_distance (kaldıraçsız, isolated margin)
            # risk_usdt = qty × sl_distance
            # qty = risk_usdt / sl_distance
            qty_unlevered = risk_usdt / sl_distance

            # Kaldıraç pozisyon boyutunu büyütür
            # Ama risk hesabında kaldıraçı dahil ediyoruz:
            # Gerçek kayıp = qty × sl_distance (kaldıraçlı marginden)
            # Margin gereksinimi = qty × price / leverage
            qty = qty_unlevered  # kaldıraç risk hesabına dahil değil, sadece marjine etkisi var

            # Minimum qty ve notional kontrolü & Mikro bakiye desteği:
            min_qty = self._client.get_min_qty(symbol)
            min_notional = self._client.get_min_notional(symbol)
            
            # Eğer hesaplanan pozisyon Binance minimumunun altındaysa ama bakiye yetiyorsa minimuma yükselt
            notional = qty * price
            if notional < min_notional:
                min_req_qty = self._client.round_qty(symbol, (min_notional * Decimal("1.05")) / price)
                min_req_qty = max(min_req_qty, min_qty)
                req_margin = (min_req_qty * price) / Decimal(str(config.LEVERAGE))
                if req_margin <= balance * Decimal("0.98"):
                    qty = min_req_qty
                    notional = qty * price
                else:
                    logger.warning(
                        f"[{symbol}] Notional {notional:.2f} < min {min_notional} ve marjin yetersiz (gerekli: {req_margin:.2f}, bakiye: {balance:.2f})"
                    )
                    return None, None, None

            if qty < min_qty:
                qty = min_qty
                notional = qty * price

            # Margin kontrolü: yeterli bakiye var mı?
            required_margin = (qty * price) / Decimal(str(config.LEVERAGE))
            if required_margin > balance * Decimal("0.98"):
                logger.warning(f"[{symbol}] Yetersiz margin: {required_margin:.2f} > {balance:.2f}")
                return None, None, None

            sl_price = self._client.round_price(symbol, sl_price)
            tp_price = self._client.round_price(symbol, tp_price)

            return qty, sl_price, tp_price

        except Exception as exc:
            logger.error(f"[{symbol}] Pozisyon boyutu hesaplama hatası: {exc}")
            return None, None, None
