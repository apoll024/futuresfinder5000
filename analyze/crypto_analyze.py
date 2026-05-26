"""
Crypto signal analysis engine for FuturesFinder5000.

Indicators : EMA9, EMA21, EMA50, RSI14, MACD 12/26/9, ATR14, Bollinger %B, VWAP
             Computed on THREE timeframes: 1m (entry), 5m (confirm), 1h (bias)

Data sources (all free, no API key required):
  Fear & Greed    : Alternative.me        https://api.alternative.me/fng/
  News headlines  : Messari free API      https://data.messari.io/api/v1/news/{asset}
  Market snapshot : CoinGecko free        https://api.coingecko.com/api/v3/coins/markets
  Trending coins  : CoinGecko free        https://api.coingecko.com/api/v3/search/trending
  Global context  : CoinGecko free        https://api.coingecko.com/api/v3/global
  Funding rates   : Binance Futures free  https://fapi.binance.com/fapi/v1/fundingRate
  Open interest   : Binance Futures free  https://fapi.binance.com/fapi/v1/openInterest

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
from db.models import Session, Bar, Signal, get_setting, log_llm_session
from analyze.ai_context import SYSTEM_INSTRUCTIONS, build_db_context
from ingest.crypto_derivatives import fetch_and_store, latest_derivatives

LLM_API_URL    = os.getenv("LLM_API_URL", "https://models.inference.ai.azure.com/chat/completions")
LLM_MODEL      = os.getenv("LLM_MODEL",   "gpt-4o")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GITHUB_TOKEN   = os.getenv("GITHUB_TOKEN", "")

# Optional CoinGecko Demo API key — raises rate limit from 30 to 500 req/min
# Get free key at https://www.coingecko.com/en/api
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY", "")

COINGECKO_BASE  = "https://api.coingecko.com/api/v3"
MESSARI_BASE    = "https://data.messari.io/api/v1"
FEAR_GREED_BASE = "https://api.alternative.me"
BINANCE_FUTURES = "https://fapi.binance.com/fapi/v1"

# Symbol map: our pair format → Binance perpetual symbol
_BINANCE_SYM = {
    "BTC/USD": "BTCUSDT",
    "ETH/USD": "ETHUSDT",
    "SOL/USD": "SOLUSDT",
    "BNB/USD": "BNBUSDT",
    "XRP/USD": "XRPUSDT",
    "ADA/USD": "ADAUSDT",
    "DOGE/USD": "DOGEUSDT",
    "AVAX/USD": "AVAXUSDT",
    "MATIC/USD": "MATICUSDT",
    "LINK/USD": "LINKUSDT",
}


def _llm_headers() -> dict:
    h = {"Content-Type": "application/json"}
    token = GITHUB_TOKEN or GEMINI_API_KEY
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _coingecko_headers() -> dict:
    h = {"accept": "application/json"}
    if COINGECKO_API_KEY:
        h["x-cg-demo-api-key"] = COINGECKO_API_KEY
    return h


# ── Caches ────────────────────────────────────────────────────────────────────

_fear_greed_cache: dict = {"value": None, "label": None, "fetched_at": 0.0}
FEAR_GREED_TTL = 3600      # 1 hour — index updates daily

_market_cache: dict = {"coins": None, "fetched_at": 0.0}
MARKET_TTL = 300           # 5 minutes

_trending_cache: dict = {"keywords": None, "fetched_at": 0.0}
TRENDING_TTL = 900         # 15 minutes

_global_cache: dict = {"data": None, "fetched_at": 0.0}
GLOBAL_TTL = 900           # 15 minutes

_funding_cache: dict = {}  # keyed by binance symbol, value: {rate, direction, fetched_at}
FUNDING_TTL = 600          # 10 minutes

_oi_cache: dict = {}       # keyed by binance symbol
OI_TTL = 600               # 10 minutes


# ── External data fetchers ────────────────────────────────────────────────────

def fetch_fear_greed() -> dict:
    """Crypto Fear & Greed Index from Alternative.me (free, no key). Cached 1h."""
    now = time.time()
    if (_fear_greed_cache["value"] is not None
            and now - _fear_greed_cache["fetched_at"] < FEAR_GREED_TTL):
        return _fear_greed_cache
    try:
        r = requests.get(f"{FEAR_GREED_BASE}/fng/?limit=1", timeout=8)
        if r.ok:
            entry = r.json().get("data", [{}])[0]
            _fear_greed_cache["value"]      = int(entry.get("value", 50))
            _fear_greed_cache["label"]      = str(entry.get("value_classification", "Neutral"))
            _fear_greed_cache["fetched_at"] = now
    except Exception as e:
        print(f"  [crypto] fear-greed fetch error: {e}")
    return _fear_greed_cache


def fetch_crypto_news(ticker: str) -> list:
    """Up to 5 recent headlines from Messari free API (no key required)."""
    base = ticker.split("/")[0].lower()
    try:
        r = requests.get(f"{MESSARI_BASE}/news/{base}", params={"limit": 5}, timeout=8)
        if r.ok:
            return [a.get("title", "") for a in r.json().get("data", []) if a.get("title")]
    except Exception as e:
        print(f"  [crypto] news fetch error for {ticker}: {e}")
    return []


def fetch_market_context() -> list:
    """Top 20 coins from CoinGecko /coins/markets. CoinGecko field names used as-is. Cached 5m."""
    now = time.time()
    if _market_cache["coins"] is not None and now - _market_cache["fetched_at"] < MARKET_TTL:
        return _market_cache["coins"]
    try:
        r = requests.get(
            f"{COINGECKO_BASE}/coins/markets",
            headers=_coingecko_headers(),
            params={"vs_currency": "usd", "order": "market_cap_desc",
                    "per_page": 20, "page": 1, "price_change_percentage": "24h"},
            timeout=10,
        )
        if r.ok:
            _market_cache["coins"]      = r.json()
            _market_cache["fetched_at"] = now
            return _market_cache["coins"]
    except Exception as e:
        print(f"  [crypto] market context error: {e}")
    return _market_cache.get("coins") or []


def fetch_trending_topics() -> list:
    """Trending coin names from CoinGecko /search/trending. Cached 15m."""
    now = time.time()
    if _trending_cache["keywords"] is not None and now - _trending_cache["fetched_at"] < TRENDING_TTL:
        return _trending_cache["keywords"]
    try:
        r = requests.get(f"{COINGECKO_BASE}/search/trending",
                         headers=_coingecko_headers(), timeout=8)
        if r.ok:
            keywords = [
                f"{c['item']['symbol'].upper()} ({c['item']['name']})"
                for c in r.json().get("coins", [])[:10]
                if c.get("item")
            ]
            _trending_cache["keywords"]   = keywords
            _trending_cache["fetched_at"] = now
            return keywords
    except Exception as e:
        print(f"  [crypto] trending fetch error: {e}")
    return _trending_cache.get("keywords") or []


def fetch_global_context() -> dict:
    """
    CoinGecko /global — BTC dominance %, total market cap, 24h change.
    Gives the AI a macro read: is crypto overall rising or falling?
    Cached 15m.
    """
    now = time.time()
    if _global_cache["data"] is not None and now - _global_cache["fetched_at"] < GLOBAL_TTL:
        return _global_cache["data"]
    try:
        r = requests.get(f"{COINGECKO_BASE}/global",
                         headers=_coingecko_headers(), timeout=8)
        if r.ok:
            raw  = r.json().get("data", {})
            data = {
                "btc_dominance":      round(raw.get("market_cap_percentage", {}).get("btc", 0), 1),
                "eth_dominance":      round(raw.get("market_cap_percentage", {}).get("eth", 0), 1),
                "total_market_cap_b": round((raw.get("total_market_cap", {}).get("usd", 0) or 0) / 1e9, 1),
                "market_cap_24h_pct": round(raw.get("market_cap_change_percentage_24h_usd", 0), 2),
                "active_coins":       raw.get("active_cryptocurrencies", 0),
            }
            _global_cache["data"]       = data
            _global_cache["fetched_at"] = now
            return data
    except Exception as e:
        print(f"  [crypto] global context error: {e}")
    return _global_cache.get("data") or {}


def fetch_funding_rate(symbol: str) -> dict:
    """
    Binance perpetual funding rate for a symbol (free, no key).
    Positive funding = longs pay shorts = market over-leveraged long → reversal risk.
    Negative funding = shorts pay longs = bearish crowd → potential squeeze.
    Cached 10m per symbol.
    """
    bsym = _BINANCE_SYM.get(symbol)
    if not bsym:
        return {}
    now   = time.time()
    cache = _funding_cache.get(bsym, {})
    if cache.get("fetched_at", 0) and now - cache["fetched_at"] < FUNDING_TTL:
        return cache
    try:
        r = requests.get(f"{BINANCE_FUTURES}/fundingRate",
                         params={"symbol": bsym, "limit": 3}, timeout=8)
        if r.ok:
            entries = r.json()
            if entries:
                latest  = float(entries[-1].get("fundingRate", 0))
                prev    = float(entries[-2].get("fundingRate", 0)) if len(entries) > 1 else latest
                trend   = "rising" if latest > prev else "falling" if latest < prev else "flat"
                result  = {
                    "rate":       round(latest * 100, 4),   # as % e.g. 0.0100 → 1.0%
                    "trend":      trend,
                    "signal":     ("crowded_long"  if latest >  0.0005 else
                                   "crowded_short" if latest < -0.0005 else "neutral"),
                    "fetched_at": now,
                }
                _funding_cache[bsym] = result
                return result
    except Exception as e:
        print(f"  [crypto] funding rate error for {symbol}: {e}")
    return {}


def fetch_open_interest(symbol: str) -> dict:
    """
    Binance perpetual open interest (free, no key).
    Rising OI + rising price = new money entering = strong trend.
    Falling OI + rising price = short covering = weaker, may fade.
    Cached 10m per symbol.
    """
    bsym = _BINANCE_SYM.get(symbol)
    if not bsym:
        return {}
    now   = time.time()
    cache = _oi_cache.get(bsym, {})
    if cache.get("fetched_at", 0) and now - cache["fetched_at"] < OI_TTL:
        return cache
    try:
        r = requests.get(f"{BINANCE_FUTURES}/openInterest",
                         params={"symbol": bsym}, timeout=8)
        if r.ok:
            data = r.json()
            result = {
                "open_interest":  float(data.get("openInterest", 0)),
                "notional_b":     round(float(data.get("openInterest", 0))
                                        * float(data.get("time", 1)) / 1e9, 2)
                                  if data.get("time") else None,
                "fetched_at":     now,
            }
            # Also fetch OI history for trend (last 2 entries at 5m resolution)
            rh = requests.get(f"{BINANCE_FUTURES}/openInterestHist",
                               params={"symbol": bsym, "period": "5m", "limit": 3},
                               timeout=8)
            if rh.ok:
                hist = rh.json()
                if len(hist) >= 2:
                    oi_now  = float(hist[-1].get("sumOpenInterest", 0))
                    oi_prev = float(hist[-2].get("sumOpenInterest", 0))
                    result["oi_trend"] = ("rising" if oi_now > oi_prev * 1.002
                                          else "falling" if oi_now < oi_prev * 0.998
                                          else "flat")
            _oi_cache[bsym] = result
            return result
    except Exception as e:
        print(f"  [crypto] open interest error for {symbol}: {e}")
    return {}


def fetch_bars(symbol: str, hours: int = 24) -> pd.DataFrame:
    """
    Fetch last N hours of 1-min bars from DB with timestamps.
    Returns DataFrame with columns: ts, open, high, low, close, volume.
    Wider window (24h) needed so we can aggregate into 5m and 1h timeframes.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    db = Session()
    try:
        rows = (db.query(Bar)
                  .filter(Bar.symbol == symbol, Bar.ts >= cutoff)
                  .order_by(Bar.ts.asc())
                  .all())
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame([{
            "ts":     r.ts,
            "open":   float(r.open),
            "high":   float(r.high),
            "low":    float(r.low),
            "close":  float(r.close),
            "volume": float(r.volume),
        } for r in rows])
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        df = df.set_index("ts")
        return df
    finally:
        db.close()


def _resample_ohlcv(df_1m: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample 1m bars to a higher timeframe (e.g. '5min', '1h')."""
    agg = df_1m.resample(rule).agg({
        "open":   "first",
        "high":   "max",
        "low":    "min",
        "close":  "last",
        "volume": "sum",
    }).dropna(subset=["close"])
    return agg


def _compute_tf_snapshot(df: pd.DataFrame, label: str) -> dict:
    """
    Compute indicator snapshot for one timeframe dataframe.
    Returns dict of scalars tagged with the timeframe label.
    """
    if len(df) < 26:
        return {}

    close  = df["close"]
    high   = df["high"]
    low    = df["low"]
    volume = df["volume"]

    ema9  = ta_trend.EMAIndicator(close, window=9).ema_indicator()
    ema21 = ta_trend.EMAIndicator(close, window=21).ema_indicator()
    ema50 = ta_trend.EMAIndicator(close, window=50).ema_indicator() if len(df) >= 50 else None
    rsi14 = ta_momentum.RSIIndicator(close, window=14).rsi()
    macd_ind  = ta_trend.MACD(close, window_fast=12, window_slow=26, window_sign=9)
    macd_val  = macd_ind.macd()
    macd_sig  = macd_ind.macd_signal()
    macd_hist = macd_ind.macd_diff()
    atr14     = ta_vol.AverageTrueRange(high, low, close, window=14).average_true_range()
    bb        = ta_vol.BollingerBands(close, window=20, window_dev=2)
    bb_pct_b  = bb.bollinger_pband()
    tp        = (high + low + close) / 3
    vwap      = (tp * volume).cumsum() / volume.cumsum()

    def r2(series):
        v = series.iloc[-1] if hasattr(series, "iloc") else series
        return round(float(v), 4) if pd.notna(v) else None

    c    = r2(close)
    v    = r2(vwap)
    e9   = r2(ema9)
    e21  = r2(ema21)
    e50  = r2(ema50) if ema50 is not None else None
    rsi  = r2(rsi14)
    mval = r2(macd_val)
    msig = r2(macd_sig)
    mhst = r2(macd_hist)
    atr  = r2(atr14)
    bb_b = round(float(bb_pct_b.iloc[-1]), 3) if pd.notna(bb_pct_b.iloc[-1]) else None

    vol_ratio = None
    if len(volume) >= 20:
        avg = float(volume.iloc[-20:-1].mean())
        vol_ratio = round(float(volume.iloc[-1]) / avg, 2) if avg > 0 else None

    above_vwap    = (c > v) if c and v else None
    macd_bull     = (mval > msig) if mval is not None and msig is not None else None
    trend_aligned = (e9 is not None and e21 is not None and e50 is not None
                     and e9 > e21 > e50)

    return {
        f"{label}_close":         c,
        f"{label}_vwap":          v,
        f"{label}_above_vwap":    above_vwap,
        f"{label}_ema_9":         e9,
        f"{label}_ema_21":        e21,
        f"{label}_ema_50":        e50,
        f"{label}_trend_aligned": trend_aligned,
        f"{label}_rsi_14":        rsi,
        f"{label}_macd":          mval,
        f"{label}_macd_signal":   msig,
        f"{label}_macd_hist":     mhst,
        f"{label}_macd_bull":     macd_bull,
        f"{label}_atr_14":        atr,
        f"{label}_bb_pct_b":      bb_b,
        f"{label}_volume_ratio":  vol_ratio,
    }


def compute_indicators(df_1m: pd.DataFrame) -> dict:
    """
    Compute indicators on three timeframes: 1m (entry), 5m (confirmation), 1h (bias).
    All derived from the same 1m bar data — no extra DB queries.
    Returns flat dict with prefixed keys: 1m_close, 5m_rsi_14, 1h_trend_aligned, etc.
    Also sets top-level convenience aliases (close, rsi_14, etc.) from 1m for backward compat.
    """
    snap_1m = _compute_tf_snapshot(df_1m, "1m")
    if not snap_1m:
        return {}

    df_5m = _resample_ohlcv(df_1m, "5min")
    df_1h = _resample_ohlcv(df_1m, "1h")

    snap_5m = _compute_tf_snapshot(df_5m, "5m") if len(df_5m) >= 26 else {}
    snap_1h = _compute_tf_snapshot(df_1h, "1h") if len(df_1h) >= 26 else {}

    result = {**snap_1m, **snap_5m, **snap_1h}

    # Backward-compat aliases (used by _signal_action and older code)
    for key in ("close", "vwap", "above_vwap", "ema_9", "ema_21", "ema_50",
                "trend_aligned", "rsi_14", "macd", "macd_signal", "macd_hist",
                "macd_bull", "atr_14", "bb_pct_b", "volume_ratio"):
        result[key] = snap_1m.get(f"1m_{key}")

    return result


def _signal_action(ind: dict, fear_greed_val: int) -> str:
    """
    Lightweight technical bias for the LLM.
    This is deliberately advisory: the model can override it after weighing the
    broader context, risk/reward, positioning, news, and available capital.
    """
    rsi_1m  = ind.get("1m_rsi_14") or ind.get("rsi_14")
    rsi_5m  = ind.get("5m_rsi_14")
    vol     = ind.get("1m_volume_ratio") or ind.get("volume_ratio")
    bb_b    = ind.get("1m_bb_pct_b") or ind.get("bb_pct_b")

    bearish = sum(bool(x) for x in [
        rsi_1m is not None and rsi_1m > 75,
        not ind.get("1m_above_vwap"),
        (ind.get("1m_ema_9") is not None and ind.get("1m_ema_21") is not None
         and ind["1m_ema_9"] < ind["1m_ema_21"]),
        not ind.get("1m_macd_bull"),
        bb_b is not None and bb_b > 0.95,
        fear_greed_val < 20,
        (ind.get("1h_trend_aligned") is False and not ind.get("1h_above_vwap")),
    ])
    if bearish >= 3:
        return "sell"

    bullish = sum(bool(x) for x in [
        ind.get("1m_trend_aligned"),
        ind.get("1m_above_vwap"),
        (vol is None or vol >= 1.0),
        ind.get("1m_macd_bull"),
        rsi_1m is not None and 30 <= rsi_1m <= 72,
        bb_b is not None and 0.25 <= bb_b <= 0.95,
        fear_greed_val >= 30,
    ])
    conf_5m = (not ind.get("5m_trend_aligned") is False
               and not (rsi_5m is not None and rsi_5m > 75))
    bias_1h = ind.get("1h_above_vwap") or ind.get("1h_trend_aligned") or not snap_5m_present(ind)

    return "buy" if bullish >= 4 and conf_5m and bias_1h else "hold"


def snap_5m_present(ind: dict) -> bool:
    """True when 5m indicators were computed (enough bars)."""
    return ind.get("5m_rsi_14") is not None


def call_llm(symbol: str, ind: dict, news: list,
             fear_greed_val: int, fear_greed_label: str,
             suggested_action: str,
             market_coins: list = None,
             trending: list = None,
             global_ctx: dict = None,
             funding: dict = None,
             oi: dict = None) -> dict:

    news_block = ("\n".join(f"  • {h}" for h in news)
                  if news else "  No recent headlines available.")

    # Macro market context (top coins)
    macro_lines = []
    if market_coins:
        base = symbol.split("/")[0].lower()
        for c in market_coins[:10]:
            chg     = c.get("price_change_percentage_24h")
            chg_str = f"{chg:+.1f}%" if isinstance(chg, (int, float)) else "n/a"
            vol     = c.get("total_volume", 0)
            vol_str = f"${vol/1e9:.1f}B" if vol >= 1e9 else f"${vol/1e6:.0f}M"
            line    = (f"  {c.get('symbol','').upper()}: ${c.get('current_price'):,.2f}"
                       f"  24h:{chg_str}  vol:{vol_str}")
            if c.get("symbol", "").lower() == base:
                line += "  ← THIS COIN"
            macro_lines.append(line)
    macro_block = "\n".join(macro_lines) or "  (unavailable)"

    # Global market context
    if global_ctx:
        global_block = (
            f"  BTC dominance: {global_ctx.get('btc_dominance')}%  "
            f"ETH: {global_ctx.get('eth_dominance')}%  "
            f"Total mktcap: ${global_ctx.get('total_market_cap_b')}B  "
            f"24h change: {global_ctx.get('market_cap_24h_pct'):+.2f}%"
        )
    else:
        global_block = "  (unavailable)"

    # Derivatives context
    if funding:
        rate    = funding.get("rate", 0)
        fsignal = funding.get("signal", "neutral")
        fblock  = (f"  Funding rate: {rate:+.4f}%  trend:{funding.get('trend','?')}  "
                   f"signal:{fsignal}  "
                   f"({'longs overcrowded — reversal risk' if fsignal=='crowded_long' else 'shorts overcrowded — squeeze risk' if fsignal=='crowded_short' else 'balanced'})")
    else:
        fblock = "  (unavailable — not a Binance-mapped pair)"

    if oi:
        oiblock = (f"  Open interest trend: {oi.get('oi_trend','unknown')}  "
                   f"({'strong trend' if oi.get('oi_trend')=='rising' else 'fading move' if oi.get('oi_trend')=='falling' else 'neutral'})")
    else:
        oiblock = "  (unavailable)"

    trending_block = (", ".join(trending[:8]) if trending else "none")

    # Multi-timeframe indicator summary
    def tf_row(label, full):
        d = ind
        return (
            f"  [{full}] Close:{d.get(f'{label}_close')}  "
            f"RSI:{d.get(f'{label}_rsi_14')}  "
            f"MACD:{'▲' if d.get(f'{label}_macd_bull') else '▼'}  "
            f"Trend:{'✓' if d.get(f'{label}_trend_aligned') else '✗'}  "
            f"VWAP:{'above' if d.get(f'{label}_above_vwap') else 'below'}  "
            f"BB%B:{d.get(f'{label}_bb_pct_b')}  Vol:{d.get(f'{label}_volume_ratio')}x"
        )

    tf_block = tf_row("1m", "1-min entry") + "\n"
    if snap_5m_present(ind):
        tf_block += tf_row("5m", "5-min confirm") + "\n"
    if ind.get("1h_rsi_14") is not None:
        tf_block += tf_row("1h", "1-hour bias") + "\n"

    prompt = f"""{SYSTEM_INSTRUCTIONS}
{build_db_context(symbol, service="crypto")}
You are a cryptocurrency trading signal engine for {symbol}.

GLOBAL CRYPTO MARKET:
{global_block}

DERIVATIVES (crowd positioning):
{fblock}
{oiblock}

MULTI-TIMEFRAME INDICATORS:
{tf_block}
  ATR-14(1m): {ind.get('atr_14')} | Fear & Greed: {fear_greed_val} ({fear_greed_label}) [0=extreme fear, 100=extreme greed]

TOP 10 COINS — MACRO CONTEXT:
{macro_block}

TRENDING: {trending_block}

RECENT NEWS ({symbol.split('/')[0]}):
{news_block}

RULES:
  - Crypto trades 24/7. Use IOC time-in-force (DAY is NOT valid for crypto).
  - Your goal is profit with caution. Decide buy/sell/hold from the complete context.
  - Multi-timeframe indicators, funding, OI, news, macro context, and recent outcomes are evidence, not mandatory gates.
  - High funding can imply crowded risk; rising OI can imply trend strength. Weigh these rather than applying rigid rules.
  - Advisory technical bias: {suggested_action.upper()} — you may override it when the broader setup justifies it.

Respond ONLY with valid JSON, no markdown:
{{"action":"buy"|"sell"|"hold","confidence":0.0-1.0,"reasoning":"<concise reason ≤150 chars>"}}"""

    try:
        r = requests.post(LLM_API_URL, headers=_llm_headers(), json={
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }, timeout=60)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"].strip()
        start   = content.find("{")
        end     = content.rfind("}") + 1
        result  = json.loads(content[start:end])
        log_llm_session(service="crypto", model=LLM_MODEL, symbol=symbol,
                        prompt=prompt, response=content,
                        action=result.get("action"), confidence=result.get("confidence"))
        return result
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

    df_1m = fetch_bars(symbol, hours=56)  # 56h ensures 1h EMA-50 (needs 50+ 1h bars)
    if len(df_1m) < 26:
        print(f"  [{symbol}] Not enough bars yet ({len(df_1m)})")
        return

    ind = compute_indicators(df_1m)
    if not ind:
        return

    fg       = fetch_fear_greed()
    fg_val   = fg.get("value") or 50
    fg_label = fg.get("label") or "Neutral"

    news       = fetch_crypto_news(symbol)
    suggested  = _signal_action(ind, fg_val)
    market     = fetch_market_context()
    trending   = fetch_trending_topics()
    global_ctx = fetch_global_context()
    deriv = latest_derivatives(symbol)
    if not deriv:
        try:
            deriv = fetch_and_store(symbol)
        except Exception as e:
            print(f"  [crypto] derivatives feed unavailable for {symbol}: {e}")
            deriv = {}
    funding = (
        {
            "rate": deriv.get("funding_rate"),
            "trend": "n/a",
            "signal": (
                "crowded_long" if (deriv.get("funding_rate") or 0) > 0.05 else
                "crowded_short" if (deriv.get("funding_rate") or 0) < -0.05 else
                "neutral"
            ),
        }
        if deriv and deriv.get("mapped") is not False else fetch_funding_rate(symbol)
    )
    oi = (
        {
            "open_interest": deriv.get("open_interest"),
            "notional_b": round((deriv.get("open_interest_value") or 0) / 1e9, 2)
                          if deriv.get("open_interest_value") else None,
            "oi_trend": deriv.get("open_interest_trend"),
        }
        if deriv and deriv.get("mapped") is not False else fetch_open_interest(symbol)
    )

    result = call_llm(
        symbol, ind, news, fg_val, fg_label, suggested,
        market_coins=market,
        trending=trending,
        global_ctx=global_ctx,
        funding=funding,
        oi=oi,
    )

    action     = result.get("action", "hold")
    confidence = float(result.get("confidence", 0.5))
    reasoning  = str(result.get("reasoning", ""))[:240]

    # Store full multi-tf indicators snapshot
    db = Session()
    try:
        sig = Signal(
            symbol=symbol,
            action=action,
            confidence=confidence,
            indicators=json.dumps({**ind, "derivatives": deriv or {}}),
            reasoning=reasoning,
        )
        db.add(sig)
        db.commit()
        print(f"  [{symbol}] Signal: {action.upper()} conf={confidence:.2f}  "
              f"FG={fg_val}({fg_label})  "
              f"funding={funding.get('rate','n/a')}%  "
              f"OI={oi.get('oi_trend','n/a')}")
        if action != "hold":
            from trade.executor import execute_crypto_signal
            execute_crypto_signal(sig.id)
    finally:
        db.close()
