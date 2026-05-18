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
import os, sys, json
from datetime import datetime, time as dtime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import ta.trend as ta_trend
import ta.momentum as ta_momentum
import ta.volatility as ta_vol
import requests
from zoneinfo import ZoneInfo
from db.models import Session, Bar, Signal, init_db

LLM_API_URL = os.getenv("LLM_API_URL", "http://ollama:11434/v1/chat/completions")
MODEL       = os.getenv("LLM_MODEL", "llama3.1:8b")
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
    df["MACD_12_26_9"]   = ta_trend.macd(close, window_slow=26, window_fast=12, window_sign=9)
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
             recent_closes: list, minutes_left: int, leverage: int) -> dict:

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

    prompt = f"""You are a day trading assistant specializing in leveraged ETFs.
Analyzing: {symbol} ({leverage:+d}x leveraged ETF) | {minutes_left} minutes left in session.

CRITICAL RULES:
1. NEVER hold overnight -- daily decay destroys value; EOD close is mandatory
2. Volume ratio > 1.5 confirms moves; < 0.8 means avoid new entries
3. Opening range breakout above or_high with volume = strong buy signal
4. If < 30 min left: only "sell" to exit or "hold", NO new "buy" entries
5. If < 15 min left: only "sell" or "hold"
6. RSI > 75 = overbought; RSI < 25 = oversold
{underlying_section}
{symbol} indicators (1-min bars):
{json.dumps(indicators, indent=2)}

Recent 1-min closes (oldest to newest):
{recent_closes}

Respond ONLY with valid JSON:
{{
  "action": "buy" | "sell" | "hold",
  "confidence": 0.0-1.0,
  "reasoning": "cite specific values: VWAP position, OR breakout, volume ratio, underlying direction"
}}"""

    r = requests.post(LLM_API_URL,
                      headers={"Content-Type": "application/json"},
                      json={"model": MODEL,
                            "messages": [{"role": "user", "content": prompt}],
                            "max_tokens": 350, "temperature": 0.1},
                      timeout=120)
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"].strip()
    start = content.find("{")
    end   = content.rfind("}") + 1
    return json.loads(content[start:end])


def run_analysis(symbol: str):
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

    result = call_llm(symbol, indicators, underlying_ind, closes, minutes_left, leverage)

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

    from trade.executor import execute_signal
    execute_signal(sig_id)


if __name__ == "__main__":
    symbol = sys.argv[1] if len(sys.argv) > 1 else "TQQQ"
    init_db()
    run_analysis(symbol)
