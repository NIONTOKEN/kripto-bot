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

        # 4. Günlük kayıp limiti (Sadece gerçekleşen net zarara bakar, marjini zarar saymaz!)
        if self._starting_balance and self._starting_balance > 0:
            from app.database import get_todays_closed_pnl
            todays_pnl = await get_todays_closed_pnl()
            daily_loss_limit = self._starting_balance * config.DAILY_LOSS_LIMIT_PCT / Decimal("100")
            if todays_pnl < 0 and abs(todays_pnl) > daily_loss_limit:
                return RiskCheck(
                    False,
                    f"Günlük gerçekleşen kayıp limiti: {abs(todays_pnl):.2f} > {daily_loss_limit:.2f} USDT"
                )

        return RiskCheck(True)

    def calculate_position_size(
        self,
        symbol: str,
        ind: IndicatorResult,
        balance: Decimal,
        direction: str,
        signal_type: str = "NORMAL",
    ) -> Tuple[Optional[Decimal], Optional[Decimal], Optional[Decimal]]:
        """
        ATR tabanlı ve otomatik bileşik büyümeli pozisyon boyutu hesapla.

        Returns: (quantity, sl_price, tp_price) veya (None, None, None)
        """
        try:
            price = Decimal(str(ind.close))
            atr = Decimal(str(ind.atr))

            if price <= 0 or atr <= 0:
                logger.warning(f"[{symbol}] Geçersiz fiyat veya ATR")
                return None, None, None

            # ── 1. OTOMATİK BİLEŞİK BÜYÜME (AUTO-COMPOUNDING) MARJİN BELİRLEME ───
            # Cüzdan bakiyesine göre kademeli pozisyon marjini:
            # - Bakiye < 25 USDT: %25 marjin per pozisyon (örn: 11$ -> ~2.75$ marjin)
            # - 25 <= Bakiye < 100 USDT: %22 marjin (örn: 50$ -> 11$ marjin)
            # - 100 <= Bakiye < 1000 USDT: %20 marjin (örn: 250$ -> 50$ marjin)
            # - Bakiye >= 1000 USDT: %15 marjin (örn: 2000$ -> 300$ marjin)
            if balance < Decimal("25"):
                margin_pct = Decimal("0.25")
            elif balance < Decimal("100"):
                margin_pct = Decimal("0.22")
            elif balance < Decimal("1000"):
                margin_pct = Decimal("0.20")
            else:
                margin_pct = Decimal("0.15")

            target_margin = balance * margin_pct
            target_margin = max(target_margin, Decimal("2.0"))  # Binance min işlem için en az 2 USDT
            target_notional = target_margin * Decimal(str(config.LEVERAGE))

            # Minimum qty ve notional kontrolleri (Binance kuralları)
            min_qty = self._client.get_min_qty(symbol)
            min_notional = self._client.get_min_notional(symbol)
            target_notional = max(target_notional, min_notional * Decimal("1.15"))

            # Hedef miktar (quantity)
            raw_qty = target_notional / price
            qty = self._client.round_qty(symbol, raw_qty)
            qty = max(qty, min_qty)

            # Gerekli marjin kontrolü
            required_margin = (qty * price) / Decimal(str(config.LEVERAGE))
            if required_margin > balance * Decimal("0.90"):
                # Bakiye yetersizse bakiyenin %85'ine sığacak maksimum miktarı ver
                max_afford_notional = (balance * Decimal("0.85")) * Decimal(str(config.LEVERAGE))
                if max_afford_notional < min_notional:
                    logger.warning(f"[{symbol}] Bakiye en küçük işlem için yetersiz: {balance:.2f} USDT")
                    return None, None, None
                qty = self._client.round_qty(symbol, max_afford_notional / price)
                qty = max(qty, min_qty)
                required_margin = (qty * price) / Decimal(str(config.LEVERAGE))

            # ── 2. STOP LOSS & TAKE PROFIT (MAKRO ÇÖKÜŞ & PARABOLİK TREND AYARI) ─
            if signal_type == "COLLAPSE_SHORT":
                # LUNA tipi makro çöküş: Erken kâr alıp çıkmak YASAK!
                # Günlerce, haftalarca sürmek için TP tabanı çok derin (%85 çöküş hedefi),
                # İşlemi Trailing Stop dipten takip eder.
                sl_distance = max(atr * Decimal("3.0"), price * Decimal("0.035"))
                tp_distance = price * Decimal("0.85")
                sl_price = price + sl_distance
                tp_price = max(price - tp_distance, price * Decimal("0.05"))
            elif signal_type == "SUPER_LONG":
                # Parabolik boğa koşusu: Günlerce sürülmesi için geniş TP (%200 yükseliş hedefi)
                sl_distance = max(atr * Decimal("3.0"), price * Decimal("0.035"))
                tp_distance = price * Decimal("2.00")
                sl_price = price - sl_distance
                tp_price = price + tp_distance
            else:
                # Normal gün içi trend işlemleri
                min_sl_dist = price * Decimal("0.020")
                calc_sl_dist = atr * Decimal(str(config.SL_ATR_MULTIPLIER))
                sl_distance = max(calc_sl_dist, min_sl_dist)

                min_tp_dist = sl_distance * Decimal("1.8")
                calc_tp_dist = atr * Decimal(str(config.TP_ATR_MULTIPLIER))
                tp_distance = max(calc_tp_dist, min_tp_dist)

                if direction == "LONG":
                    sl_price = price - sl_distance
                    tp_price = price + tp_distance
                else:
                    sl_price = price + sl_distance
                    tp_price = price - tp_distance

            if sl_price <= 0 or tp_price <= 0:
                return None, None, None

            sl_price = self._client.round_price(symbol, sl_price)
            tp_price = self._client.round_price(symbol, tp_price)

            logger.info(
                f"[{symbol}] Pozisyon Boyutu Hesaplandı ({signal_type}): Notional={qty * price:.2f}$ "
                f"(Marjin={required_margin:.2f}$ [{config.LEVERAGE}x]) | SL={sl_price} | TP={tp_price}"
            )
            return qty, sl_price, tp_price

        except Exception as exc:
            logger.error(f"[{symbol}] Pozisyon boyutu hesaplama hatası: {exc}")
            return None, None, None
