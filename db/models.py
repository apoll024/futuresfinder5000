"""
Database schema and helpers.
Tables: bars (OHLCV), signals (LLM analysis + outcomes), trades, settings, health_metrics
"""
import os
from datetime import datetime
from sqlalchemy import (
    create_engine, Column, String, Float, Integer,
    DateTime, Boolean, Text, UniqueConstraint, Index
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
    """LLM-generated trade signal + post-trade outcome for XGBoost training."""
    __tablename__ = "signals"
    id          = Column(Integer, primary_key=True, autoincrement=True)
    symbol      = Column(String(10), nullable=False)
    ts          = Column(DateTime, default=datetime.utcnow)
    action      = Column(String(10))
    confidence  = Column(Float)
    reasoning   = Column(Text)
    indicators  = Column(Text)
    acted_on    = Column(Boolean, default=False)
    entry_price = Column(Float)
    exit_price  = Column(Float)
    exit_time   = Column(DateTime)
    outcome_pct = Column(Float)
    was_correct = Column(Boolean)
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
    """Runtime configuration — persisted so dashboard controls take effect immediately."""
    __tablename__ = "settings"
    key   = Column(String(50), primary_key=True)
    value = Column(Text)


class HealthMetric(Base):
    """Container and system resource metrics written by watchdog every 5 minutes."""
    __tablename__ = "health_metrics"
    id          = Column(Integer, primary_key=True, autoincrement=True)
    ts          = Column(DateTime, default=datetime.utcnow, index=True)
    metric_type = Column(String(20))   # cpu | mem | disk | container
    name        = Column(String(50))   # container name or volume path
    value       = Column(Float)        # percentage or 0/1 for container liveness
    status      = Column(String(10))   # ok | warn | critical
    note        = Column(Text)


class KnowledgeItem(Base):
    """Persistent knowledge base — articles, notes, EOD summaries injected into LLM context."""
    __tablename__ = "knowledge"
    id         = Column(Integer, primary_key=True, autoincrement=True)
    title      = Column(String(300), nullable=False)
    source_url = Column(String(600))
    content    = Column(Text, nullable=False)
    tags       = Column(String(300))   # comma-separated symbols / topics
    ts         = Column(DateTime, default=datetime.utcnow, index=True)


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
