"""
Ana bot orkestratörü.
Tüm modülleri birleştirir ve ana döngüyü yönetir.

Akış:
1. Başlangıç — DB, exchange client, WS manager, ML model
2. En yüksek hacimli sembolleri keşfet
3. Geçmiş veri yükle + ML eğit
4. WebSocket stream başlat
5. Her mum kapanışında: indikatör → sinyal → risk → emir
6. Periyodik: pozisyon senkronizasyonu, trailing stop, ML yeniden eğitim
"""
from __future__ import annotations

import asyncio
import traceback
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional

from app.config import config
from app.dashboard.server import app as fastapi_app, broadcast_ws, update_bot_state
from app.database import Signal, get_open_trades, init_db, save_signal
from app.exchange.client import BinanceClient
from app.exchange.websocket import MarketDataStore, UserDataStream, WebSocketManager
from app.execution.order_manager import OrderManager
from app.notifications.telegram import TelegramNotifier
from app.risk.manager import RiskManager
from app.strategy.indicators import calculate_indicators
from app.strategy.ml_model import MLModel
from app.strategy.regime import detect_regime
from app.strategy.signals import calculate_signal
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Global bot durdurucu bayrak (fatal hata durumunda yeni pozisyon açmayı durdur)
_fatal_error = False


class TradingBot:
    def __init__(self) -> None:
        self.client = BinanceClient()
        self.store = MarketDataStore(max_candles=config.KLINE_LIMIT + 50)
        self.ws_manager: Optional[WebSocketManager] = None
        self.user_stream: Optional[UserDataStream] = None
        self.order_manager: Optional[OrderManager] = None
        self.risk_manager: Optional[RiskManager] = None
        self.ml_model = MLModel()
        self.notifier = TelegramNotifier()
        self._symbols: List[str] = []
        self._running = False
        self._processing: Dict[str, bool] = {}  # symbol → işlemde mi?

    # ── Başlangıç ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        global _fatal_error
        logger.info("=" * 60)
        logger.info("BOT BAŞLATILIYOR")
        logger.info(f"Ortam: {'TESTNET' if config.BINANCE_TESTNET else '*** MAINNET ***'}")
        logger.info("=" * 60)

        # ── 1. Veritabanı ─────────────────────────────────────────────────────
        await init_db()

        # ── 2. Exchange client ────────────────────────────────────────────────
        await self.client.start()

        # ── 3. Bakiye doğrula ve bekleme döngüsü ──────────────────────────────
        while self._running or not hasattr(self, '_started_once'):
            self._started_once = True
            try:
                balance = await self.client.get_balance_usdt()
                total_balance = await self.client.get_total_balance_usdt()
                update_bot_state(
                    running=True,
                    balance=float(balance),
                    testnet=config.BINANCE_TESTNET,
                )
                logger.info(f"Kullanılabilir bakiye: {balance:.4f} USDT | Toplam: {total_balance:.4f} USDT")

                if balance >= config.MIN_BALANCE_USDT:
                    break  # Bakiye yeterli, işleme geç
                
                logger.info(f"Bakiye bekleniyor ({balance:.2f} < {config.MIN_BALANCE_USDT} USDT). 10sn sonra tekrar kontrol edilecek...")
                await asyncio.sleep(10)
            except Exception as exc:
                logger.warning(f"Bakiye kontrol hatası: {exc}. 10sn sonra tekrar denenecek...")
                await asyncio.sleep(10)

        # ── 4. Risk manager ───────────────────────────────────────────────────
        self.risk_manager = RiskManager(self.client)
        await self.risk_manager.refresh_daily_baseline(balance)

        # ── 5. Order manager ──────────────────────────────────────────────────
        self.order_manager = OrderManager(
            self.client,
            on_close=self._on_position_close,
        )

        # ── 6. Sembolleri keşfet ──────────────────────────────────────────────
        self._symbols = await self._discover_symbols()
        if not self._symbols:
            logger.critical("Hiç uygun sembol bulunamadı. Bot durduruldu.")
            return

        # ── 7. Geçmiş veri yükle + ML eğit ───────────────────────────────────
        await self._seed_all_symbols()

        # ── 8. Binance'deki açık pozisyonları senkronize et ───────────────────
        await self.order_manager.sync_positions()

        # ── 9. WebSocket başlat ───────────────────────────────────────────────
        self.ws_manager = WebSocketManager(
            self.store, on_kline_close=self._on_kline_close
        )
        self.ws_manager.set_symbols(self._symbols, config.PRIMARY_TF)
        await self.ws_manager.start()

        # ── 10. User data stream ──────────────────────────────────────────────
        self.user_stream = UserDataStream(
            self.client,
            on_order_update=self.order_manager.on_order_filled,
        )
        await self.user_stream.start()

        # ── 11. Dashboard state güncelle ──────────────────────────────────────
        update_bot_state(
            running=True,
            balance=float(balance),
            open_positions=len(await get_open_trades()),
            symbols_tracked=len(self._symbols),
            symbols_list=self._symbols,
            testnet=config.BINANCE_TESTNET,
            uptime_start=datetime.utcnow().isoformat(),
        )

        await self.notifier.bot_started(balance, len(self._symbols))

        self._running = True
        logger.info(f"Bot hazır — {len(self._symbols)} sembol takip ediliyor")

        # ── 12. Periyodik görevler ────────────────────────────────────────────
        asyncio.create_task(self._periodic_tasks())

    async def stop(self) -> None:
        self._running = False
        if self.ws_manager:
            await self.ws_manager.stop()
        if self.user_stream:
            await self.user_stream.stop()
        await self.client.stop()
        logger.info("Bot durduruldu")

    # ── Sembol Keşfi ──────────────────────────────────────────────────────────

    async def _discover_symbols(self) -> List[str]:
        """
        24 saatlik işlem hacmine göre en yüksek N sembolü döner.
        Kara listedekiler, kaldırılmış ve aktif olmayanlar hariçtutulur.
        """
        try:
            ticker_data = await self.client.get_ticker_24h_all()
        except Exception as exc:
            logger.error(f"Ticker verisi alınamadı: {exc}")
            return []

        valid_symbols = set(self.client.symbol_info.keys())
        blacklist = set(config.BLACKLIST)

        scored = []
        for t in ticker_data:
            sym = t.get("symbol", "")
            if sym not in valid_symbols:
                continue
            if sym in blacklist:
                continue
            if not sym.endswith("USDT"):
                continue
            try:
                vol = float(t.get("quoteVolume", 0))
                scored.append((sym, vol))
            except (ValueError, TypeError):
                pass

        scored.sort(key=lambda x: x[1], reverse=True)
        symbols = [s for s, _ in scored[: config.TOP_SYMBOLS_COUNT]]
        logger.info(f"Keşfedilen semboller: {', '.join(symbols)}")
        return symbols

    # ── Geçmiş Veri Yükleme ───────────────────────────────────────────────────

    async def _seed_symbol(self, symbol: str) -> None:
        """Tek sembol için geçmiş veri yükle ve ML eğit."""
        try:
            klines = await self.client.get_klines(
                symbol, config.PRIMARY_TF, limit=config.ML_HISTORY_CANDLES
            )
            candles = [
                {
                    "open_time": k[0],
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                    "close_time": k[6],
                    "quote_volume": float(k[7]),
                    "num_trades": k[8],
                }
                for k in klines
            ]
            self.store.seed(symbol, candles)
            await self.ml_model.train_symbol(symbol, candles)
        except Exception as exc:
            logger.warning(f"[{symbol}] Geçmiş veri yükleme hatası: {exc}")

    async def _seed_all_symbols(self) -> None:
        """Tüm sembolleri paralel olarak yükle."""
        logger.info(f"Geçmiş veri yükleniyor ({len(self._symbols)} sembol)...")
        tasks = [self._seed_symbol(s) for s in self._symbols]
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("Geçmiş veri yüklendi")

    # ── Kline İşleme ──────────────────────────────────────────────────────────

    async def _on_kline_close(self, symbol: str, kline: dict) -> None:
        """
        WebSocket'ten kapanmış mum geldiğinde çağrılır.
        Her sembol için ayrı ayrı işlenir.
        """
        # Eğer bu sembol zaten işleniyorsa skip et
        if self._processing.get(symbol):
            return
        self._processing[symbol] = True
        try:
            await self._process_symbol(symbol)
        except Exception as exc:
            # Hata tek sembolü etkiler, diğerleri devam eder
            logger.error(f"[{symbol}] İşlem hatası: {exc}")
            logger.debug(traceback.format_exc())
        finally:
            self._processing[symbol] = False

    async def _process_symbol(self, symbol: str) -> None:
        """
        Tek sembol için tam analiz ve sinyal akışı.
        Güvenlik kontrolleri dahil.
        """
        global _fatal_error

        # ── 1. Veri tazeliği kontrolü ─────────────────────────────────────────
        if self.store.is_stale(symbol):
            logger.warning(f"[{symbol}] Eski veri (>{config.STALE_DATA_SECONDS}sn), atlanıyor")
            return

        candles = self.store.get(symbol)
        if len(candles) < 210:
            return  # Yeterli veri yok

        # ── 2. İndikatörler ───────────────────────────────────────────────────
        ind = calculate_indicators(candles)
        if ind is None:
            logger.debug(f"[{symbol}] İndikatör hesaplanamadı (yetersiz veri)")
            return

        # ── 3. Rejim ──────────────────────────────────────────────────────────
        regime = detect_regime(ind)

        # ── 4. ML olasılığı ───────────────────────────────────────────────────
        if self.ml_model.needs_retrain(symbol):
            # Arka planda eğit
            asyncio.create_task(
                self.ml_model.train_symbol(symbol, candles)
            )

        ml_prob = self.ml_model.predict_long_probability(symbol, candles)

        # ── 5. Sinyal skoru ───────────────────────────────────────────────────
        signal = calculate_signal(ind, ml_prob, regime)

        # ── 6. Sinyali logla + DB'ye kaydet ──────────────────────────────────
        if signal.score >= 30:  # Düşük skorları da kaydet (analiz için)
            logger.info(
                f"SIGNAL {symbol} {signal.direction} "
                f"score={signal.score:.1f} confidence={signal.ml_confidence:.2f} "
                f"regime={regime.value}"
            )
            await save_signal(Signal(
                symbol=symbol,
                direction=signal.direction,
                score=signal.score,
                ml_confidence=signal.ml_confidence,
                regime=regime.value,
                rsi=ind.rsi,
                macd_hist=ind.macd_hist,
                adx=ind.adx,
                atr=ind.atr,
            ))

        # ── 7. NEUTRAL → işlem yok ────────────────────────────────────────────
        if signal.direction == "NEUTRAL" or signal.score < config.MIN_SCORE_TO_OPEN:
            return

        # ── 8. Fatal error → yeni pozisyon açma ──────────────────────────────
        if _fatal_error:
            return

        # ── 9. Açık pozisyon var mı? ──────────────────────────────────────────
        from app.database import get_open_trade_for_symbol
        existing = await get_open_trade_for_symbol(symbol)
        if existing is not None:
            return  # Aynı sembolde pozisyon var

        # ── 10. Risk kontrolleri ─────────────────────────────────────────────
        try:
            balance = await self.client.get_balance_usdt()
        except Exception as exc:
            logger.error(f"Bakiye alınamadı: {exc}. Pozisyon açılmıyor.")
            return

        open_trades = await get_open_trades()
        risk_check = await self.risk_manager.check_can_open(
            symbol, len(open_trades), balance
        )
        if not risk_check.allowed:
            logger.info(f"[{symbol}] Risk: {risk_check.reason}")
            return

        # ── 11. Pozisyon boyutu ───────────────────────────────────────────────
        qty, sl_price, tp_price = self.risk_manager.calculate_position_size(
            symbol, ind, balance, signal.direction
        )
        if qty is None or sl_price is None or tp_price is None:
            logger.warning(f"[{symbol}] Pozisyon boyutu hesaplanamadı")
            return

        # ── 12. Emir gönder ───────────────────────────────────────────────────
        trade = await self.order_manager.open_position(
            symbol, signal, qty, sl_price, tp_price
        )
        if trade is None:
            return

        # ── 13. Telegram bildirimi ────────────────────────────────────────────
        await self.notifier.signal(
            symbol, signal.direction, signal.score,
            signal.ml_confidence, regime.value
        )
        await self.notifier.order_opened(
            symbol, signal.direction, qty,
            Decimal(str(ind.close)), sl_price, tp_price, config.LEVERAGE
        )

        # ── 14. Dashboard güncelle ────────────────────────────────────────────
        open_count = len(await get_open_trades())
        update_bot_state(open_positions=open_count, balance=float(balance))
        await broadcast_ws({"type": "position_update"})

    # ── Pozisyon Kapanma Callback ─────────────────────────────────────────────

    async def _on_position_close(self, trade, pnl: Decimal) -> None:
        """OrderManager pozisyon kapandığında bunu çağırır."""
        pnl_pct = trade.pnl_pct or Decimal("0")
        await self.notifier.order_closed(
            trade.symbol, trade.side, pnl, pnl_pct, trade.close_reason or "?"
        )
        # Dashboard güncelle
        try:
            balance = await self.client.get_total_balance_usdt()
            open_count = len(await get_open_trades())
            daily_pnl = float(await self._get_daily_pnl())
            update_bot_state(
                balance=float(balance),
                open_positions=open_count,
                daily_pnl=daily_pnl,
            )
            await broadcast_ws({"type": "trade_closed", "symbol": trade.symbol})
        except Exception as exc:
            logger.warning(f"Dashboard güncelleme hatası: {exc}")

    async def _get_daily_pnl(self) -> Decimal:
        from app.database import get_todays_closed_pnl
        try:
            return await get_todays_closed_pnl()
        except Exception:
            return Decimal("0")

    # ── Periyodik Görevler ────────────────────────────────────────────────────

    async def _periodic_tasks(self) -> None:
        """
        Arka planda çalışan periyodik görevler.
        """
        pos_sync_interval = config.POSITION_CHECK_INTERVAL
        status_update_interval = 60
        pos_sync_counter = 0
        status_counter = 0

        while self._running:
            await asyncio.sleep(10)
            pos_sync_counter += 10
            status_counter += 10

            # ── Pozisyon senkronizasyonu ──────────────────────────────────────
            if pos_sync_counter >= pos_sync_interval:
                pos_sync_counter = 0
                try:
                    await self.order_manager.sync_positions()
                    if config.TRAILING_STOP:
                        await self.order_manager.update_trailing_stops()
                except Exception as exc:
                    logger.error(f"Periyodik senkronizasyon hatası: {exc}")

            # ── Dashboard durum güncellemesi ──────────────────────────────────
            if status_counter >= status_update_interval:
                status_counter = 0
                try:
                    balance = await self.client.get_total_balance_usdt()
                    open_count = len(await get_open_trades())
                    daily_pnl = float(await self._get_daily_pnl())
                    update_bot_state(
                        running=True,
                        balance=float(balance),
                        open_positions=open_count,
                        daily_pnl=daily_pnl,
                    )
                    await broadcast_ws({
                        "type": "status",
                        "data": {
                            "balance_usdt": float(balance),
                            "open_positions": open_count,
                            "daily_pnl": daily_pnl,
                            "running": True,
                            "testnet": config.BINANCE_TESTNET,
                        }
                    })
                except Exception as exc:
                    logger.warning(f"Durum güncellemesi hatası: {exc}")


# Singleton bot instance
_bot: Optional[TradingBot] = None


def get_bot() -> Optional[TradingBot]:
    return _bot


async def run_bot() -> None:
    """Bot'u başlat ve çalıştır."""
    global _bot, _fatal_error
    _bot = TradingBot()
    try:
        await _bot.start()
        # Bot başlatıldıktan sonra burada bekle
        while _bot._running:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        logger.info("Kullanıcı tarafından durduruldu")
    except Exception as exc:
        _fatal_error = True
        logger.critical(f"FATAL HATA: {exc}")
        logger.critical(traceback.format_exc())
        if _bot and _bot.notifier:
            await _bot.notifier.error_alert(
                f"Bot fatal hata ile durduruldu:\n{exc}"
            )
        # Mevcut pozisyonlar korunmaya devam eder (SL/TP Binance'de kalır)
    finally:
        if _bot:
            await _bot.stop()
