"""
FuturesFinder5000 — Web dashboard
Shows live signals, trade suggestions, daily P&L, and system status.
Auto-refreshes every 60 seconds. No JS framework needed.
"""
import os, sys, json, requests
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).parent.parent))

from flask import Flask, render_template, jsonify
from db.models import init_db, Session, Bar, Signal, Trade

app = Flask(__name__)
ET  = ZoneInfo("America/New_York")

LLM_API_URL  = os.getenv("LLM_API_URL", "http://ollama:11434/v1/chat/completions")
TRADE_MODE   = os.getenv("TRADE_MODE", "suggest")
SYMBOLS      = [s.strip() for s in os.getenv("SYMBOLS", "TQQQ,SQQQ,UPRO,SPXU,SOXL,SOXS").split(",")]


def ollama_healthy() -> bool:
    try:
        r = requests.get(LLM_API_URL.replace("/v1/chat/completions", "/api/tags"), timeout=3)
        return r.ok
    except Exception:
        return False


def latest_signals() -> list[dict]:
    session = Session()
    results = []
    for sym in SYMBOLS:
        sig = (session.query(Signal)
               .filter(Signal.symbol == sym)
               .order_by(Signal.ts.desc())
               .first())
        if sig:
            results.append({
                "symbol":     sym,
                "action":     sig.action,
                "confidence": round(sig.confidence * 100),
                "reasoning":  sig.reasoning,
                "ts":         sig.ts.strftime("%H:%M:%S"),
                "indicators": json.loads(sig.indicators or "{}"),
                "acted_on":   sig.acted_on,
            })
    session.close()
    return results


def recent_trades(limit: int = 30) -> list[dict]:
    session = Session()
    rows = (session.query(Trade)
            .order_by(Trade.ts.desc())
            .limit(limit)
            .all())
    trades = [{
        "symbol": t.symbol,
        "side":   t.side,
        "qty":    t.qty,
        "price":  t.price,
        "mode":   t.mode,
        "status": t.status,
        "ts":     t.ts.strftime("%H:%M:%S"),
        "value":  round(t.price * t.qty, 2),
    } for t in rows]
    session.close()
    return trades


def daily_stats() -> dict:
    session = Session()
    today = date.today().isoformat()
    trades = (session.query(Trade)
              .filter(Trade.ts.cast(str).startswith(today))
              .all())
    session.close()

    total_signals = (Session().query(Signal)
                     .filter(Signal.ts.cast(str).startswith(today))
                     .count())

    bought = sum(t.price * t.qty for t in trades if t.side == "buy"  and t.status == "filled")
    sold   = sum(t.price * t.qty for t in trades if t.side == "sell" and t.status == "filled")
    pnl    = round(sold - bought, 2)

    buys  = sum(1 for t in trades if t.side == "buy")
    sells = sum(1 for t in trades if t.side == "sell")

    return {
        "pnl":           pnl,
        "pnl_class":     "positive" if pnl >= 0 else "negative",
        "trades_today":  len(trades),
        "buys":          buys,
        "sells":         sells,
        "signals_today": total_signals,
    }


def bar_count() -> dict:
    session = Session()
    counts = {}
    for sym in SYMBOLS:
        counts[sym] = session.query(Bar).filter(Bar.symbol == sym).count()
    session.close()
    return counts


@app.route("/")
def index():
    now = datetime.now(ET)
    return render_template("index.html",
        signals      = latest_signals(),
        trades       = recent_trades(),
        stats        = daily_stats(),
        bar_counts   = bar_count(),
        trade_mode   = TRADE_MODE,
        ollama_ok    = ollama_healthy(),
        now          = now.strftime("%Y-%m-%d %H:%M:%S ET"),
        market_open  = "09:45" <= now.strftime("%H:%M") <= "15:45",
    )


@app.route("/api/signals")
def api_signals():
    return jsonify(latest_signals())


@app.route("/api/trades")
def api_trades():
    return jsonify(recent_trades())


@app.route("/api/stats")
def api_stats():
    return jsonify(daily_stats())


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5001, debug=False)
