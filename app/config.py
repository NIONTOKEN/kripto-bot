"""
Configuration — .env dosyasından tüm ayarlar burada yüklenir.
Eksik zorunlu değer varsa başlangıçta hata verir.
"""
import os
from decimal import Decimal
from typing import List

from dotenv import load_dotenv

load_dotenv()


def _required(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise ValueError(
            f"Zorunlu environment değişkeni '{key}' ayarlanmamış. "
            f".env.example dosyasından .env oluşturun."
        )
    return val


def _opt(key: str, default: str) -> str:
    return os.environ.get(key, default).strip()


class Config:
    # ─── Binance ─────────────────────────────────────────────────────────────
    BINANCE_API_KEY: str = _required("BINANCE_API_KEY")
    BINANCE_SECRET_KEY: str = _required("BINANCE_SECRET_KEY")
    BINANCE_TESTNET: bool = _opt("BINANCE_TESTNET", "false").lower() == "true"

    REST_BASE: str = (
        "https://testnet.binancefuture.com"
        if _opt("BINANCE_TESTNET", "false").lower() == "true"
        else "https://fapi.binance.com"
    )
    WS_BASE: str = (
        "wss://stream.binancefuture.com"
        if _opt("BINANCE_TESTNET", "false").lower() == "true"
        else "wss://fstream.binance.com"
    )

    # ─── Trading ─────────────────────────────────────────────────────────────
    LEVERAGE: int = int(_opt("LEVERAGE", "15"))
    MAX_OPEN_POSITIONS: int = int(_opt("MAX_OPEN_POSITIONS", "4"))
    RISK_PER_TRADE_PCT: Decimal = Decimal(_opt("RISK_PER_TRADE_PCT", "28.0"))
    MIN_SCORE_TO_OPEN: float = float(_opt("MIN_SCORE_TO_OPEN", "42.0"))
    MIN_BALANCE_USDT: Decimal = Decimal(_opt("MIN_BALANCE_USDT", "1.0"))
    MAX_LEVERAGE: int = int(_opt("MAX_LEVERAGE", "20"))
    ONLY_LONG: bool = _opt("ONLY_LONG", "false").lower() == "true"
    SCALP_MODE: bool = _opt("SCALP_MODE", "true").lower() == "true"
    SCALP_LEVERAGE: int = int(_opt("SCALP_LEVERAGE", "15"))
    SWING_LEVERAGE: int = int(_opt("SWING_LEVERAGE", "8"))

    # ─── Risk ────────────────────────────────────────────────────────────────
    DAILY_LOSS_LIMIT_PCT: Decimal = Decimal(_opt("DAILY_LOSS_LIMIT_PCT", "50.0"))
    SL_ATR_MULTIPLIER: Decimal = Decimal(_opt("SL_ATR_MULTIPLIER", "1.5"))
    TP_ATR_MULTIPLIER: Decimal = Decimal(_opt("TP_ATR_MULTIPLIER", "3.0"))
    TRAILING_STOP: bool = _opt("TRAILING_STOP", "true").lower() == "true"

    # ─── Semboller ───────────────────────────────────────────────────────────
    TOP_SYMBOLS_COUNT: int = int(_opt("TOP_SYMBOLS_COUNT", "20"))
    BLACKLIST: List[str] = [
        s.strip().upper()
        for s in _opt("BLACKLIST", "").split(",")
        if s.strip()
    ]

    # ─── Zaman Dilimleri ─────────────────────────────────────────────────────
    PRIMARY_TF: str = _opt("PRIMARY_TF", "5m")
    HIGHER_TF: str = _opt("HIGHER_TF", "1h")
    KLINE_LIMIT: int = int(_opt("KLINE_LIMIT", "300"))

    # ─── ML ──────────────────────────────────────────────────────────────────
    ML_RETRAIN_HOURS: int = int(_opt("ML_RETRAIN_HOURS", "4"))
    ML_HISTORY_CANDLES: int = int(_opt("ML_HISTORY_CANDLES", "500"))
    ML_MIN_SAMPLES: int = int(_opt("ML_MIN_SAMPLES", "100"))

    # ─── Telegram ────────────────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = _opt("TELEGRAM_BOT_TOKEN", "8830972901:AAF3vEj2HszCoZ84K6nQC97oGTzcemvg20Y")
    TELEGRAM_CHAT_ID: str = _opt("TELEGRAM_CHAT_ID", "1505452121")

    # ─── Dashboard ───────────────────────────────────────────────────────────
    DASHBOARD_HOST: str = _opt("DASHBOARD_HOST", "0.0.0.0")
    DASHBOARD_PORT: int = int(os.environ.get("PORT", _opt("DASHBOARD_PORT", "8000")))

    # ─── Veritabanı ──────────────────────────────────────────────────────────
    DATABASE_URL: str = _opt(
        "DATABASE_URL", "sqlite+aiosqlite:///./trading_bot.db"
    )

    # ─── Zamanlama ───────────────────────────────────────────────────────────
    STALE_DATA_SECONDS: int = int(_opt("STALE_DATA_SECONDS", "120"))
    POSITION_CHECK_INTERVAL: int = int(_opt("POSITION_CHECK_INTERVAL", "5"))
    LISTEN_KEY_REFRESH_MINUTES: int = int(_opt("LISTEN_KEY_REFRESH_MINUTES", "45"))


config = Config()
