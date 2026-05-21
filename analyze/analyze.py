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
import requests
from zoneinfo import ZoneInfo
from db.models import Session, Bar, Signal, Trade, init_db, get_setting, set_setting

LLM_API_URL = os.getenv("LLM_API_URL", "http://ollama:11434/v1/chat/completions")
MODEL       = os.getenv("LLM_MODEL", "llama3.2:3b")
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
        pos_rule      = "FLAT constraint: you hold a LONG position. Action may be 'sell' (exit) or 'hold'. Do NOT use 'buy' to pyramid."
    else:
        position_line = "NONE (flat — no open position today)"
        pos_rule      = "FLAT constraint: you have NO open position. Action MUST be 'buy' or 'hold'. 'sell' is INVALID — you cannot sell what you do not own."

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

    prompt = f"""You are FuturesFinder5000, an autonomous day-trading agent. Your sole mission is to grow the capital allocated to you through disciplined, rules-based leveraged ETF trading.

IDENTITY & PURPOSE:
- You manage real money. Every buy/sell signal you issue gets executed. Act accordingly.
- Your goal is consistent capital growth with controlled drawdown — not speculation.
- Think like a professional prop trader: protect capital first, grow it second.
- You are evaluated on net P&L at end of day. A day with no trades is better than a losing trade.

CURRENT ANALYSIS TARGET: {symbol} ({leverage:+d}x leveraged ETF) | {minutes_left} minutes remaining in session.

POSITION STATE: {position_line}
{pos_rule}

ENTRY RULES — only buy when ALL of the following are true:
1. Underlying is above VWAP and trending in the direction you need
2. Volume ratio ≥ 1.2 (confirms participation, not a fake move)
3. MACD signal line crossed bullish (for bull ETF) or bearish (for bear ETF)
4. RSI is NOT overbought (< 72 for buys) and NOT oversold (< 28 for sells)
5. Opening range is established (at least 15 min of session elapsed)
6. ≥ 30 minutes remain in the session (no new entries inside last 30 min)

EXIT RULES — sell/close when ANY of the following:
1. < 30 minutes left — mandatory EOD exit, NO exceptions (decay will kill you overnight)
2. RSI crosses overbought (> 75) while long — take profit
3. Price closes below VWAP on the underlying — trend has reversed
4. Volume ratio drops below 0.7 after entry — move losing conviction

CONFIDENCE CALIBRATION:
- 0.85–1.0: All signals aligned, high volume, clear trend → strong conviction
- 0.65–0.84: Most signals aligned, proceed with normal sizing
- 0.50–0.64: Mixed signals → HOLD, do not enter
- < 0.50: Conflicting signals → HOLD

CAPITAL PRESERVATION:
- When uncertain, the correct answer is always "hold"
- One losing trade undoes multiple winning ones — be selective
- Do NOT chase moves already underway; wait for the next setup
{options_section}{underlying_section}
{symbol} technical indicators (1-min bars):
{json.dumps(indicators, indent=2)}

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

    r = requests.post(LLM_API_URL,
                      headers={"Content-Type": "application/json"},
                      json={"model": MODEL,
                            "messages": [{"role": "user", "content": prompt}],
                            "max_tokens": 450, "temperature": 0.1},
                      timeout=120)
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"].strip()
    start = content.find("{")
    end   = content.rfind("}") + 1
    return json.loads(content[start:end])


def run_analysis(symbol: str):
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

    # Handle dynamic symbol addition
    new_sym = str(result.get("add_symbol") or "").strip().upper()
    if new_sym and 1 < len(new_sym) <= 6 and new_sym.isalpha():
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
