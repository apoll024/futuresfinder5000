"""
Database schema and helpers using SQLAlchemy.
Tables: bars (OHLCV), signals (LLM analysis), trades (executed orders)
"""
import os
from datetime import datetime
from sqlalchemy import (
    create_engine, Column, String, Float, Integer,
    DateTime, Boolean, Text, UniqueConstraint
)
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DB_URL = os.getenv("DB_URL", "postgresql://ff_user:changeme@db:5432/futuresfinder")
engine = create_engine(DB_URL)
Session = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


class Bar(Base):
    """OHLCV price bar — 1-minute resolution."""
    __tablename__ = "bars"
    id        = Column(Integer, primary_key=True, autoincrement=True)
    symbol    = Column(String(10), nullable=False)
    ts        = Column(DateTime, nullable=False)
    open      = Column(Float)
    high      = Column(Float)
    low       = Column(Float)
    close     = Column(Float)
    volume    = Column(Float)
    __table_args__ = (UniqueConstraint("symbol", "ts"),)


class Signal(Base):
    """LLM-generated trade signal."""
    __tablename__ = "signals"
    id         = Column(Integer, primary_key=True, autoincrement=True)
    symbol     = Column(String(10), nullable=False)
    ts         = Column(DateTime, default=datetime.utcnow)
    action     = Column(String(10))   # buy | sell | hold
    confidence = Column(Float)        # 0.0 - 1.0
    reasoning  = Column(Text)
    indicators = Column(Text)         # JSON snapshot of RSI/MACD/EMA at signal time
    acted_on   = Column(Boolean, default=False)


class Trade(Base):
    """Executed or suggested trade record."""
    __tablename__ = "trades"
    id           = Column(Integer, primary_key=True, autoincrement=True)
    symbol       = Column(String(10), nullable=False)
    ts           = Column(DateTime, default=datetime.utcnow)
    side         = Column(String(5))    # buy | sell
    qty          = Column(Float)
    price        = Column(Float)
    mode         = Column(String(10))   # suggest | paper | live
    alpaca_id    = Column(String(50))   # order ID if submitted
    status       = Column(String(20))   # suggested | submitted | filled | rejected
    signal_id    = Column(Integer)


def init_db():
    Base.metadata.create_all(engine)
