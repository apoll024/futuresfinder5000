"""
Database schema and helpers.
Tables: bars (OHLCV), signals (LLM analysis + outcomes), trades, settings, health_metrics,
        llm_sessions (full AI call log), chat_messages (advisor conversation history),
        ai_messages (persistent agent-to-user inbox)
"""
import os
from datetime import datetime
from sqlalchemy import (
    create_engine, Column, String, Float, Integer,
    DateTime, Boolean, Text, UniqueConstraint, Index, text
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
    id              = Column(Integer, primary_key=True, autoincrement=True)
    symbol          = Column(String(10), nullable=False)
    ts              = Column(DateTime, default=datetime.utcnow)
    action          = Column(String(10))
    confidence      = Column(Float)
    reasoning       = Column(Text)
    indicators      = Column(Text)
    acted_on        = Column(Boolean, default=False)
    entry_price     = Column(Float)
    exit_price      = Column(Float)
    exit_time       = Column(DateTime)
    outcome_pct     = Column(Float)
    was_correct     = Column(Boolean)
    settled         = Column(Boolean, default=False)
    # Options recommendation (nullable — only set when LLM recommends an option trade)
    option_strategy = Column(Text)           # e.g. "long_call", "long_put"
    option_contract = Column(String(30))     # OCC symbol e.g. QQQ250523C00450000
    option_strike   = Column(Float)
    option_expiry   = Column(String(12))     # YYYY-MM-DD


class Trade(Base):
    """Executed or suggested trade record."""
    __tablename__ = "trades"
    id              = Column(Integer, primary_key=True, autoincrement=True)
    symbol          = Column(String(10), nullable=False)
    ts              = Column(DateTime, default=datetime.utcnow)
    side            = Column(String(5))
    qty             = Column(Float)
    price           = Column(Float)
    mode            = Column(String(10))
    alpaca_id       = Column(String(50))
    status          = Column(String(20))
    signal_id       = Column(Integer)
    # Option position metadata (nullable — only set for option trades)
    option_contract = Column(String(30))     # OCC symbol
    option_expiry   = Column(String(12))     # YYYY-MM-DD


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


class CryptoDerivativeMetric(Base):
    """Perpetual futures derivatives metrics used for crypto market visibility."""
    __tablename__ = "crypto_derivative_metrics"
    id                    = Column(Integer, primary_key=True, autoincrement=True)
    symbol                = Column(String(15), nullable=False, index=True)
    exchange_symbol       = Column(String(20), nullable=False)
    source                = Column(String(40), default="binance_futures")
    ts                    = Column(DateTime, default=datetime.utcnow, index=True)
    funding_rate          = Column(Float)  # percent, e.g. 0.0100 means 0.01%
    mark_price            = Column(Float)
    index_price           = Column(Float)
    next_funding_time     = Column(DateTime)
    open_interest         = Column(Float)
    open_interest_value   = Column(Float)  # notional USD/USDT value when available
    open_interest_trend   = Column(String(12))
    long_short_ratio      = Column(Float)
    long_account_pct      = Column(Float)
    short_account_pct     = Column(Float)
    taker_buy_sell_ratio  = Column(Float)
    taker_buy_volume      = Column(Float)
    taker_sell_volume     = Column(Float)
    raw                   = Column(Text)


class KnowledgeItem(Base):
    """Persistent knowledge base — articles, notes, EOD summaries injected into LLM context."""
    __tablename__ = "knowledge"
    id         = Column(Integer, primary_key=True, autoincrement=True)
    title      = Column(String(300), nullable=False)
    source_url = Column(String(600))
    content    = Column(Text, nullable=False)
    tags       = Column(String(300))   # comma-separated symbols / topics
    ts         = Column(DateTime, default=datetime.utcnow, index=True)
    sentiment  = Column(Float)         # -1.0 (bearish) to +1.0 (bullish), scored by digest agent


class AIMessage(Base):
    """Persistent AI-to-user inbox — messages written by autonomous agents between sessions."""
    __tablename__ = "ai_messages"
    id       = Column(Integer, primary_key=True, autoincrement=True)
    ts       = Column(DateTime, default=datetime.utcnow, index=True)
    category = Column(String(20))    # trade | resource | update | alert | warning | info
    title    = Column(String(200), nullable=False)
    body     = Column(Text)
    read     = Column(Boolean, default=False)
    source   = Column(String(50))    # watchdog | settler | crypto | digest | backtest | analyzer


class LLMSession(Base):
    """Full audit log of every AI call — prompt, raw response, latency, outcome."""
    __tablename__ = "llm_sessions"
    id           = Column(Integer, primary_key=True, autoincrement=True)
    ts           = Column(DateTime, default=datetime.utcnow, index=True)
    service      = Column(String(20))   # analyze | crypto | advisor | review | chat
    model        = Column(String(60))
    symbol       = Column(String(15))   # nullable — not set for chat calls
    prompt       = Column(Text)
    response     = Column(Text)
    action       = Column(String(10))   # buy | sell | hold | None
    confidence   = Column(Float)
    latency_ms   = Column(Integer)


class ChatMessage(Base):
    """Persistent advisor / chat conversation history."""
    __tablename__ = "chat_messages"
    id           = Column(Integer, primary_key=True, autoincrement=True)
    ts           = Column(DateTime, default=datetime.utcnow, index=True)
    session_key  = Column(String(80), index=True)   # username or browser session id
    service      = Column(String(20))               # advisor | review | chat
    role         = Column(String(10))               # user | assistant | system
    content      = Column(Text)


Index("ix_crypto_derivative_symbol_ts", CryptoDerivativeMetric.symbol, CryptoDerivativeMetric.ts)


def write_inbox(category: str, title: str, body: str, source: str = "system") -> None:
    """Fire-and-forget: write a message to the AI inbox. Silently ignores errors."""
    try:
        db = Session()
        db.add(AIMessage(
            category=category[:20],
            title=title[:200],
            body=body,
            source=source[:50],
        ))
        db.commit()
        db.close()
    except Exception:
        pass


def log_llm_session(service: str, model: str, prompt: str, response: str,
                    symbol: str = None, action: str = None,
                    confidence: float = None, latency_ms: int = None):
    """Fire-and-forget: write one LLM call record. Silently ignores errors."""
    try:
        session = Session()
        session.add(LLMSession(
            service=service, model=model, symbol=symbol,
            prompt=prompt, response=response,
            action=action, confidence=confidence, latency_ms=latency_ms,
        ))
        session.commit()
        session.close()
    except Exception:
        pass


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

    # Migrate: add new nullable columns to existing tables (safe to run repeatedly)
    _migrations = [
        ("signals", "option_strategy", "TEXT"),
        ("signals", "option_contract", "VARCHAR(30)"),
        ("signals", "option_strike",   "FLOAT"),
        ("signals", "option_expiry",   "VARCHAR(12)"),
        ("trades",  "option_contract", "VARCHAR(30)"),
        ("trades",  "option_expiry",   "VARCHAR(12)"),
        ("llm_sessions", "symbol",     "VARCHAR(15)"),
        ("llm_sessions", "action",     "VARCHAR(10)"),
        ("llm_sessions", "confidence", "FLOAT"),
        ("llm_sessions", "latency_ms", "INTEGER"),
        ("knowledge",    "sentiment",  "FLOAT"),
    ]
    try:
        with engine.begin() as conn:
            for tbl, col, coltype in _migrations:
                conn.execute(text(
                    f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS {col} {coltype}"
                ))
    except Exception as e:
        print(f"  [init_db] column migration note: {e}")

    session = Session()
    defaults = {
        "trade_mode":              os.getenv("TRADE_MODE", "suggest"),
        "symbols":                 os.getenv("SYMBOLS", "TQQQ,SQQQ,UPRO,SPXU,SOXL,SOXS,QQQ,SPY,SOXX"),
        "options_enabled":         "false",
        "max_option_premium_usd":  "200",
        "stocks_enabled":          "true",
        "crypto_enabled":          "true",
        "crypto_symbols":          os.getenv("CRYPTO_SYMBOLS", "BTC/USD,ETH/USD,SOL/USD"),
        "wallet_enabled":          "true",
    }
    for k, v in defaults.items():
        if not session.query(Setting).filter(Setting.key == k).first():
            session.add(Setting(key=k, value=v))
    session.commit()
    session.close()
