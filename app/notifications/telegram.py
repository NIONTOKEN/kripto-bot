"""
Telegram bildirimleri.
python-telegram-bot v21 async API kullanılır.
Bot token veya chat id eksikse mesajlar sessizce görmezden gelinir.
"""
from __future__ import annotations


from decimal import Decimal
from typing import Optional

from app.config import config
from app.utils.logger import get_logger

logger = get_logger(__name__)

try:
    from telegram import Bot
    from telegram.error import TelegramError
    _TELEGRAM_AVAILABLE = True
except ImportError:
    _TELEGRAM_AVAILABLE = False
    logger.warning("python-telegram-bot yüklü değil, bildirimler devre dışı")


class TelegramNotifier:
    def __init__(self) -> None:
        self._bot: Optional[object] = None
        self._enabled = (
            _TELEGRAM_AVAILABLE
            and bool(config.TELEGRAM_BOT_TOKEN)
            and bool(config.TELEGRAM_CHAT_ID)
        )
        if self._enabled:
            self._bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
            logger.info("Telegram bildirimleri etkin")
        else:
            logger.info("Telegram bildirimleri devre dışı (token/chat_id eksik)")

    async def send(self, text: str) -> None:
        """Telegram mesajı gönder. Hata olursa loglara yaz ama crash etme."""
        if not self._enabled or self._bot is None:
            return
        try:
            await self._bot.send_message(
                chat_id=config.TELEGRAM_CHAT_ID,
                text=text,
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.warning(f"Telegram gönderim hatası: {exc}")

    async def signal(
        self,
        symbol: str,
        direction: str,
        score: float,
        confidence: float,
        regime: str,
    ) -> None:
        emoji = "🟢" if direction == "LONG" else "🔴"
        await self.send(
            f"{emoji} <b>SİNYAL</b> — {symbol}\n"
            f"Yön: <b>{direction}</b>\n"
            f"Skor: <b>{score:.1f}/100</b>\n"
            f"ML Güven: {confidence:.2%}\n"
            f"Rejim: {regime}"
        )

    async def order_opened(
        self,
        symbol: str,
        direction: str,
        quantity: Decimal,
        entry: Decimal,
        sl: Decimal,
        tp: Decimal,
        leverage: int,
    ) -> None:
        emoji = "📈" if direction == "LONG" else "📉"
        await self.send(
            f"{emoji} <b>POZİSYON AÇILDI</b> — {symbol}\n"
            f"Yön: <b>{direction}</b> × {leverage}x\n"
            f"Miktar: {quantity}\n"
            f"Giriş: {entry:.4f}\n"
            f"SL: {sl:.4f}\n"
            f"TP: {tp:.4f}"
        )

    async def order_closed(
        self,
        symbol: str,
        direction: str,
        pnl: Decimal,
        pnl_pct: Decimal,
        reason: str,
    ) -> None:
        if pnl >= 0:
            emoji = "✅"
        else:
            emoji = "❌"
        await self.send(
            f"{emoji} <b>POZİSYON KAPATILDI</b> — {symbol}\n"
            f"Yön: {direction} | Kapanış: {reason}\n"
            f"PnL: <b>{pnl:+.4f} USDT ({pnl_pct:+.2f}%)</b>"
        )

    async def error_alert(self, message: str) -> None:
        await self.send(f"🚨 <b>BOT HATASI</b>\n{message}")

    async def daily_summary(
        self,
        date_str: str,
        total_pnl: Decimal,
        num_trades: int,
        num_wins: int,
        balance: Decimal,
    ) -> None:
        win_rate = (num_wins / num_trades * 100) if num_trades > 0 else 0
        emoji = "📊"
        await self.send(
            f"{emoji} <b>GÜNLÜK ÖZET</b> — {date_str}\n"
            f"Toplam PnL: <b>{total_pnl:+.2f} USDT</b>\n"
            f"İşlem: {num_trades} | Kazanan: {num_wins} ({win_rate:.0f}%)\n"
            f"Bakiye: {balance:.2f} USDT"
        )

    async def bot_started(self, balance: Decimal, symbols: int) -> None:
        env = "TESTNET" if config.BINANCE_TESTNET else "🔴 MAINNET"
        await self.send(
            f"🤖 <b>BOT BAŞLATILDI</b>\n"
            f"Ortam: {env}\n"
            f"Bakiye: {balance:.2f} USDT\n"
            f"Takip edilen sembol: {symbols}"
        )
