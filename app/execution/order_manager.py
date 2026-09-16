"""
Emir yöneticisi.
- Pozisyon açma (market order)
- Anında SL/TP yerleştirme
- Pozisyon takibi ve kapanma algılama
- Trailing stop güncelleme
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Callable, Dict, Optional

from app.config import config
from app.database import (
    Trade,
    get_open_trade_for_symbol,
    get_open_trades,
    save_trade,
    update_trade,
    update_daily_stat,
)
from app.exchange.client import BinanceClient
from app.strategy.signals import SignalResult
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Callback tipi: (trade: Trade, pnl: Decimal) async
CloseCallback = Callable[[Trade, Decimal], asyncio.Future]


class OrderManager:
    def __init__(
        self,
        client: BinanceClient,
        on_close: Optional[CloseCallback] = None,
    ) -> None:
        self._client = client
        self.on_close = on_close
        # Aktif trailing stop high water marks
        self._trail_highs: Dict[str, Decimal] = {}   # symbol → best price
        self._trail_sl_orders: Dict[str, int] = {}   # symbol → sl_order_id

    # ── Pozisyon Açma ─────────────────────────────────────────────────────────

    async def open_position(
        self,
        symbol: str,
        signal: SignalResult,
        quantity: Decimal,
        sl_price: Decimal,
        tp_price: Decimal,
    ) -> Optional[Trade]:
        """
        1. Leverage ve margin tip ayarla
        2. Market order gönder
        3. Hemen SL + TP yerleştir
        4. DB'ye kaydet
        """
        # Önce bu sembolde açık pozisyon var mı kontrol et (güvenlik)
        existing = await get_open_trade_for_symbol(symbol)
        if existing is not None:
            logger.warning(f"[{symbol}] Zaten açık pozisyon var, yeni pozisyon açılmıyor")
            return None

        direction = signal.direction  # LONG / SHORT
        side = "BUY" if direction == "LONG" else "SELL"
        sl_side = "SELL" if direction == "LONG" else "BUY"
        tp_side = "SELL" if direction == "LONG" else "BUY"

        # ── 1. Kaldıraç ve margin tipi ayarla ────────────────────────────────
        try:
            await self._client.set_isolated_margin(symbol)
            await self._client.set_leverage(symbol, config.LEVERAGE)
        except Exception as exc:
            logger.error(f"[{symbol}] Leverage/margin ayar hatası: {exc}")
            return None

        # ── 2. Market order ───────────────────────────────────────────────────
        try:
            order_resp = await self._client.place_market_order(symbol, side, quantity)
        except Exception as exc:
            logger.error(f"[{symbol}] Market order hatası: {exc}")
            return None

        order_id = str(order_resp.get("orderId", ""))
        fill_price_str = order_resp.get("avgPrice", "0")
        if fill_price_str == "0" or not fill_price_str:
            # avgPrice bazen 0 gelir, mark price al
            try:
                fill_price = await self._client.get_mark_price(symbol)
            except Exception:
                fill_price = Decimal(str(sl_price))  # fallback
        else:
            fill_price = Decimal(fill_price_str)

        logger.info(
            f"ORDER {symbol} {direction} qty={quantity} "
            f"fill={fill_price:.4f} order_id={order_id}"
        )

        # ── 3. SL/TP emirleri ─────────────────────────────────────────────────
        sl_order_id: Optional[str] = None
        tp_order_id: Optional[str] = None

        # Kısa gecikme — pozisyonun sisteme girmesi için
        await asyncio.sleep(0.5)

        try:
            sl_resp = await self._client.place_stop_market(symbol, sl_side, sl_price)
            sl_order_id = str(sl_resp.get("orderId", ""))
            logger.info(f"PROTECTION {symbol} SL={sl_price:.4f} order_id={sl_order_id}")
        except Exception as exc:
            logger.error(f"[{symbol}] SL yerleştirme BAŞARISIZ: {exc}. Pozisyon kapatılıyor!")
            # SL yerleştirilemezse pozisyonu kapat — asla SL'siz bırakma
            try:
                close_side = "SELL" if direction == "LONG" else "BUY"
                await self._client.close_position_market(symbol, close_side, quantity)
            except Exception as close_exc:
                logger.critical(f"[{symbol}] Pozisyon kapatma da başarısız: {close_exc}")
            return None

        try:
            tp_resp = await self._client.place_take_profit_market(symbol, tp_side, tp_price)
            tp_order_id = str(tp_resp.get("orderId", ""))
            logger.info(f"PROTECTION {symbol} TP={tp_price:.4f} order_id={tp_order_id}")
        except Exception as exc:
            logger.warning(f"[{symbol}] TP yerleştirme hatası (SL var): {exc}")

        # ── 4. DB'ye kaydet ───────────────────────────────────────────────────
        trade = Trade(
            symbol=symbol,
            side=direction,
            entry_price=fill_price,
            sl_price=sl_price,
            tp_price=tp_price,
            quantity=quantity,
            leverage=config.LEVERAGE,
            status="OPEN",
            entry_order_id=order_id,
            sl_order_id=sl_order_id,
            tp_order_id=tp_order_id,
            signal_score=signal.score,
            ml_confidence=signal.ml_confidence,
            regime=signal.regime.value,
            opened_at=datetime.utcnow(),
        )
        trade = await save_trade(trade)

        if config.TRAILING_STOP:
            self._trail_highs[symbol] = fill_price

        return trade

    # ── Pozisyon Kapanma ──────────────────────────────────────────────────────

    async def on_order_filled(self, msg: dict) -> None:
        """
        User data stream'den gelen ORDER_TRADE_UPDATE mesajı.
        SL veya TP dolduğunda pozisyonu DB'de kapat.
        """
        order_data = msg.get("o", {})
        status = order_data.get("X")  # order status
        if status != "FILLED":
            return

        symbol = order_data.get("s")
        order_id = str(order_data.get("i", ""))
        order_type = order_data.get("o", "")    # STOP_MARKET, TAKE_PROFIT_MARKET, MARKET
        avg_price = Decimal(order_data.get("ap", "0") or "0")
        realized_pnl = Decimal(order_data.get("rp", "0") or "0")

        if order_type not in ("STOP_MARKET", "TAKE_PROFIT_MARKET", "MARKET"):
            return

        trade = await get_open_trade_for_symbol(symbol)
        if trade is None:
            return

        # Kontrol: bu order bizim SL veya TP mi?
        is_sl = order_id == trade.sl_order_id
        is_tp = order_id == trade.tp_order_id
        is_manual = order_type == "MARKET" and order_id != trade.entry_order_id

        if not (is_sl or is_tp or is_manual):
            return

        close_reason = "SL" if is_sl else "TP" if is_tp else "MANUAL"
        await self._close_trade(trade, close_reason, avg_price, realized_pnl)

    async def _close_trade(
        self,
        trade: Trade,
        reason: str,
        close_price: Decimal,
        realized_pnl: Decimal,
    ) -> None:
        """Pozisyonu DB'de kapat ve callback'i çağır."""
        if close_price <= 0:
            close_price = trade.entry_price

        # Gerçekleşen PnL %
        if trade.entry_price and trade.entry_price > 0:
            pnl_pct = (
                realized_pnl / (trade.entry_price * trade.quantity / Decimal(str(trade.leverage)))
            ) * Decimal("100")
        else:
            pnl_pct = Decimal("0")

        await update_trade(
            trade.id,
            status="CLOSED",
            close_reason=reason,
            pnl=realized_pnl,
            pnl_pct=pnl_pct,
            closed_at=datetime.utcnow(),
        )

        win = realized_pnl > 0
        try:
            balance = await self._client.get_total_balance_usdt()
            await update_daily_stat(realized_pnl, win, balance)
        except Exception as exc:
            logger.warning(f"Daily stat güncellenemedi: {exc}")

        logger.info(
            f"CLOSE {trade.symbol} reason={reason} "
            f"pnl={realized_pnl:+.4f} USDT ({pnl_pct:+.2f}%)"
        )

        # Diğer açık emri iptal et (SL kapandıysa TP'yi, TP kapandıysa SL'yi)
        cancel_id: Optional[str] = None
        if reason == "SL":
            cancel_id = trade.tp_order_id
        elif reason == "TP":
            cancel_id = trade.sl_order_id

        if cancel_id:
            try:
                await self._client.cancel_order(trade.symbol, int(cancel_id))
            except Exception as exc:
                logger.warning(f"[{trade.symbol}] Ters emir iptal hatası: {exc}")

        # Trailing stop temizle
        self._trail_highs.pop(trade.symbol, None)
        self._trail_sl_orders.pop(trade.symbol, None)

        if self.on_close:
            try:
                await self.on_close(trade, realized_pnl)
            except Exception as exc:
                logger.error(f"on_close callback hatası: {exc}")

    # ── Trailing Stop ─────────────────────────────────────────────────────────

    async def update_trailing_stops(self) -> None:
        """
        Kârlı pozisyonlarda trailing stop güncelle.
        Sadece config.TRAILING_STOP = True ise çalışır.
        """
        if not config.TRAILING_STOP:
            return

        open_trades = await get_open_trades()
        for trade in open_trades:
            symbol = trade.symbol
            try:
                pos = await self._client.get_position(symbol)
                if pos is None:
                    continue

                mark_price = Decimal(pos.get("markPrice", "0"))
                if mark_price <= 0:
                    continue

                direction = trade.side  # LONG / SHORT
                best = self._trail_highs.get(symbol, mark_price)

                if direction == "LONG":
                    if mark_price > best:
                        self._trail_highs[symbol] = mark_price
                        new_sl = mark_price - Decimal(str(
                            float(trade.entry_price) * 0.01  # %1 trailing
                        ))
                        new_sl = max(new_sl, trade.sl_price)  # SL'yi asla düşürme
                        if new_sl > trade.sl_price:
                            await self._update_sl(trade, new_sl)
                else:  # SHORT
                    if mark_price < best:
                        self._trail_highs[symbol] = mark_price
                        new_sl = mark_price + Decimal(str(
                            float(trade.entry_price) * 0.01
                        ))
                        new_sl = min(new_sl, trade.sl_price)
                        if new_sl < trade.sl_price:
                            await self._update_sl(trade, new_sl)

            except Exception as exc:
                logger.error(f"[{symbol}] Trailing stop güncellenemedi: {exc}")

    async def _update_sl(self, trade: Trade, new_sl: Decimal) -> None:
        """Eski SL iptal et, yeni SL yerlestir."""
        symbol = trade.symbol
        direction = trade.side
        sl_side = "SELL" if direction == "LONG" else "BUY"

        # Eski SL iptal
        if trade.sl_order_id:
            try:
                await self._client.cancel_order(symbol, int(trade.sl_order_id))
            except Exception:
                pass  # zaten dolmuş olabilir

        # Yeni SL
        try:
            resp = await self._client.place_stop_market(symbol, sl_side, new_sl)
            new_sl_id = str(resp.get("orderId", ""))
            await update_trade(trade.id, sl_price=new_sl, sl_order_id=new_sl_id)
            logger.info(f"TRAILING {symbol} yeni SL={new_sl:.4f}")
        except Exception as exc:
            logger.error(f"[{symbol}] Yeni SL yerleştirilemedi: {exc}")

    # ── Pozisyon Senkronizasyonu ──────────────────────────────────────────────

    async def sync_positions(self) -> None:
        """
        Binance'deki gerçek pozisyonları DB ile senkronize et.
        Bot yeniden başlatıldığında veya periyodik olarak çağrılır.
        """
        try:
            open_trades = await get_open_trades()
            binance_positions = {p["symbol"]: p for p in await self._client.get_positions()}

            for trade in open_trades:
                if trade.symbol not in binance_positions:
                    # Pozisyon Binance'de yok ama DB'de OPEN
                    logger.warning(
                        f"[{trade.symbol}] DB'de OPEN ama Binance'de pozisyon yok. Kapatılıyor."
                    )
                    await update_trade(
                        trade.id,
                        status="CLOSED",
                        close_reason="SYNC",
                        closed_at=datetime.utcnow(),
                    )
        except Exception as exc:
            logger.error(f"Pozisyon senkronizasyon hatası: {exc}")
