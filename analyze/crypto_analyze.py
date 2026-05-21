"""
Crypto signal analysis engine for FuturesFinder5000.

Indicators: EMA9, EMA21, EMA50, RSI14, MACD 12/26/9, ATR14, Bollinger %B, VWAP
News + Fear/Greed: cryptocurrency.cv API (keyless, free)
24/7 trading — no market-hours gate.
"""
import os, sys, json, time, requests
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import ta.trend as ta_trend
import ta.momentum as ta_momentum
import ta.volatility as ta_vol
from db.models import Session, Bar, Signal, get_setting

LLM_API_URL    = os.getenv("LLM_API_URL", "http://ollama:11434/v1/chat/completions")
LLM_MODEL      = os.getenv("LLM_MODEL",   "llama3.2:3b")
CRYPTO_CV_BASE = "https://cryptocurrency.cv"

# Fear & Greed cache — index only updates daily; cache 1 hour
_fear_greed_cache: dict = {"value": None, "label": None, "fetched_at": 0.0}
FEAR_GREED_TTL = 3600

# Market coins cache — prices change frequently; cache 5 min
_market_cache: dict = {"coins": None, "fetched_at": 0.0}
MARKET_TTL = 300

# Trending topics cache — cache 15 min
_trending_cache: dict = {"keywords": None, "fetched_at": 0.0}
TRENDING_TTL = 900


def fetch_fear_greed() -> dict:
    """Returns fear/greed index from cryptocurrency.cv, cached 1h."""
    now = time.time()
    if (_fear_greed_cache["value"] is not None
            and now - _fear_greed_cache["fetched_at"] < FEAR_GREED_TTL):
        return _fear_greed_cache
    try:
        r = requests.get(f"{CRYPTO_CV_BASE}/api/market/fear-greed", timeout=8)
        if r.ok:
            data = r.json()
            cur  = data.get("current", {})
            _fear_greed_cache["value"]      = int(cur.get("value", 50))
            _fear_greed_cache["label"]      = str(cur.get("valueClassification", "Neutral"))
            _fear_greed_cache["fetched_at"] = now
    except Exception as e:
        print(f"  [crypto] fear-greed fetch error: {e}")
    return _fear_greed_cache


def fetch_crypto_news(ticker: str) -> list:
    """Return up to 5 recent news headlines for the given crypto ticker."""
    base = ticker.split("/")[0].upper()
    try:
        r = requests.get(f"{CRYPTO_CV_BASE}/api/news?ticker={base}&limit=5", timeout=8)
        if not r.ok:
            return []
        articles = r.json().get("articles", [])
        return [a.get("title", "") for a in articles if a.get("title")]
    except Exception as e:
        print(f"  [crypto] news fetch error for {base}: {e}")
        return []


def fetch_market_context() -> list:
    """
    Return top 20 coins from /api/market/coins (free endpoint).
    Provides macro context: prices, 24h change, volume, market cap.
    Cached 5 minutes.
    """
    now = time.time()
    if (_market_cache["coins"] is not None
            and now - _market_cache["fetched_at"] < MARKET_TTL):
        return _market_cache["coins"]
    try:
        r = requests.get(f"{CRYPTO_CV_BASE}/api/market/coins?limit=20", timeout=10)
        if r.ok:
            coins = r.json().get("coins", [])
            _market_cache["coins"]      = coins
            _market_cache["fetched_at"] = now
            return coins
    except Exception as e:
        print(f"  [crypto] market/coins fetch error: {e}")
    return _market_cache.get("coins") or []


def fetch_trending_topics() -> list:
    """
    Return trending keywords from /api/trending (free endpoint).
    Cached 15 minutes.
    """
    now = time.time()
    if (_trending_cache["keywords"] is not None
            and now - _trending_cache["fetched_at"] < TRENDING_TTL):
        return _trending_cache["keywords"]
    try:
        r = requests.get(f"{CRYPTO_CV_BASE}/api/trending", timeout=8)
        if r.ok:
            data = r.json()
            keywords = [t.get("keyword") or t.get("topic") or str(t)
                        for t in (data.get("trending") or [])[:10]]
            _trending_cache["keywords"]   = keywords
            _trending_cache["fetched_at"] = now
            return keywords
    except Exception as e:
        print(f"  [crypto] trending fetch error: {e}")
    return _trending_cache.get("keywords") or []


def fetch_bars(symbol: str) -> pd.DataFrame:
    """Fetch last 6 hours of 1-min bars from DB for the given crypto symbol."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=6)
    db = Session()
    try:
        rows = (db.query(Bar)
                  .filter(Bar.symbol == symbol, Bar.ts >= cutoff)
                  .order_by(Bar.ts.asc())
                  .all())
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame([{
            "open":   float(r.open),
            "high":   float(r.high),
            "low":    float(r.low),
            "close":  float(r.close),
            "volume": float(r.volume),
        } for r in rows])
    finally:
        db.close()


def compute_indicators(df: pd.DataFrame) -> dict:
    if len(df) < 26:
        return {}

    close  = df["close"]
    high   = df["high"]
    low    = df["low"]
    volume = df["volume"]

    # EMAs
    ema9  = ta_trend.EMAIndicator(close, window=9).ema_indicator()
    ema21 = ta_trend.EMAIndicator(close, window=21).ema_indicator()
    ema50 = ta_trend.EMAIndicator(close, window=50).ema_indicator() if len(df) >= 50 else None

    # RSI-14
    rsi14 = ta_momentum.RSIIndicator(close, window=14).rsi()

    # MACD 12/26/9
    macd_ind  = ta_trend.MACD(close, window_fast=12, window_slow=26, window_sign=9)
    macd_val  = macd_ind.macd()
    macd_sig  = macd_ind.macd_signal()
    macd_hist = macd_ind.macd_diff()

    # ATR-14
    atr14 = ta_vol.AverageTrueRange(high, low, close, window=14).average_true_range()

    # Bollinger Bands %B (0=lower band, 1=upper band)
    bb      = ta_vol.BollingerBands(close, window=20, window_dev=2)
    bb_pct_b = bb.bollinger_pband()

    # VWAP (rolling over available bars)
    tp   = (high + low + close) / 3
    vwap = (tp * volume).cumsum() / volume.cumsum()

    def r2(series):
        v = series.iloc[-1]
        return round(float(v), 2) if pd.notna(v) else None

    curr_close  = r2(close)
    curr_vwap   = r2(vwap)
    curr_ema9   = r2(ema9)
    curr_ema21  = r2(ema21)
    curr_ema50  = r2(ema50) if ema50 is not None else None
    curr_rsi14  = r2(rsi14)
    curr_macd   = r2(macd_val)
    curr_msig   = r2(macd_sig)
    curr_mhist  = r2(macd_hist)
    curr_atr    = r2(atr14)
    curr_bb_b   = (round(float(bb_pct_b.iloc[-1]), 3)
                   if pd.notna(bb_pct_b.iloc[-1]) else None)

    vol_ratio = None
    if len(volume) >= 20:
        avg_vol  = float(volume.iloc[-20:-1].mean())
        curr_vol = float(volume.iloc[-1])
        vol_ratio = round(curr_vol / avg_vol, 2) if avg_vol > 0 else None

    above_vwap    = (curr_close > curr_vwap) if curr_close and curr_vwap else None
    macd_bull     = (curr_macd > curr_msig) if curr_macd is not None and curr_msig is not None else None
    trend_aligned = (
        curr_ema9 is not None and curr_ema21 is not None and curr_ema50 is not None
        and curr_ema9 > curr_ema21 > curr_ema50
    )

    return {
        "close":         curr_close,
        "vwap":          curr_vwap,
        "above_vwap":    above_vwap,
        "ema_9":         curr_ema9,
        "ema_21":        curr_ema21,
        "ema_50":        curr_ema50,
        "trend_aligned": trend_aligned,
        "rsi_14":        curr_rsi14,
        "macd":          curr_macd,
        "macd_signal":   curr_msig,
        "macd_hist":     curr_mhist,
        "macd_bull":     macd_bull,
        "atr_14":        curr_atr,
        "bb_pct_b":      curr_bb_b,
        "volume_ratio":  vol_ratio,
    }


def _signal_action(ind: dict, fear_greed_val: int) -> str:
    """
    Technical entry/exit decision.
    Entry: trend aligned + above VWAP + vol≥1.2 + MACD bull + RSI 35-68 + BB%B 0.4-0.9 + FG≥30
    Exit:  RSI>75 OR below VWAP OR EMA9<EMA21 OR MACD bearish OR BB%B>0.95 OR FG<20
    """
    rsi  = ind.get("rsi_14")
    vol  = ind.get("volume_ratio")
    bb_b = ind.get("bb_pct_b")

    exit_conds = any([
        rsi is not None and rsi > 75,
        not ind.get("above_vwap"),
        (ind.get("ema_9") is not None and ind.get("ema_21") is not None
         and ind["ema_9"] < ind["ema_21"]),
        not ind.get("macd_bull"),
        bb_b is not None and bb_b > 0.95,
        fear_greed_val < 20,
    ])
    if exit_conds:
        return "sell"

    entry_conds = all([
        ind.get("trend_aligned"),
        ind.get("above_vwap"),
        vol is not None and vol >= 1.2,
        ind.get("macd_bull"),
        rsi is not None and 35 <= rsi <= 68,
        bb_b is not None and 0.4 <= bb_b <= 0.9,
        fear_greed_val >= 30,
    ])
    return "buy" if entry_conds else "hold"


def call_llm(symbol: str, ind: dict, news: list,
             fear_greed_val: int, fear_greed_label: str,
             suggested_action: str,
             market_coins: list = None,
             trending: list = None) -> dict:
    news_block = ("\n".join(f"  • {h}" for h in news)
                  if news else "  No recent headlines available.")

    # Build macro market context from top coins
    macro_lines = []
    if market_coins:
        base = symbol.split("/")[0].lower()
        for c in market_coins[:10]:
            chg = c.get("price_change_percentage_24h")
            chg_str = f"{chg:+.1f}%" if isinstance(chg, (int, float)) else "n/a"
            vol = c.get("total_volume", 0)
            vol_str = f"${vol/1e9:.1f}B" if vol >= 1e9 else f"${vol/1e6:.0f}M"
            line = (f"  {c.get('symbol','').upper()}: ${c.get('current_price'):,.2f}"
                    f"  24h:{chg_str}  vol:{vol_str}")
            macro_lines.append(line)
            # Mark the coin being analyzed
            if c.get("symbol", "").lower() == base:
                macro_lines[-1] += "  ← THIS COIN"
    macro_block = "\n".join(macro_lines) if macro_lines else "  (unavailable)"

    trending_block = (", ".join(trending[:8]) if trending else "none")

    prompt = f"""You are a cryptocurrency trading signal engine for {symbol}.

Current indicators:
  Close:     ${ind.get('close')}
  VWAP:      ${ind.get('vwap')} (price {'ABOVE' if ind.get('above_vwap') else 'BELOW'} VWAP)
  EMA 9/21/50: {ind.get('ema_9')} / {ind.get('ema_21')} / {ind.get('ema_50')}
  Trend aligned (9>21>50): {ind.get('trend_aligned')}
  RSI-14:    {ind.get('rsi_14')} (entry zone: 35-68, overbought: >75)
  MACD hist: {ind.get('macd_hist')} ({'BULLISH' if ind.get('macd_bull') else 'BEARISH'})
  ATR-14:    {ind.get('atr_14')}
  BB %B:     {ind.get('bb_pct_b')} (0=lower band, 1=upper band; overbought: >0.95)
  Vol ratio: {ind.get('volume_ratio')}x (1.0=average)
  Fear & Greed: {fear_greed_val} ({fear_greed_label}) — scale 0-100; entry floor: 30, exit below: 20

Top 10 coins — macro context:
{macro_block}

Trending topics: {trending_block}

Recent news headlines for {symbol.split('/')[0]}:
{news_block}

Rules:
  - Crypto trades 24/7 — no market hours restriction
  - Use IOC time-in-force (DAY is NOT valid for crypto)
  - Suggested technical action: {suggested_action.upper()}

Respond ONLY with valid JSON, no markdown:
{{"action":"buy"|"sell"|"hold","confidence":0.0-1.0,"reasoning":"<concise reason ≤120 chars>"}}"""

    try:
        r = requests.post(LLM_API_URL, json={
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }, timeout=60)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"].strip()
        start = content.find("{")
        end   = content.rfind("}") + 1
        return json.loads(content[start:end])
    except Exception as e:
        print(f"  [crypto] LLM error for {symbol}: {e}")
        return {
            "action":     suggested_action,
            "confidence": 0.5,
            "reasoning":  f"LLM unavailable — technical signal: {suggested_action}",
        }


def run_analysis(symbol: str):
    if get_setting("crypto_enabled", "true") != "true":
        return

    df = fetch_bars(symbol)
    if len(df) < 26:
        print(f"  [{symbol}] Not enough bars yet ({len(df)})")
        return

    ind = compute_indicators(df)
    if not ind:
        return

    fg       = fetch_fear_greed()
    fg_val   = fg.get("value") or 50
    fg_label = fg.get("label") or "Neutral"

    news      = fetch_crypto_news(symbol)
    suggested = _signal_action(ind, fg_val)
    result    = call_llm(symbol, ind, news, fg_val, fg_label, suggested)

    action     = result.get("action", "hold")
    confidence = float(result.get("confidence", 0.5))
    reasoning  = str(result.get("reasoning", ""))[:240]

    db = Session()
    try:
        sig = Signal(
            symbol=symbol,
            action=action,
            confidence=confidence,
            indicators=json.dumps(ind),
            reasoning=reasoning,
        )
        db.add(sig)
        db.commit()
        print(f"  [{symbol}] Signal: {action.upper()} conf={confidence:.2f}  FG={fg_val}({fg_label})")
        if action != "hold":
            from trade.executor import execute_crypto_signal
            execute_crypto_signal(sig.id)
    finally:
        db.close()
