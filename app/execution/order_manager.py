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
        leverage: Optional[int] = None,
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

        target_leverage = leverage if leverage is not None else config.LEVERAGE
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
            await self._client.set_leverage(symbol, target_leverage)
        except Exception as exc:
            logger.error(f"[{symbol}] Leverage ({target_leverage}x) ayar hatası: {exc}")
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
            leverage=target_leverage,
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
                is_scalp = sig_type.startswith("SCALP")
                is_macro = sig_type in ("COLLAPSE_SHORT", "SUPER_LONG")

                if symbol not in self._trail_highs:
                    self._trail_highs[symbol] = mark_price

                # ── 0. EMİR DEFTERİ TERS BASKI ÇIKIŞI (VUR-KAÇ HIZLI KÂR AL) ───
                # Pozisyon kârdaysa (+%0.70 üzeri) ve karşı tarafta ani satış/alış duvarı veya ters baskı gelirse kârı cebe koyup hemen çık!
                if profit_pct >= Decimal("0.70") and not is_macro:
                    try:
                        depth = await self._client.get_order_book(symbol, limit=20)
                        from app.strategy.orderbook import analyze_order_book
                        ob = analyze_order_book(depth, float(mark_price))

                        should_quick_tp = False
                        wall_info = ""
                        if direction == "LONG" and (ob.imbalance <= -0.30 or ob.has_ask_wall):
                            should_quick_tp = True
                            wall_info = f"Satış duvarı={ob.ask_wall_price}" if ob.has_ask_wall else f"Satıcı Dengesizliği={ob.imbalance:.2f}"
                        elif direction == "SHORT" and (ob.imbalance >= 0.30 or ob.has_bid_wall):
                            should_quick_tp = True
                            wall_info = f"Alış duvarı={ob.bid_wall_price}" if ob.has_bid_wall else f"Alıcı Dengesizliği={ob.imbalance:.2f}"

                        if should_quick_tp:
                            logger.info(
                                f"[{symbol}] ⚡ VUR-KAÇ HIZLI KÂR KORUMA (+%{profit_pct:.2f}) - {wall_info} tespit edildi! Kâr cebe kilitleniyor."
                            )
                            await self._client.close_position_market(symbol, close_side, trade.quantity)
                            pnl = (mark_price - trade.entry_price) * trade.quantity if direction == "LONG" else (trade.entry_price - mark_price) * trade.quantity
                            await self._close_trade(trade, "QUICK_TP", mark_price, pnl)
                            continue
                    except Exception as ob_exc:
                        logger.debug(f"[{symbol}] Derinlik analiz hatası: {ob_exc}")

                # ── 1. KADEME 1: BREAK-EVEN KİLİTLEME (+%0.55 KÂRDA SIFIR RİSK) ─
                be_thresh = Decimal("2.5") if is_macro else Decimal("0.55")
                if profit_pct >= be_thresh:
                    if direction == "LONG":
                        lock_mult = Decimal("1.008") if is_macro else Decimal("1.001")
                        target_be = self._client.round_price(symbol, entry_p * lock_mult)
                        if trade.sl_price < target_be:
                            tag = f"MAKRO {sig_type}" if is_macro else "⚡ VUR-KAÇ"
                            logger.info(
                                f"[{symbol}] {tag} BREAK-EVEN KİLİTLENDİ (Kâr=+%{profit_pct:.2f}) -> Yeni SL={target_be}"
                            )
                            await self._update_sl(trade, target_be)
                    else:  # SHORT
                        lock_mult = Decimal("0.992") if is_macro else Decimal("0.999")
                        target_be = self._client.round_price(symbol, entry_p * lock_mult)
                        if trade.sl_price > target_be:
                            tag = f"MAKRO {sig_type}" if is_macro else "⚡ VUR-KAÇ"
                            logger.info(
                                f"[{symbol}] {tag} BREAK-EVEN KİLİTLENDİ (Kâr=+%{profit_pct:.2f}) -> Yeni SL={target_be}"
                            )
                            await self._update_sl(trade, target_be)

                # ── 2. KADEME 2: ASGARİ KÂR KİLİTLEME (+%1.00 KÂRDA ASGARİ %0.50 CEPTE)
                lock_thresh = Decimal("1.00")
                if profit_pct >= lock_thresh and not is_macro:
                    if direction == "LONG":
                        target_lock = self._client.round_price(symbol, entry_p * Decimal("1.005"))
                        if trade.sl_price < target_lock:
                            logger.info(
                                f"[{symbol}] 🔒 ASGARİ KÂR KİLİTLENDİ (+%{profit_pct:.2f}) -> SL={target_lock} (+%0.50 kâr garanti)"
                            )
                            await self._update_sl(trade, target_lock)
                    else:  # SHORT
                        target_lock = self._client.round_price(symbol, entry_p * Decimal("0.995"))
                        if trade.sl_price > target_lock:
                            logger.info(
                                f"[{symbol}] 🔒 ASGARİ KÂR KİLİTLENDİ (+%{profit_pct:.2f}) -> SL={target_lock} (+%0.50 kâr garanti)"
                            )
                            await self._update_sl(trade, target_lock)

                # ── 3. KADEME 3: DİNAMİK YAKIN İZ SÜREN STOP (TRAILING STOP) ────
                trail_thresh = Decimal("4.0") if is_macro else Decimal("1.35")
                if profit_pct >= trail_thresh:
                    best = self._trail_highs.get(symbol, mark_price)

                    if direction == "LONG":
                        if mark_price > best:
                            self._trail_highs[symbol] = mark_price
                        effective_high = max(best, mark_price)

                        # Scalp için zirveden sadece %0.4 geriden izle! Kârın erimesine ASLA izin verme
                        if is_scalp:
                            trail_dist = effective_high * Decimal("0.004")
                        elif is_macro:
                            trail_dist = effective_high * Decimal("0.035")
                        else:
                            trail_dist = effective_high * Decimal("0.006")

                        new_sl = self._client.round_price(symbol, effective_high - trail_dist)
                        if new_sl > trade.sl_price:
                            tag = "🚀 SÜPER BOĞA TRAIL" if is_macro else "⚡ SIKI TRAILING"
                            logger.info(
                                f"[{symbol}] {tag} ZİRVEDEN İZ SÜRDÜ (Zirve={effective_high}) -> Yeni SL={new_sl} (Kâr=+%{profit_pct:.2f})"
                            )
                            await self._update_sl(trade, new_sl)
                    else:  # SHORT
                        if mark_price < best:
                            self._trail_highs[symbol] = mark_price
                        effective_low = min(best, mark_price)

                        # Scalp için dipten sadece %0.4 geriden izle!
                        if is_scalp:
                            trail_dist = effective_low * Decimal("0.004")
                        elif is_macro:
                            trail_dist = effective_low * Decimal("0.035")
                        else:
                            trail_dist = effective_low * Decimal("0.006")

                        new_sl = self._client.round_price(symbol, effective_low + trail_dist)
                        if new_sl < trade.sl_price:
                            tag = "💀 ÇÖKÜŞ TRAIL" if is_macro else "⚡ SIKI TRAILING"
                            logger.info(
                                f"[{symbol}] {tag} DİPTEN İZ SÜRDÜ (Dip={effective_low}) -> Yeni SL={new_sl} (Kâr=+%{profit_pct:.2f})"
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
            open_symbols = {t.symbol: t for t in open_trades}
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

            # Binance'de olup veritabanında olmayan pozisyonları veritabanına aktar
            for sym, pos in binance_positions.items():
                if sym not in open_symbols:
                    pos_amt = Decimal(pos.get("positionAmt", "0"))
                    if pos_amt == Decimal("0"):
                        continue
                    entry_p = Decimal(pos.get("entryPrice", "0"))
                    side = "LONG" if pos_amt > 0 else "SHORT"
                    qty = abs(pos_amt)
                    leverage = int(pos.get("leverage", config.LEVERAGE))
                    mark_p = Decimal(pos.get("markPrice", str(entry_p)))

                    sl_price = entry_p * Decimal("0.97") if side == "LONG" else entry_p * Decimal("1.03")
                    tp_price = entry_p * Decimal("1.06") if side == "LONG" else entry_p * Decimal("0.94")

                    new_trade = Trade(
                        symbol=sym,
                        side=side,
                        entry_price=entry_p,
                        sl_price=sl_price,
                        tp_price=tp_price,
                        quantity=qty,
                        leverage=leverage,
                        status="OPEN",
                        opened_at=datetime.utcnow(),
                        signal_score=50.0,
                        ml_confidence=0.5,
                        regime="RANGING",
                    )
                    await save_trade(new_trade)
                    self._trail_highs[sym] = mark_p
                    logger.info(
                        f"[{sym}] Binance'deki açık pozisyon DB'ye senkronize edildi: {side} {qty} @ {entry_p}"
                    )
        except Exception as exc:
            logger.error(f"Pozisyon senkronizasyon hatası: {exc}")
