"""
Database schema and helpers using SQLAlchemy.
Tables: bars (OHLCV), signals (LLM analysis + outcomes), trades (executed orders), settings (runtime config)
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
    """LLM-generated trade signal + post-trade outcome for model training."""
    __tablename__ = "signals"
    id          = Column(Integer, primary_key=True, autoincrement=True)
    symbol      = Column(String(10), nullable=False)
    ts          = Column(DateTime, default=datetime.utcnow)
    action      = Column(String(10))   # buy | sell | hold
    confidence  = Column(Float)
    reasoning   = Column(Text)
    indicators  = Column(Text)         # JSON snapshot of all indicators at signal time
    acted_on    = Column(Boolean, default=False)
    # Entry / exit for outcome calculation
    entry_price = Column(Float)        # close price at signal generation time
    exit_price  = Column(Float)        # EOD close price used for settlement
    exit_time   = Column(DateTime)     # timestamp of settlement
    outcome_pct = Column(Float)        # % gain (+) or loss (-) from signal perspective
    was_correct = Column(Boolean)      # True if outcome_pct > 0
    settled     = Column(Boolean, default=False)


class Trade(Base):
    """Executed or suggested trade record."""
    __tablename__ = "trades"
    id        = Column(Integer, primary_key=True, autoincrement=True)
    symbol    = Column(String(10), nullable=False)
    ts        = Column(DateTime, default=datetime.utcnow)
    side      = Column(String(5))
    qty       = Column(Float)
    price     = Column(Float)
    mode      = Column(String(10))
    alpaca_id = Column(String(50))
    status    = Column(String(20))
    signal_id = Column(Integer)


class Setting(Base):
    """Runtime configuration — persisted in DB so UI controls take effect immediately."""
    __tablename__ = "settings"
    key   = Column(String(50), primary_key=True)
    value = Column(Text)


def get_setting(key: str, default: str = "") -> str:
    session = Session()
    row = session.query(Setting).filter(Setting.key == key).first()
    session.close()
    return row.value if row else default


def set_setting(key: str, value: str):
    session = Session()
    row = session.query(Setting).filter(Setting.key == key).first()
    if row:
        row.value = value
    else:
        session.add(Setting(key=key, value=value))
    session.commit()
    session.close()


def init_db():
    Base.metadata.create_all(engine)
    session = Session()
    defaults = {
        "trade_mode": os.getenv("TRADE_MODE", "suggest"),
        "symbols":    os.getenv("SYMBOLS", "TQQQ,SQQQ,UPRO,SPXU,SOXL,SOXS,QQQ,SPY,SOXX"),
    }
    for k, v in defaults.items():
        if not session.query(Setting).filter(Setting.key == k).first():
            session.add(Setting(key=k, value=v))
    session.commit()
    session.close()
