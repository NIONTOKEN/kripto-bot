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
        self._sl_cooldown: Dict[str, float] = {}  # symbol → cooldown bitiş timestamp
        self._htf_cache: Dict[str, Tuple[float, str, str]] = {}  # symbol → (ts, trend_1h, trend_4h)

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
        balance = Decimal("0")
        wallet_bal = Decimal("0")
        total_balance = Decimal("0")
        unrealized = Decimal("0")

        while self._running or not hasattr(self, '_started_once'):
            self._started_once = True
            try:
                balance = await self.client.get_balance_usdt()
                wallet_bal = await self.client.get_wallet_balance_usdt()
                total_balance = await self.client.get_total_balance_usdt()
                unrealized = total_balance - wallet_bal
                update_bot_state(
                    running=True,
                    balance=float(total_balance),
                    wallet_balance=float(wallet_bal),
                    unrealized_pnl=float(unrealized),
                    testnet=config.BINANCE_TESTNET,
                )
                logger.info(f"Kullanılabilir bakiye: {balance:.4f} USDT | Toplam: {total_balance:.4f} USDT")

                if total_balance >= config.MIN_BALANCE_USDT:
                    break  # Toplam bakiye yeterli, işleme ve pozisyon takibine geç
                
                logger.info(f"Bakiye bekleniyor (Toplam: {total_balance:.2f} < {config.MIN_BALANCE_USDT} USDT). 10sn sonra tekrar kontrol edilecek...")
                await asyncio.sleep(10)
            except Exception as exc:
                logger.warning(f"Bakiye kontrol hatası: {exc}. 10sn sonra tekrar denenecek...")
                await asyncio.sleep(10)

        # ── 4. Risk manager ───────────────────────────────────────────────────
        self.risk_manager = RiskManager(self.client)
        await self.risk_manager.refresh_daily_baseline(total_balance)

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
        try:
            wallet_bal = await self.client.get_wallet_balance_usdt()
            total_balance = await self.client.get_total_balance_usdt()
            unrealized = total_balance - wallet_bal
        except Exception:
            pass

        update_bot_state(
            running=True,
            balance=float(total_balance),
            wallet_balance=float(wallet_bal),
            unrealized_pnl=float(unrealized),
            open_positions=len(await get_open_trades()),
            symbols_tracked=len(self._symbols),
            symbols_list=self._symbols,
            testnet=config.BINANCE_TESTNET,
            uptime_start=datetime.utcnow().isoformat(),
        )

        await self.notifier.bot_started(total_balance, len(self._symbols))

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
        """Sembol geçmiş verilerini rate limit'e takılmadan sırayla yükle."""
        logger.info(f"Geçmiş veri yükleniyor ({len(self._symbols)} sembol)...")
        for s in self._symbols:
            await self._seed_symbol(s)
            await asyncio.sleep(0.25)  # Binance rate limit koruması
        logger.info("Geçmiş veri başarıyla yüklendi")

    # ── Kline İşleme ──────────────────────────────────────────────────────────

    async def _on_kline_close(self, symbol: str, kline: dict) -> None:
        """
        WebSocket'ten kapanmış mum geldiğinde çağrılır.
        Her sembol için ayrı ayrı işlenir.
        """
        logger.info(f"[{symbol}] Mum kapandı → işleniyor")
        # Eğer bu sembol zaten işleniyorsa skip et
        if self._processing.get(symbol):
            logger.debug(f"[{symbol}] Zaten işleniyor, atlanıyor")
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
        WebSocket veri gelmiyorsa REST'ten taze veri çeker.
        """
        global _fatal_error

        # ── 1. Veri tazeliği kontrolü — stale ise REST'ten taze çek ──────────
        if self.store.is_stale(symbol):
            try:
                klines = await self.client.get_klines(
                    symbol, config.PRIMARY_TF, limit=config.ML_HISTORY_CANDLES
                )
                candles_fresh = [
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
                self.store.seed(symbol, candles_fresh)
            except Exception as exc:
                logger.warning(f"[{symbol}] REST veri yenileme hatası: {exc}, atlanıyor")
                return

        candles = self.store.get(symbol)
        if len(candles) < 210:
            logger.warning(f"[{symbol}] Yetersiz mum: {len(candles)}/210")
            return

        # ── 1.5. Stop Loss Soğuma Süresi Kontrolü ─────────────────────────────
        import time
        is_cooling_down = time.time() < self._sl_cooldown.get(symbol, 0)
        if is_cooling_down:
            rem = int(self._sl_cooldown[symbol] - time.time())
            logger.debug(f"[{symbol}] Stop Loss sonrası soğuma süresinde ({rem}s kaldı)")

        # ── 2. İndikatörler ───────────────────────────────────────────────────
        ind = calculate_indicators(candles)
        if ind is None:
            logger.warning(f"[{symbol}] İndikatör hesaplanamadı (yetersiz veri)")
            return

        # ── 2.5. BTC Makro Piyasa Trendi (Tüm Altcoinler İçin Ana Pusula) ─────
        btc_bullish = None
        btc_candles = self.store.get("BTCUSDT")
        if btc_candles and len(btc_candles) >= 50:
            btc_ind = calculate_indicators(btc_candles)
            if btc_ind:
                if btc_ind.close >= btc_ind.ema50 and btc_ind.ema9 >= btc_ind.ema21:
                    btc_bullish = True
                elif btc_ind.close <= btc_ind.ema50 and btc_ind.ema9 <= btc_ind.ema21:
                    btc_bullish = False

        # ── 3. Rejim ──────────────────────────────────────────────────────────
        regime = detect_regime(ind)

        # ── 4. ML olasılığı ───────────────────────────────────────────────────
        if self.ml_model.needs_retrain(symbol):
            # Arka planda eğit
            asyncio.create_task(
                self.ml_model.train_symbol(symbol, candles)
            )

        ml_prob = self.ml_model.predict_long_probability(symbol, candles)

        # ── 4.5. Canlı Emir Defteri (Order Book & Depth) Analizi ──────────────
        from app.strategy.orderbook import analyze_order_book
        ob_analysis = None
        try:
            depth = await self.client.get_order_book(symbol, limit=20)
            ob_analysis = analyze_order_book(depth, ind.close)
        except Exception as exc:
            logger.debug(f"[{symbol}] Order book alınamadı: {exc}")

        # ── 4.8. Çoklu Zaman Dilimi (1H + 4H) Makro Trend Analizi ──────────
        # 10 dakikalık hafıza önbelleği (API kotasını korur ve tarama hızını artırır)
        import time
        now_ts = time.time()
        cached_htf = self._htf_cache.get(symbol)
        if cached_htf and (now_ts - cached_htf[0] < 600):
            trend_1h, trend_4h = cached_htf[1], cached_htf[2]
        else:
            trend_1h = "NEUTRAL"
            trend_4h = "NEUTRAL"
            try:
                import pandas as pd
                klines_1h = await self.client.get_klines(symbol, "1h", limit=40)
                if klines_1h and len(klines_1h) >= 20:
                    closes_1h = [float(k[4]) for k in klines_1h]
                    c_now_1h = closes_1h[-1]
                    s_1h = pd.Series(closes_1h)
                    ema20_1h = float(s_1h.ewm(span=20, adjust=False).mean().iloc[-1])
                    ema40_1h = float(s_1h.ewm(span=40, adjust=False).mean().iloc[-1])
                    if c_now_1h > ema20_1h and ema20_1h >= ema40_1h:
                        trend_1h = "BULLISH"
                    elif c_now_1h < ema20_1h and ema20_1h <= ema40_1h:
                        trend_1h = "BEARISH"

                klines_4h = await self.client.get_klines(symbol, "4h", limit=30)
                if klines_4h and len(klines_4h) >= 20:
                    closes_4h = [float(k[4]) for k in klines_4h]
                    c_now_4h = closes_4h[-1]
                    s_4h = pd.Series(closes_4h)
                    ema20_4h = float(s_4h.ewm(span=20, adjust=False).mean().iloc[-1])
                    ema50_4h = float(s_4h.ewm(span=50, adjust=False).mean().iloc[-1])
                    if c_now_4h > ema20_4h and ema20_4h >= ema50_4h:
                        trend_4h = "BULLISH"
                    elif c_now_4h < ema20_4h and ema20_4h <= ema50_4h:
                        trend_4h = "BEARISH"

                self._htf_cache[symbol] = (now_ts, trend_1h, trend_4h)
            except Exception as htf_err:
                logger.debug(f"[{symbol}] HTF (1H/4H) analiz hatası: {htf_err}")

        # ── 5. Sinyal skoru (AlphaPulse 10-Faktör + Tahta + Trend + HTF Hibrit) ─
        funding_rate = 0.0
        try:
            funding_rate = await self.client.get_funding_rate(symbol)
        except Exception:
            pass

        signal = calculate_signal(
            ind,
            ml_prob,
            regime,
            ob=ob_analysis,
            btc_bullish=btc_bullish,
            trend_1h=trend_1h,
            trend_4h=trend_4h,
            candles=candles,
            funding_rate=funding_rate,
        )

        # Soğuma süresindeyse yönü nötrle (işlem açma)
        if is_cooling_down:
            signal.direction = "NEUTRAL"

        # ── 6. Her sembolü logla (AI Radar + debug için) ──────────────────────
        ob_info = f"Tahta={ob_analysis.bias} (%{ob_analysis.imbalance*100:+.0f})" if ob_analysis else "Tahta=—"
        btc_str = "BTC=BULL" if btc_bullish is True else "BTC=BEAR" if btc_bullish is False else "BTC=—"
        type_str = f" [{signal.signal_type}]" if signal.signal_type != "NORMAL" else ""
        logger.info(
            f"[{symbol}] skor={signal.score:.1f} (Alpha={signal.alpha_score:.0f}) yön={signal.direction}{type_str} "
            f"RSI={ind.rsi:.1f} {ob_info} {btc_str} 4H={trend_4h} 1H={trend_1h} rejim={regime.value}"
        )

        # Skoru her zaman kaydet (AI Radar tablosu için)
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
            available_balance = await self.client.get_balance_usdt()
            total_balance = await self.client.get_total_balance_usdt()
        except Exception as exc:
            logger.error(f"Bakiye alınamadı: {exc}. Pozisyon açılmıyor.")
            return

        open_trades = await get_open_trades()
        risk_check = await self.risk_manager.check_can_open(
            symbol, len(open_trades), total_balance
        )
        if not risk_check.allowed:
            logger.info(f"[{symbol}] Risk: {risk_check.reason}")
            return

        # ── 11. Pozisyon boyutu (Dinamik Kaldıraç & Trend Tipi Odaklı) ───────
        qty, sl_price, tp_price, leverage = self.risk_manager.calculate_position_size(
            symbol, ind, total_balance, signal.direction, signal_type=signal.signal_type
        )
        if qty is None or sl_price is None or tp_price is None:
            logger.warning(f"[{symbol}] Pozisyon boyutu hesaplanamadı")
            return

        # ── 12. Emir gönder ───────────────────────────────────────────────────
        trade = await self.order_manager.open_position(
            symbol, signal, qty, sl_price, tp_price, leverage=leverage
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
            Decimal(str(ind.close)), sl_price, tp_price, leverage
        )

        # ── 14. Dashboard güncelle ────────────────────────────────────────────
        open_count = len(await get_open_trades())
        update_bot_state(open_positions=open_count, balance=float(total_balance))
        await broadcast_ws({"type": "position_update"})

    # ── Pozisyon Kapanma Callback ─────────────────────────────────────────────

    async def _on_position_close(self, trade, pnl: Decimal) -> None:
        """OrderManager pozisyon kapandığında bunu çağırır."""
        pnl_pct = trade.pnl_pct or Decimal("0")
        await self.notifier.order_closed(
            trade.symbol, trade.side, pnl, pnl_pct, trade.close_reason or "?"
        )
        # Eğer işlem STOP LOSS ile kapandıysa, bu coine 30 dakika soğuma süresi ver!
        if trade.close_reason == "SL":
            import time
            self._sl_cooldown[trade.symbol] = time.time() + 1800  # 30 dakika (1800 saniye)
            logger.info(
                f"[{trade.symbol}] STOP LOSS ile kapandı → 30 dakika soğuma süresi başlatıldı (Tekrar zarar yazması engellendi)."
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
        WebSocket kline eventine ek olarak her 5 dakikada saat bazlı
        sinyal taraması yapılır (fallback + güvenlik).
        """
        pos_sync_interval = 20
        status_update_interval = 30
        pos_sync_counter = 0
        status_counter = 0
        time_sync_counter = 0
        last_scan_minute = -1  # Son tarama yapılan dakika

        while self._running:
            await asyncio.sleep(3)
            pos_sync_counter += 3
            status_counter += 3
            time_sync_counter += 3

            # ── 1. Canlı Trailing Stop & Vur-Kaç Kontrolü (Her 3 saniyede bir) ──
            if config.TRAILING_STOP:
                try:
                    await self.order_manager.update_trailing_stops()
                except Exception as exc:
                    logger.debug(f"Trailing stop döngü hatası: {exc}")

            # ── Saat bazlı 5 dakikalık sembol taraması (ANA MEKANİZMA) ─────────
            import time as _time
            current_minute = int(_time.time() // 60) % 5  # 0,1,2,3,4
            if current_minute == 0 and last_scan_minute != int(_time.time() // 300):
                last_scan_minute = int(_time.time() // 300)
                logger.info(f"⏱ 5dk mum kapandı — {len(self._symbols)} sembol taranıyor")
                async def _scan_all():
                    for sym in self._symbols:
                        if not self._running:
                            break
                        try:
                            await self._on_kline_close(sym, {})
                        except Exception as exc:
                            logger.error(f"[{sym}] Tarama hatası: {exc}")
                        await asyncio.sleep(0.2)  # Rate limit koruması
                asyncio.create_task(_scan_all())

            # ── Periyodik Binance Saat Senkronizasyonu (180sn) ────────────────
            if time_sync_counter >= 180:
                time_sync_counter = 0
                try:
                    await self.client._sync_server_time()
                except Exception:
                    pass

            # ── Pozisyon senkronizasyonu (20sn) ──────────────────────────────
            if pos_sync_counter >= pos_sync_interval:
                pos_sync_counter = 0
                try:
                    await self.order_manager.sync_positions()
                except Exception as exc:
                    logger.error(f"Periyodik senkronizasyon hatası: {exc}")

            # ── Dashboard durum güncellemesi ──────────────────────────────────
            if status_counter >= status_update_interval:
                status_counter = 0
                try:
                    wallet_balance = await self.client.get_wallet_balance_usdt()
                    total_balance = await self.client.get_total_balance_usdt()
                    unrealized = total_balance - wallet_balance
                    open_count = len(await get_open_trades())
                    daily_pnl = float(await self._get_daily_pnl())
                    update_bot_state(
                        running=True,
                        balance=float(total_balance),
                        wallet_balance=float(wallet_balance),
                        unrealized_pnl=float(unrealized),
                        open_positions=open_count,
                        daily_pnl=daily_pnl,
                    )
                    await broadcast_ws({
                        "type": "status",
                        "data": {
                            "balance_usdt": float(total_balance),
                            "wallet_balance": float(wallet_balance),
                            "unrealized_pnl": float(unrealized),
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
    """Bot'u başlat ve çalıştır (Hata durumunda otomatik kendini toparlar)."""
    global _bot, _fatal_error
    retry_delay = 20
    while True:
        _bot = TradingBot()
        try:
            await _bot.start()
            while _bot._running:
                await asyncio.sleep(1)
            break
        except KeyboardInterrupt:
            logger.info("Kullanıcı tarafından durduruldu")
            break
        except Exception as exc:
            err_msg = f"{type(exc).__name__}: {exc}"
            logger.error(f"Bot başlangıç/çalışma hatası: {err_msg}. {retry_delay} saniye sonra yeniden denenecek...")
            update_bot_state(error=err_msg, running=False)
            if _bot and _bot.notifier:
                try:
                    await _bot.notifier.error_alert(f"Bot uyarısı (Yeniden başlatılıyor):\n{err_msg}")
                except Exception:
                    pass
            try:
                await _bot.stop()
            except Exception:
                pass
            await asyncio.sleep(retry_delay)
        finally:
            if _bot and not _bot._running:
                try:
                    await _bot.stop()
                except Exception:
                    pass
