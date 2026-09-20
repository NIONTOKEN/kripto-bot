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
        self._trade_types: Dict[str, str] = {}       # symbol → signal_type (COLLAPSE_SHORT, SUPER_LONG, etc.)

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
        except Exception as exc:
            # -4168: Multi-Assets modunda izole marjin desteklenmez, bu normaldir
            logger.warning(f"[{symbol}] Margin tipi ayarlanamadı (Multi-Asset modu aktif olabilir): {exc}")

        try:
            await self._client.set_leverage(symbol, config.LEVERAGE)
        except Exception as exc:
            logger.error(f"[{symbol}] Leverage ayar hatası: {exc}")
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
            sl_resp = await self._client.place_stop_market(symbol, sl_side, sl_price, quantity=quantity)
            sl_order_id = str(sl_resp.get("orderId", ""))
            logger.info(f"PROTECTION {symbol} SL={sl_price:.4f} order_id={sl_order_id}")
        except Exception as exc:
            logger.warning(
                f"[{symbol}] Borsaya SL emri girilemedi ({exc}). "
                f"Bot içi canlı Stop-Loss devrede (SL: {sl_price:.4f})"
            )

        try:
            tp_resp = await self._client.place_take_profit_market(symbol, tp_side, tp_price, quantity=quantity)
            tp_order_id = str(tp_resp.get("orderId", ""))
            logger.info(f"PROTECTION {symbol} TP={tp_price:.4f} order_id={tp_order_id}")
        except Exception as exc:
            logger.warning(
                f"[{symbol}] Borsaya TP emri girilemedi ({exc}). "
                f"Bot içi canlı Take-Profit devrede (TP: {tp_price:.4f})"
            )

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
        self._trade_types[symbol] = signal.signal_type

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
        self._trade_types.pop(trade.symbol, None)

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
                close_side = "SELL" if direction == "LONG" else "BUY"

                # ── Bot İçi SL / TP Tetiklenme Kontrolü ───────────────────────
                hit_sl = (direction == "LONG" and mark_price <= trade.sl_price) or (direction == "SHORT" and mark_price >= trade.sl_price)
                hit_tp = (direction == "LONG" and mark_price >= trade.tp_price) or (direction == "SHORT" and mark_price <= trade.tp_price)

                if hit_sl or hit_tp:
                    reason = "TP" if hit_tp else "SL"
                    logger.info(f"[{symbol}] Hedef seviyeye ulaşıldı ({reason}) mark={mark_price} -> Pozisyon kapatılıyor")
                    try:
                        await self._client.close_position_market(symbol, close_side, trade.quantity)
                        pnl = (mark_price - trade.entry_price) * trade.quantity if direction == "LONG" else (trade.entry_price - mark_price) * trade.quantity
                        await self._close_trade(trade, reason, mark_price, pnl)
                        continue
                    except Exception as close_exc:
                        logger.error(f"[{symbol}] Pozisyon kapatma hatası: {close_exc}")

                # ── KÂR YÜZDESİ HESABI ───────────────────────────────────────
                entry_p = trade.entry_price
                if entry_p <= 0:
                    continue

                profit_pct = (
                    ((mark_price - entry_p) / entry_p * Decimal("100"))
                    if direction == "LONG"
                    else ((entry_p - mark_price) / entry_p * Decimal("100"))
                )

                sig_type = self._trade_types.get(
                    symbol, "COLLAPSE_SHORT" if direction == "SHORT" else "NORMAL"
                )
                is_macro = sig_type in ("COLLAPSE_SHORT", "SUPER_LONG")

                # ── 1. BREAK-EVEN KİLİTLEME (RİSKSİZ İŞLEM) ────────────────────
                be_sl = None
                be_thresh = Decimal("3.0") if is_macro else Decimal("1.2")
                if profit_pct >= be_thresh:
                    if direction == "LONG":
                        lock_mult = Decimal("1.010") if is_macro else Decimal("1.002")
                        target_be = self._client.round_price(symbol, entry_p * lock_mult)
                        if trade.sl_price < target_be:
                            be_sl = target_be
                    else:  # SHORT
                        lock_mult = Decimal("0.990") if is_macro else Decimal("0.998")
                        target_be = self._client.round_price(symbol, entry_p * lock_mult)
                        if trade.sl_price > target_be:
                            be_sl = target_be

                if be_sl:
                    tag = f"MAKRO {sig_type}" if is_macro else "GÜN İÇİ"
                    logger.info(
                        f"[{symbol}] {tag} BREAK-EVEN KİLİTLENDİ (Kâr=+%{profit_pct:.2f}) -> Risksiz trend sürüşü! Yeni SL={be_sl}"
                    )
                    await self._update_sl(trade, be_sl)

                # ── 2. DİNAMİK TREND TRAILING STOP (BÜYÜK TRENDLERİ SÜRMEK İÇİN)
                trail_thresh = Decimal("5.0") if is_macro else Decimal("2.5")
                if profit_pct >= trail_thresh:
                    best = self._trail_highs.get(symbol, mark_price)

                    if direction == "LONG":
                        if mark_price > best:
                            self._trail_highs[symbol] = mark_price
                        effective_high = max(best, mark_price)
                        trail_dist = effective_high * Decimal("0.038") if is_macro else entry_p * Decimal("0.018")
                        new_sl = self._client.round_price(symbol, effective_high - trail_dist)
                        if new_sl > trade.sl_price:
                            tag = "🚀 SÜPER BOĞA TRAILING" if is_macro else "TRAILING STOP"
                            logger.info(
                                f"[{symbol}] {tag} SÜRÜLDÜ (Zirve={effective_high}) -> Yeni SL={new_sl} (Kâr=+%{profit_pct:.2f})"
                            )
                            await self._update_sl(trade, new_sl)
                    else:  # SHORT (LUNA Çöküş Takibi)
                        if mark_price < best:
                            self._trail_highs[symbol] = mark_price
                        effective_low = min(best, mark_price)
                        trail_dist = effective_low * Decimal("0.038") if is_macro else entry_p * Decimal("0.018")
                        new_sl = self._client.round_price(symbol, effective_low + trail_dist)
                        if new_sl < trade.sl_price:
                            tag = "💀 LUNA ÇÖKÜŞ TRAILING" if is_macro else "TRAILING STOP"
                            logger.info(
                                f"[{symbol}] {tag} SÜRÜLDÜ (Dip={effective_low}) -> Yeni SL={new_sl} (Kâr=+%{profit_pct:.2f})"
                            )
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
            resp = await self._client.place_stop_market(symbol, sl_side, new_sl, quantity=trade.quantity)
            new_sl_id = str(resp.get("orderId", ""))
            await update_trade(trade.id, sl_price=new_sl, sl_order_id=new_sl_id)
            logger.info(f"TRAILING {symbol} yeni SL={new_sl:.4f}")
        except Exception as exc:
            # -4120 Algo kısıtlaması varsa bot içi SL'yi güncelle
            trade.sl_price = new_sl
            await update_trade(trade.id, sl_price=new_sl)
            logger.info(f"TRAILING (Bot İçi) {symbol} yeni SL={new_sl:.4f}")

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
