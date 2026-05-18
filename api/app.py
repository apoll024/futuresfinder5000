"""
FuturesFinder5000 — Interactive web dashboard
Controls: trade on/off toggle, ingest on/off toggle, symbol management, capital allocation
Data: live signals, projected/actual trades, daily P&L, pending signals
"""
import os, sys, json, re, requests
from datetime import datetime, date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    import docker as docker_sdk
    _DOCKER_AVAILABLE = True
except ImportError:
    _DOCKER_AVAILABLE = False

from flask import Flask, render_template, jsonify, request, Response, stream_with_context
from sqlalchemy import func, text
from db.models import init_db, Session, Bar, Signal, Trade, HealthMetric, get_setting, set_setting

app      = Flask(__name__)
ET       = ZoneInfo("America/New_York")
LLM_API_URL  = os.getenv("LLM_API_URL",  "http://ollama:11434/v1/chat/completions")
LLM_MODEL    = os.getenv("LLM_MODEL",    "llama3.1:8b")


# ── helpers ──────────────────────────────────────────────────────────────────

def ollama_healthy() -> bool:
    try:
        r = requests.get(LLM_API_URL.replace("/v1/chat/completions", "/api/tags"), timeout=3)
        return r.ok
    except Exception:
        return False


def get_docker_client():
    if not _DOCKER_AVAILABLE:
        return None
    try:
        return docker_sdk.from_env()
    except Exception:
        return None


def get_ingest_status() -> dict:
    client = get_docker_client()
    if not client:
        return {"running": None, "status": "docker unavailable"}
    try:
        c = client.containers.get("ff_ingest")
        return {"running": c.status == "running", "status": c.status}
    except Exception as e:
        name = type(e).__name__
        if "NotFound" in name:
            return {"running": False, "status": "not found"}
        return {"running": None, "status": str(e)}


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


def pending_signals(limit: int = 25) -> list[dict]:
    """Signals model wants to act on but hasn't yet — the 'planned trades' queue."""
    session = Session()
    rows = (session.query(Signal)
            .filter(Signal.acted_on == False, Signal.action.in_(["buy", "sell"]))
            .order_by(Signal.ts.desc())
            .limit(limit)
            .all())
    result = [{
        "id":          s.id,
        "symbol":      s.symbol,
        "action":      s.action,
        "confidence":  round(s.confidence * 100),
        "reasoning":   (s.reasoning or "")[:200],
        "ts":          s.ts.strftime("%Y-%m-%d %H:%M:%S"),
        "entry_price": round(s.entry_price, 2) if s.entry_price else None,
    } for s in rows]
    session.close()
    return result


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
    start  = datetime.combine(date.today(), datetime.min.time())
    end    = start + timedelta(days=1)
    trades = session.query(Trade).filter(Trade.ts >= start, Trade.ts < end).all()
    sigs   = session.query(Signal).filter(Signal.ts >= start, Signal.ts < end).count()
    session.close()
    bought = sum(t.price * t.qty for t in trades if t.side == "buy"  and t.status == "filled")
    sold   = sum(t.price * t.qty for t in trades if t.side == "sell" and t.status == "filled")
    pnl    = round(sold - bought, 2)
    return {
        "pnl":           pnl,
        "pnl_class":     "positive" if pnl >= 0 else "negative",
        "trades_today":  len(trades),
        "buys":          sum(1 for t in trades if t.side == "buy"),
        "sells":         sum(1 for t in trades if t.side == "sell"),
        "signals_today": sigs,
    }


def db_stats() -> dict:
    """Bar/signal/trade counts, latest ingestion time, PostgreSQL DB size."""
    session = Session()
    try:
        bar_count    = session.query(Bar).count()
        signal_count = session.query(Signal).count()
        trade_count  = session.query(Trade).count()
        latest_bar   = session.query(Bar).order_by(Bar.ts.desc()).first()
        oldest_bar   = session.query(Bar).order_by(Bar.ts.asc()).first()
        sym_counts   = dict(session.query(Bar.symbol, func.count(Bar.id))
                            .group_by(Bar.symbol).all())
        try:
            row = session.execute(
                text("SELECT pg_size_pretty(pg_database_size(current_database()))")
            ).fetchone()
            db_size = row[0] if row else "N/A"
        except Exception:
            db_size = "N/A"
        return {
            "bar_count":    bar_count,
            "signal_count": signal_count,
            "trade_count":  trade_count,
            "latest_bar":   latest_bar.ts.strftime("%Y-%m-%d %H:%M") if latest_bar else "—",
            "oldest_bar":   oldest_bar.ts.strftime("%Y-%m-%d") if oldest_bar else "—",
            "db_size":      db_size,
            "sym_counts":   sym_counts,
        }
    finally:
        session.close()


def resource_stats() -> list[dict]:
    """Latest host-level CPU/mem/disk metrics from watchdog (3 cards only)."""
    session = Session()
    try:
        subq = (session.query(
                    HealthMetric.metric_type,
                    HealthMetric.name,
                    func.max(HealthMetric.ts).label("max_ts"))
                .filter(HealthMetric.metric_type == "host")
                .group_by(HealthMetric.metric_type, HealthMetric.name)
                .subquery())
        rows = (session.query(HealthMetric)
                .join(subq, (HealthMetric.metric_type == subq.c.metric_type) &
                             (HealthMetric.name == subq.c.name) &
                             (HealthMetric.ts == subq.c.max_ts))
                .all())
        label_map = {"cpu": "CPU", "mem": "RAM", "disk": "Disk"}
        return [{
            "name":   label_map.get(r.name, r.name.upper()),
            "value":  round(r.value, 1),
            "status": r.status,
            "ts":     r.ts.strftime("%H:%M"),
        } for r in rows]
    except Exception:
        return []
    finally:
        session.close()


# ── page ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    symbols    = get_symbols()
    trade_mode = get_trade_mode()
    ingest_st  = get_ingest_status()
    now        = datetime.now(ET)
    return render_template("index.html",
        signals          = latest_signals(symbols),
        trades           = recent_trades(),
        pending          = pending_signals(),
        stats            = daily_stats(),
        trade_mode       = trade_mode,
        trading_on       = trade_mode in ("paper", "live"),
        ingest_running   = ingest_st.get("running"),
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
    current  = get_trade_mode()
    new_mode = "paper" if current == "suggest" else "suggest"
    set_setting("trade_mode", new_mode)
    return jsonify({"trade_mode": new_mode, "trading_on": new_mode == "paper"})


@app.route("/api/ingest/status")
def api_ingest_status():
    return jsonify(get_ingest_status())


@app.route("/api/ingest/toggle", methods=["POST"])
def api_toggle_ingest():
    client = get_docker_client()
    if not client:
        return jsonify({"error": "Docker socket unavailable — check volume mount"}), 503
    try:
        c = client.containers.get("ff_ingest")
        if c.status == "running":
            c.stop(timeout=15)
            return jsonify({"running": False, "action": "stopped"})
        else:
            c.start()
            return jsonify({"running": True, "action": "started"})
    except Exception as e:
        if "NotFound" in type(e).__name__:
            return jsonify({"error": "ff_ingest container not found"}), 404
        return jsonify({"error": str(e)}), 500


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


@app.route("/api/signals/pending")
def api_pending_signals():
    return jsonify(pending_signals())


@app.route("/api/trades")
def api_trades():
    return jsonify(recent_trades())


@app.route("/api/stats")
def api_stats():
    ingest_st = get_ingest_status()
    return jsonify({**daily_stats(),
                    "trade_mode":       get_trade_mode(),
                    "approved_capital": get_approved_capital(),
                    "max_position":     get_max_position(),
                    "daily_loss_limit": get_daily_loss_limit(),
                    "ollama_ok":        ollama_healthy(),
                    "ingest_running":   ingest_st.get("running")})


@app.route("/api/ingest/backfill", methods=["POST"])
def api_backfill():
    """Kick off a yfinance historical backfill for one or all symbols in a background thread."""
    import threading
    data   = request.json or {}
    symbol = data.get("symbol", "").strip().upper()
    days   = int(data.get("days", 30))

    def _run(syms, d):
        try:
            from ingest.historical import ingest_historical
            import os
            os.environ["HISTORY_DAYS"] = str(d)
            if syms:
                os.environ["SYMBOLS"] = ",".join(syms)
            ingest_historical()
        except Exception as e:
            print(f"[backfill] error: {e}")

    syms = [symbol] if symbol else []
    threading.Thread(target=_run, args=(syms, days), daemon=True).start()
    label = symbol if symbol else "all symbols"
    return jsonify({"status": "started", "symbol": label, "days": days})


@app.route("/api/db/stats")
def api_db_stats():
    return jsonify(db_stats())


@app.route("/api/health/latest")
def api_health_latest():
    return jsonify(resource_stats())


def build_chat_context() -> str:
    """Snapshot of current market state injected into every LLM chat turn."""
    sigs      = latest_signals(get_symbols())
    stats     = daily_stats()
    ingest_st = get_ingest_status()
    now       = datetime.now(ET)
    mkt_open  = "09:45" <= now.strftime("%H:%M") <= "15:45"

    sig_lines = "\n".join(
        f"  {s['symbol']}: {s['action'].upper()} conf={s['confidence']}%"
        f" last=${s['last_price'] or '—'}"
        f" bars={s['bar_count']:,}"
        f" | {(s['reasoning'] or '')[:120]}"
        for s in sigs
    )

    return (
        f"Time: {now.strftime('%Y-%m-%d %H:%M ET')} | "
        f"Market: {'OPEN' if mkt_open else 'CLOSED'}\n"
        f"Trade mode: {get_trade_mode()} | "
        f"Approved capital: ${get_approved_capital():,.0f} | "
        f"Max position: ${get_max_position():,.0f} | "
        f"Daily loss limit: ${get_daily_loss_limit():,.0f}\n"
        f"Ingest: {'running' if ingest_st.get('running') else 'stopped'} | "
        f"Ollama: {'ok' if ollama_healthy() else 'OFFLINE'}\n"
        f"Today — P&L: ${stats['pnl']} | "
        f"Trades: {stats['trades_today']} (B={stats['buys']} S={stats['sells']}) | "
        f"Signals: {stats['signals_today']}\n"
        f"Watchlist signals:\n{sig_lines if sig_lines else '  No signals yet'}"
    )


# ── API: chat ─────────────────────────────────────────────────────────────────

CHAT_SYSTEM_PROMPT = """You are FuturesFinder5000's AI trading assistant, running live on an Oracle Cloud VM.
You have deep expertise in leveraged ETF day trading (TQQQ/SQQQ, UPRO/SPXU, SOXL/SOXS pairs).

You can:
- Explain your current signals and the reasoning behind them
- Analyze market conditions and indicator values
- Suggest strategy adjustments (the user applies them via the dashboard)
- Answer questions about specific positions, risk levels, or trade history
- Discuss what you're watching and why

Key rules you always follow:
- Never hold leveraged ETFs overnight (daily decay)
- VWAP on the underlying (QQQ, SPY, SOXX) is the primary entry signal
- Volume ratio > 1.5 confirms moves; < 0.8 = avoid new entries
- Opening range first 15 min = no new entries
- < 30 min left in session = exits only

At the start of each message you are given a live snapshot of current market state — use it to give specific, grounded answers. Be direct and concise. Cite actual values."""


def _fetch_url_text(url: str, max_chars: int = 6000) -> str:
    """Fetch a URL and return plain text (strip HTML tags). Used to inject web content into chat."""
    import re as _re
    try:
        resp = requests.get(url, timeout=15,
                            headers={"User-Agent": "Mozilla/5.0 (compatible; FF5000/1.0)"})
        resp.raise_for_status()
        html = resp.text
        # Strip scripts/styles then all tags
        html = _re.sub(r'<(script|style)[^>]*>.*?</\1>', '', html, flags=_re.I | _re.S)
        text = _re.sub(r'<[^>]+>', ' ', html)
        text = _re.sub(r'[ \t]{2,}', ' ', text)
        text = _re.sub(r'\n{3,}', '\n\n', text).strip()
        return text[:max_chars]
    except Exception as e:
        return f"[Could not fetch {url}: {e}]"


_URL_RE = re.compile(r'https?://\S+')


@app.route("/api/chat", methods=["POST"])
def api_chat():
    data     = request.json or {}
    user_msg = data.get("message", "").strip()
    history  = data.get("history", [])   # [{role, content}, ...]

    if not user_msg:
        return jsonify({"error": "No message"}), 400

    # If the user pasted a URL, fetch it server-side and inject the content
    url_match = _URL_RE.search(user_msg)
    fetched_content = ""
    if url_match:
        fetched_url = url_match.group(0)
        fetched_content = _fetch_url_text(fetched_url)

    ctx = build_chat_context()
    system_content = CHAT_SYSTEM_PROMPT + f"\n\n--- Live market snapshot ---\n{ctx}\n---"
    if fetched_content:
        system_content += f"\n\n--- Web page content fetched from {fetched_url} ---\n{fetched_content}\n---"

    messages = (
        [{"role": "system", "content": system_content}]
        + history[-20:]   # keep last 20 turns to bound context length
        + [{"role": "user", "content": user_msg}]
    )

    def generate():
        try:
            r = requests.post(
                LLM_API_URL,
                headers={"Content-Type": "application/json"},
                json={"model": LLM_MODEL, "messages": messages,
                      "stream": True, "temperature": 0.25, "max_tokens": 1024},
                stream=True, timeout=(15, 300),
            )
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                decoded = line.decode("utf-8")
                if not decoded.startswith("data: "):
                    continue
                chunk = decoded[6:]
                if chunk.strip() == "[DONE]":
                    yield "data: [DONE]\n\n"
                    return
                try:
                    obj   = json.loads(chunk)
                    delta = obj["choices"][0]["delta"].get("content", "")
                    if delta:
                        yield f"data: {json.dumps({'content': delta})}\n\n"
                except Exception:
                    pass
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
            yield "data: [DONE]\n\n"

    return Response(stream_with_context(generate()),
                    content_type="text/event-stream",
                    headers={"X-Accel-Buffering": "no",
                             "Cache-Control": "no-cache"})


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5001, debug=False)