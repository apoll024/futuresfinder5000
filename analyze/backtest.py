"""
Agent 3 — Backtesting Engine
=============================
Replays settled signals from the DB and computes performance metrics.
Called on-demand via POST /api/backtest/run — never runs continuously.

Metrics returned:
  win_rate, avg_win_pct, avg_loss_pct, total_pct, sharpe (mean/stdev),
  best_trade, worst_trade, per-symbol breakdown, daily P&L series.
"""
import sys, statistics
from datetime import datetime, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from db.models import Session, Signal, write_inbox


def run_backtest(symbol: str = None, days: int = 30) -> dict:
    """
    Replay settled signals and compute performance metrics.

    Parameters
    ----------
    symbol : filter to one symbol (e.g. "BTC/USD"). None = all symbols.
    days   : look-back window in days (1–365).

    Returns dict with full metrics + per-symbol breakdown + daily P&L series.
    """
    db = Session()
    cutoff = datetime.utcnow() - timedelta(days=max(1, min(365, days)))

    q = (
        db.query(Signal)
        .filter(
            Signal.settled == True,
            Signal.outcome_pct.isnot(None),
            Signal.acted_on == True,
            Signal.ts >= cutoff,
        )
    )
    if symbol:
        q = q.filter(Signal.symbol == symbol.upper())

    signals = q.order_by(Signal.ts).all()
    db.close()

    if not signals:
        return {"error": "No settled signals in range", "days": days,
                "symbol": symbol or "ALL", "total": 0}

    wins   = [s for s in signals if s.outcome_pct > 0]
    losses = [s for s in signals if s.outcome_pct <= 0]

    win_rate  = round(len(wins) / len(signals) * 100, 1) if signals else 0
    avg_win   = round(sum(s.outcome_pct for s in wins)   / len(wins),   3) if wins   else 0
    avg_loss  = round(sum(s.outcome_pct for s in losses) / len(losses), 3) if losses else 0
    total_pct = round(sum(s.outcome_pct for s in signals), 3)

    returns = [s.outcome_pct for s in signals]
    sharpe  = 0.0
    if len(returns) > 1:
        mean = statistics.mean(returns)
        std  = statistics.stdev(returns)
        sharpe = round(mean / std, 3) if std else 0.0

    best  = max(signals, key=lambda s: s.outcome_pct)
    worst = min(signals, key=lambda s: s.outcome_pct)

    # Per-symbol breakdown
    by_sym: dict = {}
    for s in signals:
        e = by_sym.setdefault(s.symbol, {"trades": 0, "wins": 0, "total_pct": 0.0})
        e["trades"]    += 1
        e["wins"]      += 1 if s.outcome_pct > 0 else 0
        e["total_pct"]  = round(e["total_pct"] + s.outcome_pct, 3)
    for sym, e in by_sym.items():
        e["win_rate"] = round(e["wins"] / e["trades"] * 100, 1)

    # Daily P&L series
    from collections import defaultdict
    daily: dict = defaultdict(float)
    for s in signals:
        if s.ts:
            daily[s.ts.strftime("%Y-%m-%d")] = round(
                daily[s.ts.strftime("%Y-%m-%d")] + s.outcome_pct, 3
            )
    daily_series = [{"date": k, "pct": v} for k, v in sorted(daily.items())]

    result = {
        "days":        days,
        "symbol":      symbol or "ALL",
        "total":       len(signals),
        "wins":        len(wins),
        "losses":      len(losses),
        "win_rate":    win_rate,
        "avg_win_pct": avg_win,
        "avg_loss_pct":avg_loss,
        "total_pct":   total_pct,
        "sharpe":      sharpe,
        "best_trade":  {"symbol": best.symbol,  "pct": best.outcome_pct,  "ts": str(best.ts)},
        "worst_trade": {"symbol": worst.symbol, "pct": worst.outcome_pct, "ts": str(worst.ts)},
        "by_symbol":   by_sym,
        "daily":       daily_series,
        "run_at":      datetime.utcnow().isoformat(),
    }

    write_inbox(
        "update",
        f"Backtest ({days}d {symbol or 'ALL'}): {win_rate:.0f}% win rate",
        f"Traded {len(signals)} signals | W:{len(wins)} L:{len(losses)} | "
        f"Avg win: +{avg_win:.3f}% | Avg loss: {avg_loss:.3f}% | "
        f"Total: {total_pct:+.3f}% | Sharpe: {sharpe}",
        source="backtest",
    )
    return result
