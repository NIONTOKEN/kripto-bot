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
    ) -> Tuple[Optional[Decimal], Optional[Decimal], Optional[Decimal], int]:
        """
        ATR tabanlı, dinamik kaldıraçlı ve otomatik bileşik büyümeli pozisyon boyutu hesapla.

        Returns: (quantity, sl_price, tp_price, leverage) veya (None, None, None, leverage)
        """
        # Varsayılan kaldıraç
        default_lev = config.LEVERAGE

        # ── 0. DİNAMİK KALDIRAÇ BELİRLEME (VUR-KAÇ vs MAKRO) ───────────────────
        if signal_type.startswith("SCALP"):
            # Vur-Kaç Scalp: Hızlı kâr alımı, yüksek kaldıraç (örn. 15x-20x)
            leverage = min(config.SCALP_LEVERAGE, config.MAX_LEVERAGE)
        elif signal_type in ("SUPER_LONG", "COLLAPSE_SHORT"):
            # Uzun vadeli makro trend / çöküş: Daha temkinli kaldıraç (örn. 6x-8x)
            leverage = min(config.SWING_LEVERAGE, 8)
        else:
            # Gün içi standart trend
            leverage = min(config.LEVERAGE, config.MAX_LEVERAGE)

        lev_dec = Decimal(str(leverage))

        try:
            price = Decimal(str(ind.close))
            atr = Decimal(str(ind.atr))

            if price <= 0 or atr <= 0:
                logger.warning(f"[{symbol}] Geçersiz fiyat veya ATR")
                return None, None, None, leverage

            # ── 1. OTOMATİK BİLEŞİK BÜYÜME MARJİN BELİRLEME ───────────────────
            # Cüzdan bakiyesine göre kademeli pozisyon marjini
            if balance < Decimal("25"):
                margin_pct = Decimal("0.25")
            elif balance < Decimal("100"):
                margin_pct = Decimal("0.22")
            elif balance < Decimal("1000"):
                margin_pct = Decimal("0.20")
            else:
                margin_pct = Decimal("0.15")

            target_margin = balance * margin_pct

            # Binance min notional kontrolü (Genelde 5 USDT)
            min_notional = self._client.get_min_notional(symbol)
            min_qty = self._client.get_min_qty(symbol)

            # Minimum notional'ı karşılamak için gereken asgari marjin
            min_required_margin = (min_notional * Decimal("1.10")) / lev_dec
            target_margin = max(target_margin, min_required_margin)
            target_notional = target_margin * lev_dec
            target_notional = max(target_notional, min_notional * Decimal("1.10"), Decimal("5.20"))

            # Hedef miktar (quantity)
            raw_qty = target_notional / price
            qty = self._client.round_qty(symbol, raw_qty)
            qty = max(qty, min_qty)

            # Miktar * Fiyat min_notional'ın altında kalırsa bir kademe artır
            if qty * price < min_notional:
                step = self._client.get_step_size(symbol)
                while qty * price < min_notional * Decimal("1.05"):
                    qty += step
                qty = self._client.round_qty(symbol, qty)

            # Gerekli marjin kontrolü
            required_margin = (qty * price) / lev_dec
            if required_margin > balance * Decimal("0.90"):
                # Bakiye yetersizse bakiyenin %85'ine sığacak maksimum miktarı dene
                max_afford_notional = (balance * Decimal("0.85")) * lev_dec
                if max_afford_notional < min_notional:
                    logger.warning(
                        f"[{symbol}] Bakiye en küçük işlem ({min_notional:.2f}$) için yetersiz: "
                        f"Bakiye={balance:.2f} USDT, Gereken Marjin={min_required_margin:.2f} USDT"
                    )
                    return None, None, None, leverage
                qty = self._client.round_qty(symbol, max_afford_notional / price)
                qty = max(qty, min_qty)
                required_margin = (qty * price) / lev_dec

            # ── 2. STOP LOSS & TAKE PROFIT AYARLARI ────────────────────────────
            if signal_type.startswith("SCALP"):
                # Vur-Kaç Scalp: Dar SL (%0.9-%1.2), hızlı TP (%1.8-%2.5)
                sl_distance = max(atr * Decimal("1.1"), price * Decimal("0.010"))
                tp_distance = max(atr * Decimal("2.0"), price * Decimal("0.022"))
                if direction == "LONG":
                    sl_price = price - sl_distance
                    tp_price = price + tp_distance
                else:
                    sl_price = price + sl_distance
                    tp_price = price - tp_distance
            elif signal_type == "COLLAPSE_SHORT":
                # LUNA tipi makro çöküş: Erken kâr alıp çıkmak YASAK! Derin TP, Trailing takip eder
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
                min_sl_dist = price * Decimal("0.018")
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
                return None, None, None, leverage

            sl_price = self._client.round_price(symbol, sl_price)
            tp_price = self._client.round_price(symbol, tp_price)

            logger.info(
                f"[{symbol}] Pozisyon Boyutu Hesaplandı ({signal_type}): Notional={qty * price:.2f}$ "
                f"(Marjin={required_margin:.2f}$ [{leverage}x]) | SL={sl_price} | TP={tp_price}"
            )
            return qty, sl_price, tp_price, leverage

        except Exception as exc:
            logger.error(f"[{symbol}] Pozisyon boyutu hesaplama hatası: {exc}")
            return None, None, None, leverage
