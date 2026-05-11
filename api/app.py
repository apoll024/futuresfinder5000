"""
FuturesFinder5000 — Interactive web dashboard
Controls: trade on/off toggle, symbol management, capital allocation
Data: live signals, projected/actual trades, daily P&L
"""
import os, sys, json, requests
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).parent.parent))

from flask import Flask, render_template, jsonify, request
from db.models import init_db, Session, Bar, Signal, Trade, get_setting, set_setting

app  = Flask(__name__)
ET   = ZoneInfo("America/New_York")
LLM_API_URL = os.getenv("LLM_API_URL", "http://ollama:11434/v1/chat/completions")


# ── helpers ──────────────────────────────────────────────────────────────────

def ollama_healthy() -> bool:
    try:
        r = requests.get(LLM_API_URL.replace("/v1/chat/completions", "/api/tags"), timeout=3)
        return r.ok
    except Exception:
        return False


def get_symbols() -> list[str]:
    raw = get_setting("symbols", "TQQQ,SQQQ,UPRO,SPXU,SOXL,SOXS,QQQ,SPY,SOXX")
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def get_trade_mode() -> str:
    return get_setting("trade_mode", "suggest")


def get_approved_capital() -> float:
    return float(get_setting("approved_capital_usd", "1000"))


def get_max_position() -> float:
    return float(get_setting("max_position_usd", "500"))


def get_daily_loss_limit() -> float:
    return float(get_setting("max_daily_loss_usd", "200"))


def latest_signals(symbols: list[str]) -> list[dict]:
    session = Session()
    results = []
    for sym in symbols:
        sig = (session.query(Signal)
               .filter(Signal.symbol == sym)
               .order_by(Signal.ts.desc())
               .first())
        bar = (session.query(Bar)
               .filter(Bar.symbol == sym)
               .order_by(Bar.ts.desc())
               .first())
        results.append({
            "symbol":     sym,
            "action":     sig.action     if sig else "—",
            "confidence": round(sig.confidence * 100) if sig else 0,
            "reasoning":  sig.reasoning  if sig else "Waiting for data...",
            "ts":         sig.ts.strftime("%H:%M:%S") if sig else "—",
            "acted_on":   sig.acted_on   if sig else False,
            "indicators": json.loads(sig.indicators or "{}") if sig else {},
            "last_price": round(bar.close, 2) if bar else None,
            "bar_count":  session.query(Bar).filter(Bar.symbol == sym).count(),
        })
    session.close()
    return results


def recent_trades(limit: int = 50) -> list[dict]:
    session = Session()
    rows = (session.query(Trade).order_by(Trade.ts.desc()).limit(limit).all())
    trades = [{
        "id":     t.id,
        "symbol": t.symbol,
        "side":   t.side,
        "qty":    t.qty,
        "price":  t.price,
        "mode":   t.mode,
        "status": t.status,
        "ts":     t.ts.strftime("%Y-%m-%d %H:%M:%S"),
        "value":  round((t.price or 0) * (t.qty or 0), 2),
    } for t in rows]
    session.close()
    return trades


def daily_stats() -> dict:
    session = Session()
    today  = date.today().isoformat()
    trades = session.query(Trade).filter(Trade.ts.cast(str).startswith(today)).all()
    sigs   = session.query(Signal).filter(Signal.ts.cast(str).startswith(today)).count()
    session.close()
    bought = sum(t.price * t.qty for t in trades if t.side == "buy"  and t.status == "filled")
    sold   = sum(t.price * t.qty for t in trades if t.side == "sell" and t.status == "filled")
    pnl    = round(sold - bought, 2)
    return {
        "pnl":          pnl,
        "pnl_class":    "positive" if pnl >= 0 else "negative",
        "trades_today": len(trades),
        "buys":         sum(1 for t in trades if t.side == "buy"),
        "sells":        sum(1 for t in trades if t.side == "sell"),
        "signals_today":sigs,
    }


# ── page ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    symbols    = get_symbols()
    trade_mode = get_trade_mode()
    now        = datetime.now(ET)
    return render_template("index.html",
        signals          = latest_signals(symbols),
        trades           = recent_trades(),
        stats            = daily_stats(),
        trade_mode       = trade_mode,
        trading_on       = trade_mode in ("paper", "live"),
        approved_capital = get_approved_capital(),
        max_position     = get_max_position(),
        daily_loss_limit = get_daily_loss_limit(),
        ollama_ok        = ollama_healthy(),
        now              = now.strftime("%Y-%m-%d %H:%M:%S ET"),
        market_open      = "09:45" <= now.strftime("%H:%M") <= "15:45",
    )


# ── API: controls ─────────────────────────────────────────────────────────────

@app.route("/api/trade/toggle", methods=["POST"])
def toggle_trading():
    """Switch between suggest (off) and paper (on). Live requires manual env change."""
    current = get_trade_mode()
    new_mode = "paper" if current == "suggest" else "suggest"
    set_setting("trade_mode", new_mode)
    return jsonify({"trade_mode": new_mode, "trading_on": new_mode == "paper"})


@app.route("/api/symbols/add", methods=["POST"])
def add_symbol():
    sym = request.json.get("symbol", "").strip().upper()
    if not sym:
        return jsonify({"error": "No symbol provided"}), 400
    symbols = get_symbols()
    if sym not in symbols:
        symbols.append(sym)
        set_setting("symbols", ",".join(symbols))
    return jsonify({"symbols": symbols})


@app.route("/api/symbols/remove", methods=["POST"])
def remove_symbol():
    sym     = request.json.get("symbol", "").strip().upper()
    symbols = [s for s in get_symbols() if s != sym]
    set_setting("symbols", ",".join(symbols))
    return jsonify({"symbols": symbols})


@app.route("/api/settings", methods=["POST"])
def update_settings():
    data = request.json or {}
    updated = {}
    for key in ("approved_capital_usd", "max_position_usd", "max_daily_loss_usd"):
        if key in data:
            try:
                val = float(data[key])
                set_setting(key, str(val))
                updated[key] = val
            except ValueError:
                return jsonify({"error": f"Invalid value for {key}"}), 400
    return jsonify({"updated": updated})


# ── API: data ─────────────────────────────────────────────────────────────────

@app.route("/api/signals")
def api_signals():
    return jsonify(latest_signals(get_symbols()))


@app.route("/api/trades")
def api_trades():
    return jsonify(recent_trades())


@app.route("/api/stats")
def api_stats():
    return jsonify({**daily_stats(),
                    "trade_mode":       get_trade_mode(),
                    "approved_capital": get_approved_capital(),
                    "max_position":     get_max_position(),
                    "daily_loss_limit": get_daily_loss_limit(),
                    "ollama_ok":        ollama_healthy()})


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5001, debug=False)
