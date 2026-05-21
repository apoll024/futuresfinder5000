"""
FuturesFinder5000 — Interactive web dashboard
Controls: trade on/off toggle, ingest on/off toggle, symbol management, capital allocation
Data: live signals, projected/actual trades, daily P&L, pending signals
"""
import os, sys, json, re, requests, hashlib, functools, threading
from datetime import datetime, date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    import docker as docker_sdk
    _DOCKER_AVAILABLE = True
except ImportError:
    _DOCKER_AVAILABLE = False

from flask import Flask, render_template, jsonify, request, Response, stream_with_context, session, redirect, url_for
from sqlalchemy import func, text
from db.models import init_db, Session, Bar, Signal, Trade, HealthMetric, KnowledgeItem, get_setting, set_setting

app           = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "ff5k-change-me-in-prod-32bytes!!")
ET            = ZoneInfo("America/New_York")
LLM_API_URL   = os.getenv("LLM_API_URL",  "http://ollama:11434/v1/chat/completions")
LLM_MODEL     = os.getenv("LLM_MODEL",    "llama3.2:3b")

# Auth credentials — stored as SHA-256 hashes; set via env or use defaults
_AUTH_USER     = os.getenv("DASHBOARD_USER", "admin")
_AUTH_HASH     = os.getenv("DASHBOARD_PASS_HASH",
                            hashlib.sha256(b"ultracrosswalknormalhijinx").hexdigest())


def _check_password(pw: str) -> bool:
    return hashlib.sha256(pw.encode()).hexdigest() == _AUTH_HASH


def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("logged_in"):
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if username == _AUTH_USER and _check_password(password):
            session["logged_in"] = True
            session.permanent = True
            next_url = request.args.get("next") or "/"
            return redirect(next_url)
        error = "Invalid username or password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


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


def get_options_enabled() -> bool:
    return get_setting("options_enabled", "false") == "true"


def get_max_option_premium() -> float:
    return float(get_setting("max_option_premium_usd", "200"))


def get_stocks_enabled() -> bool:
    return get_setting("stocks_enabled", "true") == "true"


def get_crypto_enabled() -> bool:
    return get_setting("crypto_enabled", "true") == "true"


def get_crypto_symbols() -> list[str]:
    raw = get_setting("crypto_symbols", "BTC/USD,ETH/USD,SOL/USD")
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def get_all_positions()-> list[dict]:
    """Open positions: Alpaca in paper/live mode; derived from today's trade log in suggest mode."""
    mode = get_trade_mode()
    if mode != "suggest":
        try:
            from trade.executor import get_client
            client = get_client()
            raw    = client.get_all_positions()
            db     = Session()
            result = []
            for pos in raw:
                sym       = pos.symbol
                bar       = db.query(Bar).filter(Bar.symbol == sym).order_by(Bar.ts.desc()).first()
                cur_price = float(bar.close) if bar else float(pos.current_price or 0)
                avg_entry = float(pos.avg_entry_price or 0)
                qty       = float(pos.qty or 0)
                unreal    = round((cur_price - avg_entry) * qty, 2)
                pct       = round((cur_price - avg_entry) / avg_entry * 100, 2) if avg_entry else 0.0
                result.append({
                    "symbol":         sym,
                    "qty":            round(qty, 4),
                    "avg_entry":      round(avg_entry, 4),
                    "current_price":  round(cur_price, 4),
                    "unrealized_pnl": unreal,
                    "pct_change":     pct,
                    "market_value":   round(qty * cur_price, 2),
                })
            db.close()
            return result
        except Exception as e:
            print(f"[positions] Alpaca error: {e}")
            return []

    # Suggest mode — derive net positions from today's trade log
    db = Session()
    try:
        today_start = datetime.combine(date.today(), datetime.min.time())
        trades      = db.query(Trade).filter(Trade.ts >= today_start).order_by(Trade.ts.asc()).all()
    finally:
        db.close()

    net_qty:    dict[str, float] = {}
    total_cost: dict[str, float] = {}
    for t in trades:
        sym   = t.symbol
        qty   = float(t.qty   or 0)
        price = float(t.price or 0)
        net_qty.setdefault(sym, 0.0)
        total_cost.setdefault(sym, 0.0)
        if t.side == "buy":
            total_cost[sym] += price * qty
            net_qty[sym]    += qty
        elif t.side == "sell" and net_qty[sym] > 0:
            avg             = total_cost[sym] / net_qty[sym]
            removed         = min(qty, net_qty[sym])
            total_cost[sym] -= avg * removed
            net_qty[sym]    = max(0.0, net_qty[sym] - qty)

    result = []
    db2    = Session()
    for sym, qty in net_qty.items():
        if qty <= 0:
            continue
        avg_entry = total_cost[sym] / qty if qty > 0 else 0.0
        bar       = db2.query(Bar).filter(Bar.symbol == sym).order_by(Bar.ts.desc()).first()
        cur_price = float(bar.close) if bar else avg_entry
        unreal    = round((cur_price - avg_entry) * qty, 2)
        pct       = round((cur_price - avg_entry) / avg_entry * 100, 2) if avg_entry else 0.0
        result.append({
            "symbol":         sym,
            "qty":            round(qty, 4),
            "avg_entry":      round(avg_entry, 4),
            "current_price":  round(cur_price, 4),
            "unrealized_pnl": unreal,
            "pct_change":     pct,
            "market_value":   round(qty * cur_price, 2),
        })
    db2.close()
    return result


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
@login_required
def index():
    symbols    = get_symbols()
    trade_mode = get_trade_mode()
    ingest_st  = get_ingest_status()
    now        = datetime.now(ET)
    return render_template("index.html",
        signals             = latest_signals(symbols),
        trades              = recent_trades(),
        pending             = pending_signals(),
        stats               = daily_stats(),
        trade_mode          = trade_mode,
        trading_on          = trade_mode in ("paper", "live"),
        ingest_running      = ingest_st.get("running"),
        approved_capital    = get_approved_capital(),
        max_position        = get_max_position(),
        daily_loss_limit    = get_daily_loss_limit(),
        options_enabled     = get_options_enabled(),
        max_option_premium  = get_max_option_premium(),
        stocks_enabled      = get_stocks_enabled(),
        crypto_enabled      = get_crypto_enabled(),
        crypto_symbols      = get_crypto_symbols(),
        ollama_ok           = ollama_healthy(),
        now                 = now.strftime("%Y-%m-%d %H:%M:%S ET"),
        market_open         = "09:45" <= now.strftime("%H:%M") <= "15:45",
    )


# ── API: controls ─────────────────────────────────────────────────────────────

@app.route("/api/trade/toggle", methods=["POST"])
@login_required
def toggle_trading():
    """Switch between suggest (off) and paper (on). Live requires manual env change."""
    current  = get_trade_mode()
    new_mode = "paper" if current == "suggest" else "suggest"
    set_setting("trade_mode", new_mode)
    return jsonify({"trade_mode": new_mode, "trading_on": new_mode == "paper"})


@app.route("/api/ingest/status")
@login_required
def api_ingest_status():
    return jsonify(get_ingest_status())


@app.route("/api/ingest/toggle", methods=["POST"])
@login_required
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
@login_required
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
@login_required
def remove_symbol():
    sym     = request.json.get("symbol", "").strip().upper()
    symbols = [s for s in get_symbols() if s != sym]
    set_setting("symbols", ",".join(symbols))
    return jsonify({"symbols": symbols})


@app.route("/api/stocks/toggle", methods=["POST"])
@login_required
def toggle_stocks():
    new = "false" if get_stocks_enabled() else "true"
    set_setting("stocks_enabled", new)
    return jsonify({"stocks_enabled": new == "true"})


@app.route("/api/crypto/toggle", methods=["POST"])
@login_required
def toggle_crypto():
    new = "false" if get_crypto_enabled() else "true"
    set_setting("crypto_enabled", new)
    return jsonify({"crypto_enabled": new == "true"})


@app.route("/api/crypto/symbols/add", methods=["POST"])
@login_required
def add_crypto_symbol():
    sym = (request.json or {}).get("symbol", "").strip().upper()
    if not sym:
        return jsonify({"error": "No symbol provided"}), 400
    syms = get_crypto_symbols()
    if sym not in syms:
        syms.append(sym)
        set_setting("crypto_symbols", ",".join(syms))
    return jsonify({"crypto_symbols": syms})


@app.route("/api/crypto/symbols/remove", methods=["POST"])
@login_required
def remove_crypto_symbol():
    sym  = (request.json or {}).get("symbol", "").strip().upper()
    syms = [s for s in get_crypto_symbols() if s != sym]
    set_setting("crypto_symbols", ",".join(syms))
    return jsonify({"crypto_symbols": syms})


# ── Crypto market data (fear-greed, top coins, trending) ────────────────────
_market_cache: dict = {"data": None, "ts": 0.0}
_MARKET_CACHE_TTL = 300   # 5 min

@app.route("/api/crypto/market")
@login_required
def crypto_market():
    """Aggregated live market data for the sidebar panel."""
    import time as _time
    now = _time.time()
    if _market_cache["data"] and now - _market_cache["ts"] < _MARKET_CACHE_TTL:
        return jsonify({**_market_cache["data"], "cached": True})

    CV_BASE = "https://cryptocurrency.cv"
    fear_greed = {"value": 50, "label": "Neutral", "trend": None}
    coins      = []
    trending   = []

    try:
        r = requests.get(f"{CV_BASE}/api/market/fear-greed", timeout=8)
        if r.ok:
            d   = r.json()
            cur = d.get("current", {})
            tr  = d.get("trend", {})
            fear_greed = {
                "value": cur.get("value", 50),
                "label": cur.get("valueClassification", "Neutral"),
                "trend": tr,
            }
    except Exception as e:
        print(f"[market] fear-greed error: {e}")

    try:
        r = requests.get(f"{CV_BASE}/api/market/coins?limit=15", timeout=10)
        if r.ok:
            coins = r.json().get("coins", [])[:15]
    except Exception as e:
        print(f"[market] coins error: {e}")

    try:
        r = requests.get(f"{CV_BASE}/api/trending", timeout=8)
        if r.ok:
            t = r.json().get("trending", [])
            trending = [(x.get("keyword") or x.get("topic") or str(x)) for x in t[:8]]
    except Exception as e:
        print(f"[market] trending error: {e}")

    data = {"fear_greed": fear_greed, "coins": coins, "trending": trending}
    _market_cache["data"] = data
    _market_cache["ts"]   = now
    return jsonify({**data, "cached": False})


# ── Wallet endpoints ─────────────────────────────────────────────────────────
@app.route("/api/wallet/info")
@login_required
def wallet_info():
    """Return wallet address, balances, and network."""
    try:
        from wallet.crypto_wallet import get_wallet_summary
        return jsonify(get_wallet_summary())
    except Exception as e:
        return jsonify({"configured": False, "error": str(e)})


@app.route("/api/wallet/refresh", methods=["POST"])
@login_required
def wallet_refresh():
    """Force-refresh wallet balance cache."""
    try:
        from wallet.crypto_wallet import get_wallet_summary
        return jsonify(get_wallet_summary(force=True))
    except Exception as e:
        return jsonify({"configured": False, "error": str(e)})


@app.route("/api/settings", methods=["POST"])
@login_required
def update_settings():
    data = request.json or {}
    updated = {}
    # Numeric limits
    for key in ("approved_capital_usd", "max_position_usd", "max_daily_loss_usd",
                "max_option_premium_usd"):
        if key in data:
            try:
                val = float(data[key])
                set_setting(key, str(val))
                updated[key] = val
            except ValueError:
                return jsonify({"error": f"Invalid value for {key}"}), 400
    # Boolean settings
    for key in ("options_enabled", "stocks_enabled", "crypto_enabled"):
        if key in data:
            val = "true" if str(data[key]).lower() in ("true", "1", "yes") else "false"
            set_setting(key, val)
            updated[key] = val
    return jsonify({"updated": updated})


# ── API: data ─────────────────────────────────────────────────────────────────

@app.route("/api/signals")
@login_required
def api_signals():
    return jsonify(latest_signals(get_symbols()))


@app.route("/api/signals/pending")
@login_required
def api_pending_signals():
    return jsonify(pending_signals())


@app.route("/api/trades")
@login_required
def api_trades():
    return jsonify(recent_trades())


@app.route("/api/stats")
@login_required
def api_stats():
    ingest_st = get_ingest_status()
    return jsonify({**daily_stats(),
                    "trade_mode":       get_trade_mode(),
                    "approved_capital": get_approved_capital(),
                    "max_position":     get_max_position(),
                    "daily_loss_limit": get_daily_loss_limit(),
                    "stocks_enabled":   get_stocks_enabled(),
                    "crypto_enabled":   get_crypto_enabled(),
                    "ollama_ok":        ollama_healthy(),
                    "ingest_running":   ingest_st.get("running")})


@app.route("/api/stats/reset", methods=["POST"])
@login_required
def api_stats_reset():
    """Delete records by scope to reset dashboard counters."""
    scope = (request.json or {}).get("scope", "")
    db = Session()
    try:
        today_start = datetime.combine(date.today(), datetime.min.time())
        today_end   = today_start + timedelta(days=1)
        deleted = 0
        if scope == "signals_today":
            deleted = db.query(Signal).filter(Signal.ts >= today_start, Signal.ts < today_end).delete()
        elif scope == "trades_today":
            deleted = db.query(Trade).filter(Trade.ts >= today_start, Trade.ts < today_end).delete()
        elif scope == "buys_today":
            deleted = db.query(Trade).filter(Trade.ts >= today_start, Trade.ts < today_end, Trade.side == "buy").delete()
        elif scope == "sells_today":
            deleted = db.query(Trade).filter(Trade.ts >= today_start, Trade.ts < today_end, Trade.side == "sell").delete()
        elif scope == "signals_all":
            deleted = db.query(Signal).delete()
        elif scope == "trades_all":
            deleted = db.query(Trade).delete()
        else:
            return jsonify({"error": f"Unknown scope: {scope}"}), 400
        db.commit()
        return jsonify({"status": "ok", "deleted": deleted, "scope": scope})
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


# ── API: holdings / liquidation ───────────────────────────────────────────────

@app.route("/api/positions")
@login_required
def api_positions():
    return jsonify(get_all_positions())


@app.route("/api/positions/liquidate", methods=["POST"])
@login_required
def api_liquidate_position():
    symbol = (request.json or {}).get("symbol", "").strip().upper()
    if not symbol:
        return jsonify({"error": "No symbol provided"}), 400

    mode = get_trade_mode()
    if mode == "suggest":
        positions = get_all_positions()
        pos = next((p for p in positions if p["symbol"] == symbol), None)
        if not pos:
            return jsonify({"error": f"No open position in {symbol}"}), 400
        db = Session()
        try:
            bar   = db.query(Bar).filter(Bar.symbol == symbol).order_by(Bar.ts.desc()).first()
            price = float(bar.close) if bar else pos["avg_entry"]
            db.add(Trade(symbol=symbol, side="sell", qty=pos["qty"],
                         price=price, mode=mode, status="suggested"))
            db.commit()
        finally:
            db.close()
        return jsonify({"status": "suggested", "symbol": symbol, "qty": pos["qty"]})

    try:
        from trade.executor import get_client
        get_client().close_position(symbol)
        return jsonify({"status": "closed", "symbol": symbol})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/positions/liquidate-all", methods=["POST"])
@login_required
def api_liquidate_all():
    mode      = get_trade_mode()
    positions = get_all_positions()
    if not positions:
        return jsonify({"status": "ok", "closed": 0})

    if mode == "suggest":
        db = Session()
        try:
            for pos in positions:
                bar   = db.query(Bar).filter(Bar.symbol == pos["symbol"]).order_by(Bar.ts.desc()).first()
                price = float(bar.close) if bar else pos["avg_entry"]
                db.add(Trade(symbol=pos["symbol"], side="sell", qty=pos["qty"],
                             price=price, mode=mode, status="suggested"))
            db.commit()
        finally:
            db.close()
        return jsonify({"status": "suggested", "closed": len(positions)})

    try:
        from trade.executor import close_all_positions
        close_all_positions("MANUAL")
        return jsonify({"status": "closed", "closed": len(positions)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/positions/ai-review", methods=["POST"])
@login_required
def api_positions_ai_review():
    positions = get_all_positions()
    if not positions:
        return jsonify({"error": "No open positions to review"}), 400

    sigs           = latest_signals([p["symbol"] for p in positions])
    indicators_map = {s["symbol"]: s.get("indicators", {}) for s in sigs}
    now            = datetime.now(ET)
    cutoff_dt      = now.replace(hour=15, minute=45, second=0, microsecond=0)
    minutes_left   = max(0, int((cutoff_dt - now).total_seconds() / 60))
    mkt_open       = "09:45" <= now.strftime("%H:%M") <= "15:45"

    prompt = (
        "You are FuturesFinder5000 reviewing open positions for potential liquidation.\n\n"
        f"SESSION: {now.strftime('%H:%M ET')} | Market: {'OPEN' if mkt_open else 'CLOSED'} | "
        f"{minutes_left} min to session close | Mode: {get_trade_mode()}\n\n"
        "OPEN POSITIONS:\n"
        + json.dumps(positions, indent=2)
        + "\n\nCURRENT INDICATORS PER SYMBOL:\n"
        + json.dumps(indicators_map, indent=2)
        + "\n\nEXIT RULES — recommend 'liquidate' when ANY apply:\n"
        "1. < 30 minutes left in session — mandatory EOD exit (leveraged ETF decay)\n"
        "2. RSI > 75 while long — overbought, take profit\n"
        "3. Price below VWAP on underlying — trend reversed\n"
        "4. Volume ratio < 0.7 after entry — conviction gone\n"
        "5. Unrealized loss > 2% of position value — stop loss triggered\n"
        "6. Position open > 2 hours with no meaningful gain\n\n"
        "Analyze each position against current indicators. Be specific — cite actual values "
        "(RSI, VWAP delta, volume ratio, P&L %, minutes remaining).\n\n"
        "Respond ONLY with valid JSON — no prose outside it:\n"
        "{\n"
        '  "summary": "1-2 sentence overall assessment",\n'
        '  "positions": [\n'
        '    {"symbol": "TICKER", "action": "hold|liquidate", "urgency": "low|medium|high",\n'
        '     "reasoning": "cite actual indicator values"}\n'
        "  ]\n"
        "}"
    )

    messages = [
        {"role": "system", "content": (
            "You are FuturesFinder5000 — a disciplined leveraged ETF day-trading agent. "
            "Review positions strictly and conservatively. Always cite actual numbers. "
            "When uncertain, recommend hold rather than guessing."
        )},
        {"role": "user", "content": prompt},
    ]

    def generate():
        try:
            r = requests.post(
                LLM_API_URL,
                headers={"Content-Type": "application/json"},
                json={"model": LLM_MODEL, "messages": messages,
                      "stream": True, "temperature": 0.1, "max_tokens": 1024},
                stream=True, timeout=(15, 120),
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
                    headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


# ── API: news digest ──────────────────────────────────────────────────────────

_digest_running = False


@app.route("/api/news/digest/status")
@login_required
def api_news_digest_status():
    return jsonify({
        "last_run":   get_setting("news_digest_last_run", "Never"),
        "last_count": int(get_setting("news_digest_last_count", "0") or 0),
        "running":    _digest_running,
    })


@app.route("/api/news/digest/run", methods=["POST"])
@login_required
def api_news_digest_run():
    global _digest_running
    if _digest_running:
        return jsonify({"error": "Digest already running"}), 409

    def _run():
        global _digest_running
        _digest_running = True
        try:
            from ingest.news_digest import run_digest
            run_digest()
        except Exception as e:
            print(f"[digest] manual run error: {e}")
        finally:
            _digest_running = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "started"})


# ── API: options chain ─────────────────────────────────────────────────────────

@app.route("/api/options/chain/<symbol>")
@login_required
def api_options_chain(symbol: str):
    """Return available weekly option contracts for a given underlying symbol."""
    try:
        from trade.executor import get_option_chain
        direction = request.args.get("direction", "call")
        dte_max   = int(request.args.get("dte_max", "14"))
        contracts = get_option_chain(symbol.upper(), direction, dte_max=dte_max)
        return jsonify([{
            "symbol":      str(c.symbol),
            "type":        str(c.contract_type),
            "strike":      float(c.strike_price or 0),
            "expiry":      str(c.expiration_date),
            "close_price": float(c.close_price or 0),
            "open_interest": int(c.open_interest or 0),
        } for c in contracts])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/knowledge", methods=["GET"])
@login_required
def api_kb_list():
    session = Session()
    try:
        items = session.query(KnowledgeItem).order_by(KnowledgeItem.ts.desc()).limit(100).all()
        return jsonify([{
            "id": i.id, "title": i.title, "source_url": i.source_url,
            "tags": i.tags, "ts": str(i.ts)[:16],
            "preview": (i.content[:200] + "…") if len(i.content) > 200 else i.content,
        } for i in items])
    finally:
        session.close()


@app.route("/api/knowledge", methods=["POST"])
@login_required
def api_kb_add():
    data    = request.json or {}
    mode    = data.get("mode", "text")   # "text" | "url"
    title   = data.get("title", "").strip()
    tags    = data.get("tags", "").strip().upper()
    content = data.get("content", "").strip()
    url     = data.get("url", "").strip()

    if mode == "url" and url:
        try:
            content = _fetch_url_text(url, max_chars=12000)
        except Exception as e:
            return jsonify({"error": f"Failed to fetch URL: {e}"}), 502
        if not title:
            title = url.split("/")[-1] or url
    elif mode == "html" and content:
        import re as _re
        content = _re.sub(r'<(script|style)[^>]*>.*?</\1>', '', content, flags=_re.I | _re.S)
        content = _re.sub(r'<[^>]+>', ' ', content)
        content = _re.sub(r'[ \t]{2,}', ' ', content)
        content = _re.sub(r'\n{3,}', '\n\n', content).strip()

    if not content:
        return jsonify({"error": "No content"}), 400
    if not title:
        title = content[:80]

    new_id = kb_add(title, content, source_url=url, tags=tags)
    return jsonify({"status": "saved", "id": new_id})


@app.route("/api/knowledge/<int:item_id>", methods=["DELETE"])
@login_required
def api_kb_delete(item_id):
    session = Session()
    try:
        item = session.query(KnowledgeItem).filter(KnowledgeItem.id == item_id).first()
        if not item:
            return jsonify({"error": "Not found"}), 404
        session.delete(item)
        session.commit()
        return jsonify({"status": "deleted"})
    finally:
        session.close()


@app.route("/api/ingest/backfill", methods=["POST"])
@login_required
def api_backfill():
    """Kick off a yfinance historical backfill for one or all symbols in a background thread."""
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
@login_required
def api_db_stats():
    return jsonify(db_stats())


@app.route("/api/health/latest")
@login_required
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

    positions = get_all_positions()
    pos_lines = "\n".join(
        f"  {p['symbol']}: {p['qty']}sh @ ${p['avg_entry']}"
        f" | now ${p['current_price']}"
        f" | P&L ${p['unrealized_pnl']:+.2f} ({p['pct_change']:+.1f}%)"
        f" | value ${p['market_value']:,.0f}"
        for p in positions
    )

    return (
        f"Time: {now.strftime('%Y-%m-%d %H:%M ET')} | "
        f"Market: {'OPEN' if mkt_open else 'CLOSED'}\n"
        f"Trade mode: {get_trade_mode()} | "
        f"Approved capital: ${get_approved_capital():,.0f} | "
        f"Max position: ${get_max_position():,.0f} | "
        f"Daily loss limit: ${get_daily_loss_limit():,.0f}\n"
        f"Options trading: {'ENABLED (max premium ${:.0f})'.format(get_max_option_premium()) if get_options_enabled() else 'DISABLED'} | "
        f"Stocks: {'ENABLED' if get_stocks_enabled() else 'DISABLED'} | "
        f"Crypto: {'ENABLED' if get_crypto_enabled() else 'DISABLED'}\n"
        f"News digest last run: {get_setting('news_digest_last_run', 'Never')}\n"
        f"Ingest: {'running' if ingest_st.get('running') else 'stopped'} | "
        f"Ollama: {'ok' if ollama_healthy() else 'OFFLINE'}\n"
        f"Today — P&L: ${stats['pnl']} | "
        f"Trades: {stats['trades_today']} (B={stats['buys']} S={stats['sells']}) | "
        f"Signals: {stats['signals_today']}\n"
        f"Open positions ({len(positions)}):\n{pos_lines if pos_lines else '  No open positions'}\n"
        f"Watchlist signals:\n{sig_lines if sig_lines else '  No signals yet'}"
    )


# ── API: chat ─────────────────────────────────────────────────────────────────

CHAT_SYSTEM_PROMPT = """You are FuturesFinder5000 — an autonomous leveraged ETF day-trading agent running live on a dedicated Oracle Cloud server.

YOUR MISSION:
You exist to grow the capital allocated to you through disciplined, rules-based day trading of leveraged ETFs (TQQQ/SQQQ, UPRO/SPXU, SOXL/SOXS and related pairs). You are not a generic assistant — you are a trading agent first. Every answer you give should reflect that purpose.

YOUR CAPABILITIES:
- You analyze real-time 1-minute bar data and technical indicators every minute the market is open
- You issue buy/sell/hold signals that get executed automatically against your allocated capital
- You track your own P&L, win rate, and open positions in a live PostgreSQL database
- You can autonomously add new symbols to your watchlist if you identify a strong setup
- You have a knowledge base of trading articles and strategies you draw from

HOW YOU TRADE:
- Underlying index direction (QQQ, SPY, SOXX) is your primary signal — you trade the leveraged version in the direction of the underlying trend
- VWAP is your anchor: above = bullish bias, below = bearish bias
- Volume ratio (current vs 20-period avg) confirms or invalidates moves
- MACD crossover + RSI provide timing confirmation
- You NEVER hold leveraged ETFs overnight — daily compounding decay destroys value
- You only enter high-conviction setups (confidence ≥ 0.65); when uncertain, you hold

CAPITAL DISCIPLINE:
- Capital preservation takes priority over growth — a flat day beats a losing day
- You honor hard stop rules: no new entries in last 30 min, mandatory EOD exit
- You size positions within approved capital limits set by the operator
- Your performance is measured in net daily P&L and cumulative growth over time

HOW TO RESPOND:
- Be direct and data-driven. Always cite actual values from the live snapshot when answering.
- If the market is CLOSED, say so plainly — do not speculate about absent signals.
- If a value is not in the snapshot, say so — never fabricate data.
- Do not describe your own infrastructure or internal errors — you have no visibility into those.
- When asked about performance, reference actual P&L and trade history from the snapshot.
- When asked for a recommendation, give one — you are a trading agent, not a disclaimer machine."""


def kb_search(query: str, limit: int = 4) -> list[dict]:
    """Full-text search the knowledge base using Postgres tsvector. Returns top matches."""
    session = Session()
    try:
        sql = text("""
            SELECT id, title, source_url, tags, ts,
                   LEFT(content, 1200) AS snippet
            FROM knowledge
            WHERE to_tsvector('english', content || ' ' || title)
                  @@ plainto_tsquery('english', :q)
            ORDER BY ts DESC
            LIMIT :lim
        """)
        rows = session.execute(sql, {"q": query, "lim": limit}).fetchall()
        return [{"id": r.id, "title": r.title, "source_url": r.source_url,
                 "tags": r.tags, "ts": str(r.ts)[:16], "snippet": r.snippet}
                for r in rows]
    except Exception:
        return []
    finally:
        session.close()


def kb_add(title: str, content: str, source_url: str = "", tags: str = "") -> int:
    """Save an item to the knowledge base. Returns the new row id."""
    session = Session()
    try:
        item = KnowledgeItem(title=title[:300], content=content,
                             source_url=source_url[:600] if source_url else "",
                             tags=tags[:300] if tags else "")
        session.add(item)
        session.commit()
        return item.id
    finally:
        session.close()



def _fetch_url_text(url, max_chars=6000):
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
@login_required
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

    # Inject relevant KB articles (FTS on user message)
    kb_hits = kb_search(user_msg)
    if kb_hits:
        kb_block = "\n\n--- Relevant knowledge base articles ---\n"
        for h in kb_hits:
            kb_block += f"\n[{h['ts']}] {h['title']}"
            if h.get("tags"):
                kb_block += f" (tags: {h['tags']})"
            kb_block += f"\n{h['snippet']}\n"
        kb_block += "---"
        system_content += kb_block

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