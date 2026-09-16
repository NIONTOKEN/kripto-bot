"""
SQLAlchemy async database models and session management.
Tables: trades, signals, daily_stats
"""
from __future__ import annotations


from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    Numeric,
    String,
    Text,
    select,
    func,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import config
from app.utils.logger import get_logger

logger = get_logger(__name__)

engine = create_async_engine(
    config.DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False}
    if "sqlite" in config.DATABASE_URL
    else {},
)

AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, expire_on_commit=False
)


class Base(DeclarativeBase):
    pass


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False, index=True)
    side = Column(String(5), nullable=False)          # LONG / SHORT
    entry_price = Column(Numeric(20, 8), nullable=False)
    sl_price = Column(Numeric(20, 8), nullable=True)
    tp_price = Column(Numeric(20, 8), nullable=True)
    quantity = Column(Numeric(20, 8), nullable=False)
    leverage = Column(Integer, nullable=False)
    pnl = Column(Numeric(20, 8), nullable=True)
    pnl_pct = Column(Numeric(10, 4), nullable=True)   # realized ROE %
    status = Column(String(10), nullable=False, default="OPEN")  # OPEN/CLOSED/ERROR
    close_reason = Column(String(30), nullable=True)   # SL/TP/MANUAL/LIQUIDATION
    entry_order_id = Column(String(30), nullable=True)
    sl_order_id = Column(String(30), nullable=True)
    tp_order_id = Column(String(30), nullable=True)
    signal_score = Column(Float, nullable=True)
    ml_confidence = Column(Float, nullable=True)
    regime = Column(String(15), nullable=True)
    opened_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    closed_at = Column(DateTime, nullable=True)


class Signal(Base):
    __tablename__ = "signals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)
    direction = Column(String(7), nullable=False)     # LONG/SHORT/NEUTRAL
    score = Column(Float, nullable=False)
    ml_confidence = Column(Float, nullable=True)
    regime = Column(String(15), nullable=True)
    rsi = Column(Float, nullable=True)
    macd_hist = Column(Float, nullable=True)
    adx = Column(Float, nullable=True)
    atr = Column(Float, nullable=True)


class DailyStat(Base):
    __tablename__ = "daily_stats"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, unique=True, nullable=False)
    total_pnl = Column(Numeric(20, 8), default=Decimal("0"))
    num_trades = Column(Integer, default=0)
    num_wins = Column(Integer, default=0)
    starting_balance = Column(Numeric(20, 8), nullable=True)
    ending_balance = Column(Numeric(20, 8), nullable=True)
    notes = Column(Text, nullable=True)


async def init_db() -> None:
    """Tabloları oluştur (yoksa)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Veritabanı hazır")


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


# ── Yardımcı fonksiyonlar ─────────────────────────────────────────────────────

async def save_trade(trade: Trade) -> Trade:
    async with AsyncSessionLocal() as session:
        session.add(trade)
        await session.commit()
        await session.refresh(trade)
    return trade


async def update_trade(trade_id: int, **kwargs) -> None:
    async with AsyncSessionLocal() as session:
        trade = await session.get(Trade, trade_id)
        if trade is None:
            logger.warning(f"Trade {trade_id} bulunamadı")
            return
        for key, value in kwargs.items():
            setattr(trade, key, value)
        await session.commit()


async def get_open_trades() -> List[Trade]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Trade).where(Trade.status == "OPEN")
        )
        return list(result.scalars().all())


async def get_open_trade_for_symbol(symbol: str) -> Optional[Trade]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Trade).where(Trade.symbol == symbol, Trade.status == "OPEN")
        )
        return result.scalars().first()


async def get_todays_closed_pnl() -> Decimal:
    """Bugün kapatılan işlemlerin toplam PnL'i."""
    today = date.today()
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(func.sum(Trade.pnl)).where(
                Trade.status == "CLOSED",
                func.date(Trade.closed_at) == today,
            )
        )
        val = result.scalar()
        return Decimal(str(val)) if val is not None else Decimal("0")


async def save_signal(signal: Signal) -> None:
    async with AsyncSessionLocal() as session:
        session.add(signal)
        await session.commit()


async def get_recent_trades(limit: int = 50) -> List[Trade]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Trade).order_by(Trade.opened_at.desc()).limit(limit)
        )
        return list(result.scalars().all())


async def get_or_create_daily_stat(balance: Optional[Decimal] = None) -> DailyStat:
    today = date.today()
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(DailyStat).where(DailyStat.date == today)
        )
        stat = result.scalars().first()
        if stat is None:
            stat = DailyStat(date=today, starting_balance=balance)
            session.add(stat)
            await session.commit()
            await session.refresh(stat)
        return stat


async def update_daily_stat(pnl_delta: Decimal, win: bool, ending_balance: Decimal) -> None:
    today = date.today()
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(DailyStat).where(DailyStat.date == today)
        )
        stat = result.scalars().first()
        if stat is None:
            stat = DailyStat(date=today)
            session.add(stat)
        stat.total_pnl = (stat.total_pnl or Decimal("0")) + pnl_delta
        stat.num_trades = (stat.num_trades or 0) + 1
        if win:
            stat.num_wins = (stat.num_wins or 0) + 1
        stat.ending_balance = ending_balance
        await session.commit()
