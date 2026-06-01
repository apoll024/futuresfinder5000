"""
Crypto trade execution layer — Coinbase only.
All stock/Alpaca code has been removed. This module handles crypto signal execution via Coinbase SDK.
"""
import os, sys, json
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).parent.parent))

from db.llm_guard import llm_is_available, invalidate_cache as _llm_invalidate
from db.models import Session, Signal, Trade, Bar, get_setting

TRADE_MODE       = os.getenv("TRADE_MODE", "suggest")
MAX_POSITION_USD = float(os.getenv("MAX_POSITION_USD", "500"))
MAX_DAILY_LOSS   = float(os.getenv("MAX_DAILY_LOSS_USD", "200"))
MAX_OPEN         = int(os.getenv("MAX_OPEN_POSITIONS", "5"))
MIN_CONFIDENCE   = float(os.getenv("MIN_SIGNAL_CONFIDENCE", "0.50"))
COINBASE_BUY_CASH_BUFFER = float(os.getenv("COINBASE_BUY_CASH_BUFFER", "0.97"))
ET               = ZoneInfo("America/New_York")




def daily_pnl() -> float:
    """Net P&L from today's trades (negative = loss)."""
    session = Session()
    today_start = datetime.combine(date.today(), dtime.min)
    trades = (session.query(Trade)
              .filter(Trade.ts >= today_start,
                      Trade.status == "filled")
              .all())
    session.close()
    bought = sum(t.price * t.qty for t in trades if t.side == "buy")
    sold   = sum(t.price * t.qty for t in trades if t.side == "sell")
    return sold - bought


def _coinbase_order_id(resp: dict) -> str:
    return (
        resp.get("order_id")
        or resp.get("success_response", {}).get("order_id")
        or resp.get("order", {}).get("order_id")
        or ""
    )


def _coinbase_error(resp: dict) -> str:
    err = resp.get("error_response") or resp.get("error") or {}
    if isinstance(err, dict):
        parts = [err.get("error"), err.get("message"), err.get("error_details")]
        return " | ".join(str(p) for p in parts if p)
    errs = resp.get("errs") or resp.get("errors")
    if errs:
        return str(errs)
    return str(err) if err else "Coinbase did not return an order id"


def _float_from_preview(preview: dict, key: str) -> float:
    raw = preview.get(key, 0)
    if isinstance(raw, dict):
        raw = raw.get("value", 0)
    try:
        return float(raw or 0)
    except (TypeError, ValueError):
        return 0.0


def _coinbase_buy_size(cb_sym: str, available_quote: float, max_usd: float) -> tuple[float, str]:
    from trade.coinbase_client import preview_market_buy

    buffers = [COINBASE_BUY_CASH_BUFFER, 0.95, 0.90, 0.85]
    tried = []
    for buffer in buffers:
        quote = round(min(max_usd, available_quote * buffer), 2)
        if quote < 1:
            continue
        try:
            preview = preview_market_buy(cb_sym, quote)
            errs = preview.get("errs") or preview.get("errors") or []
            total = _float_from_preview(preview, "order_total")
            fees = _float_from_preview(preview, "commission_total")
            required = (total or quote) + fees
            if not errs and required <= available_quote:
                return quote, f"preview ok: order=${total or quote:.2f}, fees=${fees:.2f}, required=${required:.2f}"
            tried.append(f"${quote:.2f}: errs={errs or 'none'}, required=${required:.2f}")
        except Exception as e:
            tried.append(f"${quote:.2f}: preview error={e}")
    return 0.0, "; ".join(tried)


def _coinbase_buy_product(symbol: str) -> tuple[str, float, str]:
    from trade.coinbase_client import coinbase_symbol, get_crypto_balance, product_exists

    base = symbol.split("/")[0].upper()
    usd = get_crypto_balance("USD")
    usdc = get_crypto_balance("USDC")
    usdc_product = f"{base}-USDC"
    if usdc >= usd and usdc > 0 and product_exists(usdc_product):
        return usdc_product, usdc, "USDC"
    return coinbase_symbol(symbol), usd, "USD"


def close_all_positions(reason: str = "EOD"):
    """Stub — bulk close not implemented for crypto-only mode."""
    print(f"  [executor] close_all_positions({reason}) called — no-op in crypto-only mode")


def eod_check():
    """No-op — crypto trades 24/7."""
    pass


# ── Crypto helpers ─────────────────────────────────────────────────────────────

CRYPTO_BASES = {"BTC", "ETH", "SOL", "AVAX", "MATIC", "DOT", "ADA", "LINK",
                "UNI", "DOGE", "SHIB", "LTC", "XRP", "ATOM", "NEAR", "APT"}


def is_crypto(symbol: str) -> bool:
    """True for crypto symbols in both BTC/USD (stream) and BTCUSD (position) formats."""
    if "/" in symbol:
        return True
    upper = symbol.upper()
    return any(upper.startswith(base) for base in CRYPTO_BASES)


def _get_crypto_position_qty(symbol: str) -> float:
    """Return available quantity for a crypto asset via Coinbase."""
    from trade.coinbase_client import is_configured as cb_ok, get_crypto_balance
    if cb_ok():
        return get_crypto_balance(symbol.split("/")[0])
    return 0.0


# ── Options trading ────────────────────────────────────────────────────────────

def get_option_chain(underlying: str, direction: str, dte_max: int = 14) -> list:
    """Options trading via Alpaca has been removed. Returns empty list."""
    return []



def pick_option_contract(contracts: list, current_price: float,
                         moneyness: str, direction: str):
    """
    Select the best contract from a chain by targeting a strike near the given
    moneyness, preferring contracts that have a recent close_price.
    """
    if not contracts:
        return None

    # Map moneyness → target strike multiplier
    multipliers = {
        "atm":       1.00,
        "slight_otm": 1.02 if direction == "call" else 0.98,
        "deep_otm":   1.05 if direction == "call" else 0.95,
        "itm":        0.97 if direction == "call" else 1.03,
    }
    target_strike = current_price * multipliers.get(moneyness, 1.00)

    def dist(c):
        try:
            return abs(float(c.strike_price or 0) - target_strike)
        except Exception:
            return 99999.0

    contracts.sort(key=dist)

    # Return the first contract with a usable close_price; fall back to closest
    for c in contracts[:5]:
        try:
            if c.close_price and float(c.close_price) > 0:
                return c
        except Exception:
            pass
    return contracts[0]



def execute_crypto_signal(signal_id: int):
    """
    Execute a crypto signal via Coinbase Advanced Trade (primary) or Alpaca (fallback).
    Buys use USD notional; sells use the full available base-currency balance.
    """
    # CRITICAL SAFETY CONTROL: block all trades if LLM is unavailable
    if not llm_is_available():
        from db.models import write_inbox
        write_inbox("alert", "LLM Unavailable — Crypto Trade Blocked",
                    "execute_crypto_signal called but LLM endpoint is unreachable. "
                    "Trade execution is blocked until LLM is restored.",
                    source="executor")
        print(f"  [executor] ABORT execute_crypto_signal({signal_id}) — LLM unavailable")
        return
    session = Session()
    sig = session.query(Signal).filter(Signal.id == signal_id).first()
    if not sig or sig.action == "hold":
        session.close()
        return

    # Read mode from DB (live/suggest) — falls back to env var if not set
    mode = get_setting("trade_mode", TRADE_MODE)
    if mode == "off":
        print(f"  [executor] TRADING OFF — blocking crypto {sig.symbol} {sig.action.upper()}")
        session.close()
        return
    print(f"  [executor] Crypto Mode={mode}  {sig.symbol} {sig.action.upper()}  conf={sig.confidence:.2f}")

    if sig.confidence < MIN_CONFIDENCE:
        print(f"  [executor] Below confidence threshold ({sig.confidence:.2f} < {MIN_CONFIDENCE}) — skip")
        session.close()
        return

    # Watchlist-only mode: skip symbols not in the current crypto watchlist
    if get_setting("watchlist_only", "true") == "true":
        crypto_wl = [s.strip().upper() for s in get_setting("crypto_symbols", "").split(",") if s.strip()]
        if crypto_wl and sig.symbol.upper() not in crypto_wl:
            print(f"  [executor] SKIP — {sig.symbol} not in crypto watchlist (watchlist_only mode)")
            session.close()
            return

    indicators = json.loads(sig.indicators or "{}")
    price      = indicators.get("close", 1.0)

    trade = Trade(
        symbol=sig.symbol, side=sig.action, qty=0, price=price,
        mode=mode, signal_id=sig.id, status="suggested",
    )

    if mode == "suggest":
        session.add(trade)
        sig.acted_on = True
        action_str = sig.action.upper()
        sym_str    = sig.symbol
        session.commit()
        session.close()
        print(f"  [executor] Crypto suggestion: {action_str} {sym_str} @ ~${price:.2f}")
        return

    # Safety: daily loss halt
    pnl = daily_pnl()
    if pnl <= -MAX_DAILY_LOSS:
        print(f"  [executor] HALT — daily loss limit hit (${pnl:.2f}). Closing all positions.")
        close_all_positions("LOSS-HALT")
        session.close()
        return

    # ── Fee-aware profitability gate (live sell orders only) ──────────────────
    # Coinbase charges ~0.6% taker fee per side (~1.2% round trip).
    # Block a sell if net proceeds after fees would be less than the cost basis
    # UNLESS confidence >= 0.80 (high-conviction stop-loss override).
    COINBASE_FEE_RATE = 0.006
    if sig.action == "sell":
        recent_buy = (
            session.query(Trade)
            .filter(
                Trade.symbol == sig.symbol,
                Trade.side   == "buy",
                Trade.status.in_(["filled", "submitted"]),
            )
            .order_by(Trade.ts.desc())
            .first()
        )
        if recent_buy and recent_buy.price and recent_buy.price > 0:
            cost_with_fee = recent_buy.price * (1 + COINBASE_FEE_RATE)
            net_proceeds  = price * (1 - COINBASE_FEE_RATE)
            if net_proceeds < cost_with_fee:
                pct_loss = (net_proceeds / cost_with_fee - 1) * 100
                if sig.confidence < 0.80:
                    print(
                        f"  [executor] FEE-GATE skip sell {sig.symbol} — "
                        f"net loss after fees ({pct_loss:.2f}%), "
                        f"conf={sig.confidence:.2f} < 0.80. "
                        "Raise confidence to 0.80+ for stop-loss override."
                    )
                    session.close()
                    return
                else:
                    print(
                        f"  [executor] STOP-LOSS sell {sig.symbol} — "
                        f"net loss after fees ({pct_loss:.2f}%) but "
                        f"conf={sig.confidence:.2f} >= 0.80 (stop-loss override)"
                    )

    from trade.coinbase_client import (
        is_configured as cb_ok, coinbase_symbol,
        place_market_buy, place_market_sell, get_crypto_balance,
    )

    # Determine buy size: respect DB max_position_usd AND available balance
    max_pos_db = float(get_setting("max_position_usd", str(MAX_POSITION_USD)))

    try:
        if cb_ok():
            # ── Coinbase execution ──────────────────────────────────────────
            cb_sym = coinbase_symbol(sig.symbol)
            if sig.action == "buy":
                cb_sym, available_quote, quote_currency = _coinbase_buy_product(sig.symbol)
                buy_size, size_note = _coinbase_buy_size(cb_sym, available_quote, max_pos_db)
                if buy_size < 1.0:
                    trade.status = "rejected"
                    trade.alpaca_id = "insufficient-funds"
                    print(f"  [executor] Insufficient {quote_currency} after Coinbase fees (${available_quote:.2f}) — {size_note}")
                    session.add(trade)
                    sig.acted_on = True
                    session.commit()
                    session.close()
                    return
                print(f"  [executor] Buying ${buy_size:.2f} of {cb_sym} (available {quote_currency}: ${available_quote:.2f}, max: ${max_pos_db:.2f}; {size_note})")
                resp      = place_market_buy(cb_sym, buy_size)
                trade.qty = round(buy_size / price, 6) if price > 0 else 0
            else:
                base = sig.symbol.split("/")[0]
                qty  = get_crypto_balance(base)
                if qty <= 0:
                    print(f"  [executor] No Coinbase balance for {base} — sell skipped")
                    session.close()
                    return
                resp      = place_market_sell(cb_sym, qty)
                trade.qty = qty
            order_id = _coinbase_order_id(resp)
            if resp.get("success") is False or resp.get("error_response") or not order_id:
                reason = _coinbase_error(resp)
                trade.alpaca_id = str(order_id or reason)[:50]
                trade.status = "rejected"
                print(f"  [executor] Coinbase {sig.action.upper()} {cb_sym} rejected: {reason}")
            else:
                trade.alpaca_id = str(order_id)[:50]
                trade.status    = "submitted"
                print(f"  [executor] Coinbase {sig.action.upper()} {cb_sym} → {order_id}")
        else:
            print(f"  [executor] Coinbase not configured — {sig.symbol} {sig.action.upper()} skipped")
            trade.status = "skipped"
    except Exception as e:
        trade.status = "rejected"
        print(f"  [executor] Crypto order rejected: {e}")

    session.add(trade)
    sig.acted_on = True
    session.commit()
    session.close()
