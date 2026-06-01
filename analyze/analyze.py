"""
Day-trading signal analysis optimized for leveraged ETFs.

Leveraged ETF pairs (bull / bear):
  TQQQ / SQQQ  ->  underlying QQQ  (Nasdaq 100, 3x)
  UPRO / SPXU  ->  underlying SPY  (S&P 500, 3x)
  SOXL / SOXS  ->  underlying SOXX (Semiconductors, 3x)
  TECL / TECS  ->  underlying XLK  (Technology, 3x)
  TNA  / TZA   ->  underlying IWM  (Russell 2000, 3x)

Key leveraged ETF rules baked into LLM prompt:
  - Daily rebalancing decay means NO overnight holds (ever)
  - Trade the underlying direction -- TQQQ when QQQ trending up, SQQQ when down
  - VWAP on the UNDERLYING is the primary entry signal
  - Morning volatility (first 15 min) = avoid new entries
  - Volume spike on leveraged ETF = confirms underlying move
  - Stop implied at opening range low (bull) or high (bear)
"""
import os, sys, json, threading
from datetime import datetime, time as dtime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import ta.trend as ta_trend
import ta.momentum as ta_momentum
import ta.volatility as ta_vol
from zoneinfo import ZoneInfo
from db.models import Session, Bar, Signal, Trade, init_db, get_setting, set_setting, log_llm_session
from analyze.ai_context import SYSTEM_INSTRUCTIONS, build_db_context
from db.llm_guard import llm_is_available
from db.llm_gateway import chat_completion, extract_json_object, risk_review_decision

LLM_API_URL    = os.getenv("LLM_API_URL", "https://models.github.ai/inference/chat/completions")
MODEL          = os.getenv("LLM_MODEL", "gpt-4o")
GITHUB_TOKEN   = os.getenv("GITHUB_TOKEN", "")


def _llm_headers() -> dict:
    h = {"Content-Type": "application/json"}
    token = GITHUB_TOKEN
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h
TRADE_MODE  = os.getenv("TRADE_MODE", "suggest")
API_KEY     = os.getenv("ALPACA_API_KEY")
SECRET_KEY  = os.getenv("ALPACA_SECRET_KEY")
ET          = ZoneInfo("America/New_York")
MARKET_OPEN = dtime(9, 30)
ENTRY_START = dtime(9, 45)
CUTOFF      = dtime(15, 45)

PAIRS = {
    "TQQQ": {"bear": "SQQQ", "underlying": "QQQ",  "leverage": 3},
    "SQQQ": {"bull": "TQQQ", "underlying": "QQQ",  "leverage": -3},
    "UPRO": {"bear": "SPXU", "underlying": "SPY",  "leverage": 3},
    "SPXU": {"bull": "UPRO", "underlying": "SPY",  "leverage": -3},
    "SOXL": {"bear": "SOXS", "underlying": "SOXX", "leverage": 3},
    "SOXS": {"bull": "SOXL", "underlying": "SOXX", "leverage": -3},
    "TECL": {"bear": "TECS", "underlying": "XLK",  "leverage": 3},
    "TECS": {"bull": "TECL", "underlying": "XLK",  "leverage": -3},
    "TNA":  {"bear": "TZA",  "underlying": "IWM",  "leverage": 3},
    "TZA":  {"bull": "TNA",  "underlying": "IWM",  "leverage": -3},
}


def get_open_position(symbol: str) -> dict | None:
    """Return today's open position for symbol, or None if flat.
    In suggest mode: derived from today's trade log.
    In paper/live mode: queried from Alpaca directly.
    """
    if TRADE_MODE != "suggest":
        try:
            from alpaca.trading.client import TradingClient
            client = TradingClient(API_KEY, SECRET_KEY, paper=True)
            pos = client.get_open_position(symbol)
            return {"qty": float(pos.qty), "avg_entry": float(pos.avg_entry_price),
                    "unrealized_pnl": float(pos.unrealized_pl)}
        except Exception:
            return None
    # Suggest mode: net from today's trade log
    session = Session()
    try:
        today_start = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0)
        trades = session.query(Trade).filter(
            Trade.symbol == symbol, Trade.ts >= today_start
        ).all()
        bought = sum(t.qty for t in trades if t.side == "buy")
        sold   = sum(t.qty for t in trades if t.side == "sell")
        net    = bought - sold
        return {"qty": net, "avg_entry": None, "unrealized_pnl": None} if net > 0 else None
    finally:
        session.close()


def _backfill_new_symbol(symbol: str):
    try:
        from ingest.historical import ingest_historical
        ingest_historical(symbols_override=[symbol])
        print(f"  [analyze] Backfill complete for new symbol {symbol}")
    except Exception as e:
        print(f"  [analyze] Backfill error for {symbol}: {e}")


def market_is_open(allow_entry: bool = True) -> bool:
    now = datetime.now(ET).time()
    if allow_entry:
        return ENTRY_START <= now <= CUTOFF
    return MARKET_OPEN <= now <= CUTOFF


def fetch_bars_today(symbol: str) -> pd.DataFrame:
    session = Session()
    today_open = datetime.now(ET).replace(hour=9, minute=30, second=0, microsecond=0)
    rows = (session.query(Bar)
            .filter(Bar.symbol == symbol, Bar.ts >= today_open)
            .order_by(Bar.ts.asc())
            .all())
    session.close()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([{
        "ts": r.ts, "open": r.open, "high": r.high,
        "low": r.low, "close": r.close, "volume": r.volume
    } for r in rows])


def compute_indicators(df: pd.DataFrame) -> dict:
    close  = df["close"]
    high   = df["high"]
    low    = df["low"]
    volume = df["volume"]

    # VWAP -- primary day trading signal (manual calculation)
    df["vwap"] = (
        (volume * (high + low + close) / 3).cumsum()
        / volume.cumsum()
    )

    # Trend / momentum (ta library)
    df["EMA_9"]          = ta_trend.ema_indicator(close, window=9)
    df["EMA_21"]         = ta_trend.ema_indicator(close, window=21)
    df["RSI_9"]          = ta_momentum.rsi(close, window=9)
    df["MACD_12_26_9"]   = ta_trend.macd(close, window_slow=26, window_fast=12)
    df["MACDs_12_26_9"]  = ta_trend.macd_signal(close, window_slow=26, window_fast=12, window_sign=9)
    df["ATRr_14"]        = ta_vol.average_true_range(high, low, close, window=14)

    # Opening range (first 15 bars)
    or_bars = df.head(15)
    or_high = float(or_bars["high"].max()) if len(or_bars) >= 5 else None
    or_low  = float(or_bars["low"].min())  if len(or_bars) >= 5 else None

    last      = df.iloc[-1]
    avg_vol   = float(df["volume"].tail(20).mean())
    vol_ratio = float(last["volume"]) / avg_vol if avg_vol > 0 else 1.0

    return {
        "close":         round(float(last["close"]), 4),
        "vwap":          round(float(last["vwap"]), 4),
        "above_vwap":    bool(last["close"] > last["vwap"]),
        "ema_9":         round(float(last.get("EMA_9", 0) or 0), 4),
        "ema_21":        round(float(last.get("EMA_21", 0) or 0), 4),
        "ema9_above_21": bool((last.get("EMA_9", 0) or 0) > (last.get("EMA_21", 0) or 0)),
        "rsi_9":         round(float(last.get("RSI_9", 0) or 0), 2),
        "macd":          round(float(last.get("MACD_12_26_9", 0) or 0), 4),
        "macd_signal":   round(float(last.get("MACDs_12_26_9", 0) or 0), 4),
        "macd_bullish":  bool((last.get("MACD_12_26_9", 0) or 0) > (last.get("MACDs_12_26_9", 0) or 0)),
        "atr_14":        round(float(last.get("ATRr_14", 0) or 0), 4),
        "volume":        int(last["volume"]),
        "volume_ratio":  round(vol_ratio, 2),
        "or_high":       round(or_high, 4) if or_high else None,
        "or_low":        round(or_low, 4)  if or_low  else None,
        "or_breakout":   ("above" if or_high and last["close"] > or_high
                          else "below" if or_low and last["close"] < or_low
                          else "inside"),
    }


def call_llm(symbol: str, indicators: dict, underlying_indicators,
             recent_closes: list, minutes_left: int, leverage: int,
             position: dict | None = None) -> dict:

    # Position state
    if position:
        entry_str = f" @ ${position['avg_entry']:.2f}" if position.get('avg_entry') else ""
        pnl_str   = f", unrealized P&L: ${position['unrealized_pnl']:.2f}" if position.get('unrealized_pnl') is not None else ""
        position_line = f"LONG {int(position['qty'])} shares{entry_str}{pnl_str}"
        pos_rule      = "Position guidance: you already hold this symbol. Prefer sell to reduce/exit or hold; add only if risk/reward strongly justifies it."
    else:
        position_line = "NONE (flat — no open position today)"
        pos_rule      = "Position guidance: you are flat. Buy if upside/reward justifies risk; hold if no edge. Do not signal sell unless there is an existing position to close."

    underlying_section = ""
    if underlying_indicators:
        pair_info = PAIRS.get(symbol, {})
        und       = pair_info.get("underlying", "")
        direction = "BULL (3x long)" if leverage > 0 else "BEAR (3x short/inverse)"
        underlying_section = f"""
Underlying index ({und}) -- PRIMARY directional signal:
{json.dumps(underlying_indicators, indent=2)}

{symbol} is a {direction} ETF tracking {und}.
If {und} is above VWAP and trending up -> favor TQQQ/UPRO/SOXL side.
If {und} is below VWAP and trending down -> favor SQQQ/SPXU/SOXS side.
"""

    # Options section — only included when options trading is enabled
    options_enabled = get_setting("options_enabled", "false") == "true"
    pair_info       = PAIRS.get(symbol, {})
    underlying_sym  = pair_info.get("underlying", "")

    if options_enabled and underlying_sym:
        options_section = f"""
OPTIONS TRADING (ENABLED — underlying: {underlying_sym}):
You MAY recommend an options trade on {underlying_sym} (the underlying, NOT the leveraged ETF).
Only recommend options when ALL are true:
  1. Your ETF action is 'buy' or 'sell' (not 'hold')
  2. Your confidence is >= 0.85
  3. The directional move is expected to be meaningful (strong trend / breakout)
Appropriate strategies:
  - ATM call (moneyness: 'atm'): moderate conviction, expect moderate move
  - Slight OTM call (moneyness: 'slight_otm'): high conviction, expect larger move
  - Deep OTM call (moneyness: 'deep_otm'): very high conviction, expect big move
  - ITM call (moneyness: 'itm'): more expensive but moves with delta ≈ 0.7+
  (Use 'put' equivalents for bearish signals)
DTE preference: 5 = 0DTE+ aggressive, 7 = standard weekly, 14 = next weekly
If NOT recommending options, set option_rec to null.
"""
        option_rec_schema = (
            '  "option_rec": null | {\n'
            '    "direction": "call" | "put",\n'
            '    "moneyness": "atm" | "slight_otm" | "deep_otm" | "itm",\n'
            '    "dte_preference": 5 | 7 | 14,\n'
            '    "reasoning": "why options + which moneyness"\n'
            '  }\n'
        )
    else:
        options_section    = ""
        option_rec_schema  = '  "option_rec": null\n'

    prompt = f"""{SYSTEM_INSTRUCTIONS}
{build_db_context(symbol, service="analyze")}
You are FuturesFinder5000, an autonomous trading agent. Your mission is to grow allocated capital while using caution and respecting configured risk limits.

IDENTITY & PURPOSE:
- You manage real money. Every buy/sell signal you issue gets executed. Act accordingly.
- Your goal is profitable decision-making with controlled drawdown.
- Think like a professional trader: weigh reward vs risk, but do not freeze because one indicator is imperfect.
- A day with no trades is acceptable, but missed profitable opportunities also matter.

CURRENT ANALYSIS TARGET: {symbol} ({leverage:+d}x leveraged ETF) | {minutes_left} minutes remaining in session.

POSITION STATE: {position_line}
{pos_rule}

TRADING DISCRETION:
- Decide buy/sell/hold from the full evidence set: trend, VWAP, MACD, RSI, volume, opening range, time remaining, recent closes, broader market context, prior outcomes, and current position.
- Indicators are not hard gates. They are inputs into your expected value and risk/reward judgment.
- Buy when you believe the opportunity has positive expected value after risk; sell when risk/reward favors taking profit or cutting exposure; hold when edge is insufficient.
- Use higher confidence for clear asymmetric opportunities and lower confidence when the edge is marginal or data is conflicted.
- Respect configured capital limits and daily loss protection.
{options_section}{underlying_section}
{symbol} technical indicators (1-min bars):
{json.dumps(indicators, separators=(",", ":"))}

Recent 1-min closes (oldest → newest):
{recent_closes}

Respond ONLY with valid JSON — no prose, no explanation outside the JSON:
{{
  "action": "buy" | "sell" | "hold",
  "confidence": 0.0-1.0,
  "reasoning": "cite specific values: VWAP delta, volume ratio, RSI, MACD crossover, minutes left",
  "add_symbol": null,
{option_rec_schema}}}
add_symbol: set to a ticker string ONLY if you identify a high-conviction opportunity in a liquid US equity or leveraged ETF not currently tracked. Must be alphanumeric, ≤ 5 chars. Otherwise null."""

    content, meta = chat_completion(
        purpose="analyze:analyst",
        symbol=symbol,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=320,
        temperature=0.1,
        timeout=90,
        critical=True,
    )
    result = extract_json_object(content)
    result = risk_review_decision(
        service="analyze",
        symbol=symbol,
        decision=result,
        context={
            "minutes_left": minutes_left,
            "leverage": leverage,
            "position": position,
            "indicators": indicators,
            "underlying": underlying_indicators,
        },
    )
    log_llm_session(service="analyze", model=MODEL, symbol=symbol,
                    prompt=prompt, response=content,
                    action=result.get("action"), confidence=result.get("confidence"),
                    latency_ms=meta.get("latency_ms"))
    return result


def run_analysis(symbol: str):
    if not llm_is_available():
        print(f"  [{symbol}] SKIP run_analysis — LLM unavailable")
        return
    if get_setting("stocks_enabled", "true") != "true":
        return
    if not market_is_open(allow_entry=True):
        return

    df = fetch_bars_today(symbol)
    if len(df) < 10:
        print(f"  [{symbol}] Not enough bars yet ({len(df)})")
        return

    now          = datetime.now(ET)
    cutoff_dt    = now.replace(hour=15, minute=45, second=0, microsecond=0)
    minutes_left = max(0, int((cutoff_dt - now).total_seconds() / 60))

    indicators = compute_indicators(df)
    closes     = [round(float(c), 2) for c in df["close"].tail(15).tolist()]
    leverage   = PAIRS.get(symbol, {}).get("leverage", 1)

    underlying_ind = None
    und_symbol = PAIRS.get(symbol, {}).get("underlying")
    if und_symbol:
        und_df = fetch_bars_today(und_symbol)
        if len(und_df) >= 10:
            underlying_ind = compute_indicators(und_df)

    result = call_llm(symbol, indicators, underlying_ind, closes, minutes_left, leverage,
                      position=get_open_position(symbol))

    session = Session()
    sig = Signal(
        symbol=symbol,
        action=result.get("action", "hold"),
        confidence=result.get("confidence", 0.0),
        reasoning=result.get("reasoning", ""),
        indicators=json.dumps(indicators),
        entry_price=indicators.get("close"),
    )
    session.add(sig)
    session.commit()
    sig_id = sig.id
    session.close()

    print(f"  [{symbol}] {sig.action.upper()} conf={sig.confidence:.2f} | {sig.reasoning[:100]}")

    # Handle dynamic symbol addition (market mode only — blocked in watchlist_only mode)
    new_sym = str(result.get("add_symbol") or "").strip().upper()
    if (new_sym and 1 < len(new_sym) <= 6 and new_sym.isalpha()
            and get_setting("watchlist_only", "true") != "true"):
        current = get_setting("symbols")
        current_list = [s.strip() for s in current.split(",") if s.strip()]
        if new_sym not in current_list:
            current_list.append(new_sym)
            set_setting("symbols", ",".join(current_list))
            print(f"  [{symbol}] LLM added new symbol to watchlist: {new_sym}")
            threading.Thread(target=_backfill_new_symbol, args=(new_sym,), daemon=True).start()

    from trade.executor import execute_signal
    execute_signal(sig_id)

    # Execute option recommendation if present and options are enabled
    option_rec = result.get("option_rec")
    if (option_rec and isinstance(option_rec, dict)
            and get_setting("options_enabled", "false") == "true"):
        und_symbol = PAIRS.get(symbol, {}).get("underlying")
        if und_symbol:
            try:
                from trade.executor import execute_option_order
                execute_option_order(und_symbol, option_rec, sig_id, TRADE_MODE)
            except Exception as e:
                print(f"  [{symbol}] Option order error: {e}")


if __name__ == "__main__":
    symbol = sys.argv[1] if len(sys.argv) > 1 else "TQQQ"
    init_db()
    run_analysis(symbol)
