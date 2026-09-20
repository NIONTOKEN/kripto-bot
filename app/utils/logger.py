"""
Structured logging — ISO timestamp + level + module + message.
Örnek çıktı:
  2026-09-10 12:00:00 INFO  [signals  ] SIGNAL BTCUSDT LONG score=82 confidence=0.81
"""
import logging
import sys


_FMT = "%(asctime)s %(levelname)-5s [%(name)-10s] %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"


def _configure_root() -> None:
    root = logging.getLogger()
    if root.handlers:
        return  # zaten yapılandırılmış

    import io
    # Windows cp1254 unicode encode hatasını engelle
    stream = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter(_FMT, _DATE_FMT))
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    # Gürültülü kütüphaneleri sustur
    for noisy in ("websockets", "asyncio", "urllib3", "aiohttp", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


_configure_root()


def get_logger(name: str) -> logging.Logger:
    """Modül ismine göre logger döner. Sadece son kısmı alır."""
    short = name.split(".")[-1]
    return logging.getLogger(short)
