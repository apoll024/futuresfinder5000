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
       • Relevant knowledge base items (tagged with symbol or "crypto")
"""
import sys, json
from datetime import date, datetime, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from db.models import Session, Signal, Trade, LLMSession, KnowledgeItem

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
  5. KNOWLEDGE BASE    — Any relevant market notes or EOD summaries? Apply them.

RISK RULES (non-negotiable):
  • Never issue 'buy' when already long — no pyramiding
  • Never issue 'sell' when flat — you cannot sell what you do not own
  • No new entries in the last 30 minutes of stock market session
  • Confidence < 0.65 → HOLD, no exceptions
  • Daily loss halt: if P&L ≤ −$200, signal HOLD on everything
=== END STANDING INSTRUCTIONS ===
""".strip()


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
            # Derive open/flat from buys vs sells
            bought_qty = sum(t.qty for t in today_trades if t.side == "buy" and t.status in ("filled", "suggested"))
            sold_qty   = sum(t.qty for t in today_trades if t.side == "sell" and t.status in ("filled", "suggested"))
            net_qty    = bought_qty - sold_qty
            if net_qty > 0:
                lines.append(f"  → NET POSITION: LONG {net_qty:.2f} shares/units")
            else:
                lines.append(f"  → NET POSITION: FLAT")
        else:
            lines.append(f"\nTODAY'S TRADES: none yet for {symbol}.")

        # 3. Daily P&L (all symbols)
        all_today_trades = (
            db.query(Trade)
            .filter(
                Trade.ts >= datetime.strptime(today_str, "%Y-%m-%d"),
                Trade.status == "filled",
            )
            .all()
        )
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

        # 5. Knowledge base items relevant to this symbol
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
    return "\n".join(lines)
