"""
AI Context Builder — ai_context.py
===================================
Fetches relevant DB history and formats it into a context block that is
prepended to EVERY LLM prompt before the model acts. This ensures the AI
always knows what it has already done before issuing a new signal.

Sections injected into each prompt:
  1. STANDING INSTRUCTIONS  — rules that never change (identity, risk limits)
  2. DB CONTEXT SNAPSHOT    — pulled fresh from Postgres before each call:
       • Recent signals for this symbol (last 5)
       • Today's trades for this symbol
       • Last 3 LLM session reasoning entries for this symbol
       • Daily P&L and daily loss halt status
       • Signal outcome accuracy (last 10 acted-on signals with outcomes)
       • Available USDC capital (Coinbase balance, crypto only)
       • Relevant knowledge base items (tagged with symbol or "crypto")
"""
import os, sys, json
from datetime import date, datetime, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from db.models import Session, Signal, Trade, LLMSession, KnowledgeItem, write_inbox

# ── Agent registry (injected into AI context so it knows its own capabilities) ─

AGENT_REFERENCE = """
=== FF5000 AGENT REGISTRY ===
You have 7 specialized agents running alongside you. Reference this when asked about capabilities.

AGENT 1 — DATA INTEGRITY MONITOR (watchdog, every 5 min)
  Detects price bar gaps, missing data, schema issues. Writes alert to inbox when problems found.

AGENT 2 — NEWS SENTIMENT ANALYZER (digest service, 5-min crypto / 7am+noon stocks)
  Scores each news article -1.0 (bearish) to +1.0 (bullish) using keyword analysis.
  Sentiment stored on every KnowledgeItem. Aggregated in /api/analytics.

AGENT 3 — BACKTESTER (on-demand via /api/backtest/run)
  Replays settled signals, computes win rate, avg gain/loss, Sharpe ratio, per-symbol breakdown.
  Writes result summary to inbox after each run.

AGENT 4 — TRADE EXECUTION MONITOR (settler service, every 15 min)
  Checks for acted_on signals with no matching Trade record. Inbox alert if gaps found.

AGENT 5 — EOD CLEANUP (settler service, 16:15 ET weekdays)
  Settles outcomes, computes daily P&L + win rate, writes EOD summary to KB and inbox.

AGENT 6 — CRYPTO DATA MONITOR (crypto service, continuous 24/7)
  Monitors USDC balance and win rate. Inbox alert when USDC < $50 or win rate < 40%.

AGENT 7 — PERFORMANCE ANALYTICS (api service, on-demand)
  Aggregates daily P&L, win rate by symbol, signal accuracy trends. See /api/analytics.
=== END AGENT REGISTRY ===
"""

# ── Inbox throttle: avoid spamming the same warning repeatedly ─────────────────
_inbox_last_written: dict = {}   # title → datetime

def _inbox_throttled(category: str, title: str, body: str, source: str,
                     hours: float = 2.0) -> None:
    """Write to inbox at most once per `hours` for a given title."""
    now = datetime.utcnow()
    last = _inbox_last_written.get(title)
    if last and (now - last).total_seconds() < hours * 3600:
        return
    _inbox_last_written[title] = now
    write_inbox(category, title, body, source)


# ── Standing system instructions ─────────────────────────────────────────────

SYSTEM_INSTRUCTIONS = """
=== FUTURESFINDER5000 — STANDING AI INSTRUCTIONS ===

You are FuturesFinder5000's autonomous trading AI. You manage real money.
Before issuing ANY signal you MUST consult the DB Context Snapshot below.

MANDATORY PRE-ACTION CHECKLIST (verify against DB Context):
  1. RECENT SIGNALS    — Have you already signaled buy/sell for this symbol in
                         the last 5 minutes? If yes, default to HOLD unless
                         conditions have materially changed.
  2. OPEN POSITION     — Check today's trades. Are you already LONG this symbol?
                         If yes, 'buy' would pyramid — use 'sell' or 'hold' only.
                         If flat, 'sell' is INVALID — use 'buy' or 'hold' only.
  3. DAILY P&L         — If daily P&L is negative and approaching the loss halt
                         threshold, be MORE conservative. Preserve capital.
  4. PRIOR REASONING   — Review your last 3 reasoning entries for this symbol.
                         Are you repeating a signal that previously failed?
                         Do not chase the same wrong call twice.
  5. OUTCOME ACCURACY  — Review your recent win/loss rate. If win rate < 40%,
                         raise confidence threshold — only act on strong setups.
  6. AVAILABLE CAPITAL — Never exceed available USDC balance when sizing buys.
  7. KNOWLEDGE BASE    — Any relevant market notes or EOD summaries? Apply them.

RISK RULES (non-negotiable):
  • Never issue 'buy' when already long — no pyramiding
  • Never issue 'sell' when flat — you cannot sell what you do not own
  • Confidence < 0.65 → HOLD, no exceptions
  • Daily loss halt: if P&L ≤ −$200, signal HOLD on everything
=== END STANDING INSTRUCTIONS ===
""".strip()


# ── Available capital fetcher (Coinbase) ──────────────────────────────────────

def fetch_available_usdc() -> float | None:
    """
    Query Coinbase for available USDC (and USD) balance via the JWT-based client.
    Returns combined USD+USDC float or None if unavailable / not configured.
    """
    try:
        from trade.coinbase_client import is_configured, get_crypto_balance
        if not is_configured():
            return None
        usdc = get_crypto_balance("USDC")
        usd  = get_crypto_balance("USD")
        return round(usdc + usd, 2)
    except Exception:
        return None


# ── DB context fetcher ────────────────────────────────────────────────────────

def build_db_context(symbol: str, service: str = "analyze") -> str:
    """
    Query Postgres for recent activity on `symbol` and return a formatted
    context block to inject at the top of every LLM prompt.

    Parameters
    ----------
    symbol  : trading symbol, e.g. "TQQQ" or "BTC/USD"
    service : "analyze" for stocks, "crypto" for crypto
    """
    lines = ["\n=== DB CONTEXT SNAPSHOT (read this before acting) ==="]
    db = Session()
    today_str = date.today().isoformat()
    cutoff_5m = datetime.utcnow() - timedelta(minutes=5)
    cutoff_1h = datetime.utcnow() - timedelta(hours=1)

    # ── Service partition: crypto sees only crypto symbols, stocks sees only stocks ──
    is_crypto = service == "crypto"
    def _sym_filter(q, model):
        """Restrict query to the correct asset class."""
        if is_crypto:
            return q.filter(model.symbol.like("%/%"))
        else:
            return q.filter(~model.symbol.like("%/%"))

    try:
        # 1. Recent signals for this symbol (last 5, newest first)
        recent_signals = (
            db.query(Signal)
            .filter(Signal.symbol == symbol)
            .order_by(Signal.ts.desc())
            .limit(5)
            .all()
        )
        if recent_signals:
            lines.append("\nRECENT SIGNALS (this symbol, newest first):")
            for s in recent_signals:
                age = ""
                if s.ts:
                    diff = datetime.utcnow() - s.ts
                    mins = int(diff.total_seconds() / 60)
                    age = f" ({mins}m ago)"
                acted = " [ACTED ON]" if s.acted_on else ""
                outcome = ""
                if s.outcome_pct is not None:
                    outcome = f" → outcome: {s.outcome_pct:+.2f}%"
                lines.append(
                    f"  {s.ts.strftime('%H:%M') if s.ts else '?'}{age}  "
                    f"{s.action.upper()} conf={s.confidence:.2f}{acted}{outcome}"
                    f"\n    reason: {s.reasoning or 'n/a'}"
                )
        else:
            lines.append("\nRECENT SIGNALS: none recorded for this symbol yet.")

        # Warn if a signal was issued within the last 5 minutes
        very_recent = [s for s in recent_signals if s.ts and s.ts > cutoff_5m]
        if very_recent:
            last = very_recent[0]
            lines.append(
                f"\n⚠ CAUTION: You already signaled {last.action.upper()} "
                f"{int((datetime.utcnow()-last.ts).total_seconds()/60)}m ago. "
                f"Default to HOLD unless conditions have materially changed."
            )

        # 2. Today's trades for this symbol
        today_trades = (
            db.query(Trade)
            .filter(
                Trade.symbol == symbol,
                Trade.ts >= datetime.strptime(today_str, "%Y-%m-%d"),
            )
            .order_by(Trade.ts.desc())
            .all()
        )
        if today_trades:
            lines.append(f"\nTODAY'S TRADES ({today_str}, this symbol):")
            for t in today_trades:
                lines.append(
                    f"  {t.ts.strftime('%H:%M') if t.ts else '?'}  "
                    f"{t.side.upper()} {t.qty} @ ${t.price:.2f}  "
                    f"status={t.status}  mode={t.mode}"
                )
            bought_qty = sum(t.qty for t in today_trades if t.side == "buy" and t.status in ("filled", "suggested"))
            sold_qty   = sum(t.qty for t in today_trades if t.side == "sell" and t.status in ("filled", "suggested"))
            net_qty    = bought_qty - sold_qty
            if net_qty > 0:
                lines.append(f"  → NET POSITION: LONG {net_qty:.2f} shares/units")
            else:
                lines.append(f"  → NET POSITION: FLAT")
        else:
            lines.append(f"\nTODAY'S TRADES: none yet for {symbol}.")

        # 3. Daily P&L (same asset class only)
        all_today_trades_q = (
            db.query(Trade)
            .filter(
                Trade.ts >= datetime.strptime(today_str, "%Y-%m-%d"),
                Trade.status == "filled",
            )
        )
        all_today_trades = _sym_filter(all_today_trades_q, Trade).all()
        bought_usd = sum(t.price * (t.qty or 0) for t in all_today_trades if t.side == "buy")
        sold_usd   = sum(t.price * (t.qty or 0) for t in all_today_trades if t.side == "sell")
        daily_pnl  = sold_usd - bought_usd
        halt_flag  = "⚠ DAILY LOSS HALT ACTIVE — signal HOLD only" if daily_pnl <= -200 else ""
        lines.append(f"\nDAILY P&L (all symbols, filled trades): ${daily_pnl:+.2f}  {halt_flag}")

        # 4. Last 3 LLM reasoning entries for this symbol
        prior_llm = (
            db.query(LLMSession)
            .filter(LLMSession.symbol == symbol, LLMSession.ts >= cutoff_1h)
            .order_by(LLMSession.ts.desc())
            .limit(3)
            .all()
        )
        if prior_llm:
            lines.append(f"\nPRIOR AI REASONING (last hour, this symbol):")
            for entry in prior_llm:
                mins = int((datetime.utcnow() - entry.ts).total_seconds() / 60) if entry.ts else "?"
                lines.append(
                    f"  {mins}m ago  action={entry.action or '?'}  "
                    f"conf={entry.confidence or '?'}\n"
                    f"    response: {(entry.response or '')[:200]}"
                )

        # 5. Signal outcome accuracy (last 10 acted-on signals with settled outcomes)
        try:
            settled_signals = (
                db.query(Signal)
                .filter(
                    Signal.symbol == symbol,
                    Signal.acted_on == True,
                    Signal.outcome_pct.isnot(None),
                )
                .order_by(Signal.ts.desc())
                .limit(10)
                .all()
            )
            if settled_signals:
                wins   = [s for s in settled_signals if s.outcome_pct > 0]
                losses = [s for s in settled_signals if s.outcome_pct <= 0]
                win_rate   = len(wins) / len(settled_signals) * 100
                avg_win    = sum(s.outcome_pct for s in wins)   / len(wins)   if wins   else 0
                avg_loss   = sum(s.outcome_pct for s in losses) / len(losses) if losses else 0
                lines.append(
                    f"\nOUTCOME ACCURACY (last {len(settled_signals)} acted-on trades): "
                    f"{len(wins)}W / {len(losses)}L  "
                    f"win rate: {win_rate:.0f}%  "
                    f"avg win: +{avg_win:.2f}%  avg loss: {avg_loss:.2f}%"
                )
                if win_rate < 40:
                    lines.append(
                        "  ⚠ Win rate below 40% — be selective. Only act on high-confidence setups (>0.80)."
                    )
                    _inbox_throttled(
                        "warning",
                        f"Low win rate on {symbol}: {win_rate:.0f}%",
                        f"Last {len(settled_signals)} settled signals: {len(wins)}W/{len(losses)}L. "
                        f"Avg win: +{avg_win:.2f}% | Avg loss: {avg_loss:.2f}%. "
                        "Confidence threshold raised automatically.",
                        source="analyzer",
                        hours=4.0,
                    )
                elif win_rate >= 65:
                    lines.append("  ✓ Win rate healthy — strategy is working.")
        except Exception:
            pass  # outcome accuracy is optional

        # 6. Available capital (crypto only)
        if service == "crypto":
            try:
                usdc = fetch_available_usdc()
                if usdc is not None:
                    lines.append(f"\nAVAILABLE CAPITAL: ${usdc:,.2f} USDC")
                    if usdc < 50:
                        lines.append("  ⚠ Low balance — consider smaller position sizes or skip buys.")
                        _inbox_throttled(
                            "resource",
                            f"Low USDC balance: ${usdc:.2f}",
                            f"Available USDC is ${usdc:.2f}. Deposit more to enable crypto buys.",
                            source="crypto",
                            hours=6.0,
                        )
                else:
                    lines.append("\nAVAILABLE CAPITAL: (unavailable — Coinbase SDK not configured)")
            except Exception:
                pass

        # 7. Knowledge base items relevant to this symbol
        base = symbol.split("/")[0]  # e.g. "BTC" from "BTC/USD"
        kb_items = (
            db.query(KnowledgeItem)
            .filter(KnowledgeItem.tags.ilike(f"%{base}%"))
            .order_by(KnowledgeItem.ts.desc())
            .limit(2)
            .all()
        )
        if kb_items:
            lines.append(f"\nKNOWLEDGE BASE (relevant to {base}):")
            for item in kb_items:
                age_days = (datetime.utcnow() - item.ts).days if item.ts else "?"
                snippet = (item.content or "")[:300].replace("\n", " ")
                lines.append(f"  [{age_days}d ago] {item.title}\n    {snippet}")

    except Exception as e:
        lines.append(f"\n[db_context error — proceeding without history: {e}]")
    finally:
        db.close()

    lines.append("=== END DB CONTEXT SNAPSHOT ===\n")
    lines.append(AGENT_REFERENCE)
    return "\n".join(lines)
