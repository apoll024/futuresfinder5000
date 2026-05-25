"""
Daily news digest — auto-ingest financial articles from RSS feeds and per-symbol
yfinance news into the knowledge base.

Scheduling:
  • CRYPTO feeds (CoinDesk, Decrypt, CryptoSlate, The Block, Bitcoin Magazine,
    CryptoPanic) — polled every 5 minutes, 24/7. Crypto never sleeps.
  • MARKET feeds (MarketWatch, CNBC, Motley Fool, Reuters, Yahoo Finance, Benzinga)
    — run at 7:00 AM and 12:00 PM ET only.
  • yfinance per-symbol news — runs alongside market feed schedule.

Deduplication: skips any article whose URL is already in the knowledge base.
"""
import os, sys, re, time, signal, calendar, threading
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).parent.parent))

import requests
import feedparser
from db.models import init_db, Session, KnowledgeItem, get_setting, set_setting

ET              = ZoneInfo("America/New_York")
MARKET_TIMES_ET = [dtime(7, 0), dtime(12, 0)]   # 7 AM and noon ET for market feeds
CRYPTO_INTERVAL = 300                             # 5 minutes between crypto feed polls
MAX_AGE_H       = 14                              # skip articles older than 14h
MAX_PER_FEED    = 15                              # max articles per RSS feed per run
MAX_PER_SYM     = 6                               # max yfinance articles per symbol
FETCH_DELAY     = 1.5                             # seconds between HTTP fetches

_shutdown = False


def _handle_exit(signum, frame):
    global _shutdown
    _shutdown = True
    print("[digest] Shutdown signal received")


signal.signal(signal.SIGTERM, _handle_exit)
signal.signal(signal.SIGINT,  _handle_exit)


# ── Feed registry ──────────────────────────────────────────────────────────────

CRYPTO_FEEDS = [
    {"name": "CoinDesk",        "url": "https://www.coindesk.com/arc/outboundfeeds/rss/",   "tags": "CRYPTO,NEWS"},
    {"name": "Decrypt",         "url": "https://decrypt.co/feed",                            "tags": "CRYPTO,NEWS"},
    {"name": "CryptoSlate",     "url": "https://cryptoslate.com/feed/",                      "tags": "CRYPTO,NEWS"},
    {"name": "The Block",       "url": "https://www.theblock.co/rss.xml",                    "tags": "CRYPTO,NEWS"},
    {"name": "Bitcoin Magazine", "url": "https://bitcoinmagazine.com/.rss/full/",             "tags": "CRYPTO,BTC,NEWS"},
    {"name": "CryptoPanic",     "url": "https://cryptopanic.com/news/rss/",                  "tags": "CRYPTO,NEWS,SENTIMENT"},
]

MARKET_FEEDS = [
    {"name": "MarketWatch Top Stories", "url": "https://feeds.marketwatch.com/marketwatch/topstories/", "tags": "MARKET,NEWS"},
    {"name": "CNBC Markets",            "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html", "tags": "MARKET,NEWS"},
    {"name": "The Motley Fool",         "url": "https://www.fool.com/feeds/index.aspx",                 "tags": "ANALYSIS,NEWS"},
    {"name": "Reuters Business",        "url": "https://feeds.reuters.com/reuters/businessNews",         "tags": "MARKET,NEWS"},
    {"name": "Yahoo Finance Top Stories","url": "https://finance.yahoo.com/rss/topfinstories",           "tags": "MARKET,NEWS"},
    {"name": "Benzinga News",           "url": "https://www.benzinga.com/feeds/news",                    "tags": "MARKET,NEWS"},
]

# Combined for backward compat (used by old single-run code paths)
RSS_FEEDS = CRYPTO_FEEDS + MARKET_FEEDS

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; FuturesFinder5000-digest/1.0; +https://github.com)"
}

# ── Agent 2: Sentiment scoring (keyword-based, no NLP deps needed) ─────────────
_BULL_WORDS = {
    "bullish", "rally", "surge", "soar", "gain", "gains", "breakout", "recovery",
    "upside", "upgrade", "buy", "outperform", "strong", "beat", "record", "high",
    "growth", "profit", "exceed", "rise", "rises", "rising", "positive", "optimistic",
    "bull", "moon", "pump", "rebound", "support", "bounce",
}
_BEAR_WORDS = {
    "bearish", "crash", "drop", "fall", "decline", "selloff", "sell-off", "slump",
    "plunge", "loss", "losses", "downgrade", "sell", "underperform", "weak", "miss",
    "low", "shrink", "contraction", "fear", "uncertainty", "negative", "pessimistic",
    "bear", "dump", "breakdown", "resistance", "correction", "recession",
}

def _sentiment_score(text: str) -> float:
    """
    Keyword-based sentiment. Returns -1.0 (fully bearish) to +1.0 (fully bullish).
    Returns 0.0 if no keywords found.
    """
    words = re.findall(r"[a-z]+", text.lower())
    bull  = sum(1 for w in words if w in _BULL_WORDS)
    bear  = sum(1 for w in words if w in _BEAR_WORDS)
    total = bull + bear
    if total == 0:
        return 0.0
    return round((bull - bear) / total, 3)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _fetch_text(url: str, max_chars: int = 3000) -> str:
    """Fetch a URL and strip HTML tags, returning plain text up to max_chars."""
    try:
        resp = requests.get(url, timeout=10, headers=_HEADERS)
        resp.raise_for_status()
        html = resp.text
        html = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', html, flags=re.I | re.S)
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'[ \t]{2,}', ' ', text)
        text = re.sub(r'\n{3,}', '\n\n', text).strip()
        return text[:max_chars]
    except Exception:
        return ""


def _url_in_kb(url: str) -> bool:
    if not url:
        return False
    db = Session()
    try:
        return db.query(KnowledgeItem).filter(KnowledgeItem.source_url == url).first() is not None
    finally:
        db.close()


def _save_to_kb(title: str, content: str, source_url: str, tags: str,
                sentiment: float = None) -> bool:
    if not content or not content.strip():
        return False
    db = Session()
    try:
        db.add(KnowledgeItem(
            title=title[:300],
            content=content,
            source_url=(source_url or "")[:600],
            tags=(tags or "")[:300],
            sentiment=sentiment,
        ))
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        print(f"  [digest] KB save error: {e}")
        return False
    finally:
        db.close()


def _is_recent(published_ts) -> bool:
    if not published_ts:
        return True
    try:
        ts    = float(calendar.timegm(published_ts)) if hasattr(published_ts, '__iter__') else float(published_ts)
        age_h = (datetime.now(ET).timestamp() - ts) / 3600
        return age_h <= MAX_AGE_H
    except Exception:
        return True


# ── Ingestion ──────────────────────────────────────────────────────────────────

def ingest_rss_feed(feed_cfg: dict) -> int:
    """Parse one RSS feed and save new articles to the KB. Returns count saved."""
    name  = feed_cfg["name"]
    url   = feed_cfg["url"]
    tags  = feed_cfg.get("tags", "NEWS")
    saved = 0
    try:
        parsed = feedparser.parse(url, request_headers=_HEADERS)
    except Exception as e:
        print(f"  [digest] RSS parse error for {name}: {e}")
        return 0

    for entry in parsed.entries[:MAX_PER_FEED]:
        if _shutdown:
            break

        article_url = entry.get("link", "")
        title       = entry.get("title", "")[:300]
        pub_ts      = entry.get("published_parsed")

        if not title or not article_url:
            continue
        if not _is_recent(pub_ts):
            continue
        if _url_in_kb(article_url):
            continue

        content = ""
        if entry.get("summary"):
            content = re.sub(r'<[^>]+>', ' ', entry.summary).strip()
        if len(content) < 200:
            time.sleep(FETCH_DELAY)
            content = _fetch_text(article_url)

        if not content:
            continue

        sentiment = _sentiment_score(title + " " + content[:2000])
        if _save_to_kb(title, content, article_url, tags, sentiment=sentiment):
            saved += 1
            print(f"  [digest]   + {title[:70]}… [sentiment={sentiment:+.2f}]")

    return saved


def ingest_yfinance_news(symbols: list) -> int:
    """Pull per-symbol news from yfinance and save new articles to the KB."""
    try:
        import yfinance as yf
    except ImportError:
        print("  [digest] yfinance not installed — skipping per-symbol news")
        return 0

    saved     = 0
    seen_urls: set = set()

    for sym in symbols:
        if _shutdown:
            break
        try:
            news = yf.Ticker(sym).news or []
            for item in news[:MAX_PER_SYM]:
                article_url = item.get("link", "")
                title       = item.get("title", "")[:300]
                pub_ts      = item.get("providerPublishTime")

                if not title or not article_url:
                    continue
                if article_url in seen_urls:
                    continue
                if not _is_recent(pub_ts):
                    continue
                if _url_in_kb(article_url):
                    continue

                seen_urls.add(article_url)
                time.sleep(FETCH_DELAY)
                content = _fetch_text(article_url)
                if not content:
                    content = title

                if _save_to_kb(title, content, article_url, sym):
                    saved += 1
                    print(f"  [digest]   + [{sym}] {title[:60]}…")
        except Exception as e:
            print(f"  [digest] yfinance error for {sym}: {e}")

    return saved


def run_crypto_feeds() -> int:
    """
    Poll all crypto RSS feeds. Called every CRYPTO_INTERVAL seconds.
    Fast — only fetches summaries, no full-page downloads for recent crypto feeds.
    """
    saved = 0
    for feed in CRYPTO_FEEDS:
        if _shutdown:
            break
        saved += ingest_rss_feed(feed)
    if saved:
        set_setting("crypto_digest_last_run",   datetime.now(ET).strftime("%Y-%m-%d %H:%M ET"))
        set_setting("crypto_digest_last_count", str(saved))
        print(f"[digest] Crypto poll: +{saved} new articles")
    return saved


def run_market_digest() -> int:
    """
    Full market digest run: market RSS feeds + per-symbol yfinance news.
    Runs at scheduled market feed times (7 AM, noon ET).
    """
    start = datetime.now(ET)
    total = 0
    print(f"[digest] Starting market digest at {start.strftime('%H:%M ET')} …")

    for feed in MARKET_FEEDS:
        if _shutdown:
            break
        total += ingest_rss_feed(feed)

    symbols_raw = get_setting("symbols", "TQQQ,SQQQ,UPRO,SPXU,SOXL,SOXS,QQQ,SPY")
    symbols     = [s.strip() for s in symbols_raw.split(",") if s.strip()][:12]
    total      += ingest_yfinance_news(symbols)

    elapsed = (datetime.now(ET) - start).total_seconds()
    set_setting("news_digest_last_run",   start.strftime("%Y-%m-%d %H:%M ET"))
    set_setting("news_digest_last_count", str(total))
    print(f"[digest] Market digest done — {total} new articles in {elapsed:.0f}s")
    return total


def run_digest() -> int:
    """Full combined digest (crypto + market). Retained for backward compat."""
    total  = run_crypto_feeds()
    total += run_market_digest()
    return total


# ── Scheduler ──────────────────────────────────────────────────────────────────

def _seconds_until_next_market_run() -> float:
    now  = datetime.now(ET)
    runs = [now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
            for t in MARKET_TIMES_ET]
    future = sorted(r for r in runs if r > now)
    if future:
        return (future[0] - now).total_seconds()
    next_7am = (now + timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0)
    return (next_7am - now).total_seconds()


def _crypto_loop():
    """Background thread: poll crypto feeds every CRYPTO_INTERVAL seconds."""
    print(f"[digest] Crypto feed loop started — polling every {CRYPTO_INTERVAL}s")
    while not _shutdown:
        try:
            run_crypto_feeds()
        except Exception as e:
            print(f"[digest] Crypto loop error: {e}")
        # Sleep in ticks so shutdown is responsive
        deadline = time.monotonic() + CRYPTO_INTERVAL
        while time.monotonic() < deadline and not _shutdown:
            time.sleep(1)


if __name__ == "__main__":
    init_db()
    print("[digest] News digest service started")
    print("[digest]   Crypto feeds: every 5 minutes, 24/7")
    print("[digest]   Market feeds: 7:00 AM and 12:00 PM ET")

    # Start continuous crypto polling in a background thread
    crypto_thread = threading.Thread(target=_crypto_loop, daemon=True)
    crypto_thread.start()

    # Run market digest immediately if we're in the morning window
    now_et = datetime.now(ET)
    if dtime(7, 0) <= now_et.time() <= dtime(13, 0):
        print("[digest] In morning window — running market digest now")
        run_market_digest()

    # Market feed scheduler (main thread)
    while not _shutdown:
        secs    = _seconds_until_next_market_run()
        next_dt = datetime.now(ET).timestamp() + secs
        print(f"[digest] Next market digest in {secs / 3600:.1f}h "
              f"({datetime.fromtimestamp(next_dt, ET).strftime('%H:%M ET')})")
        deadline = time.monotonic() + secs
        while time.monotonic() < deadline and not _shutdown:
            time.sleep(1)
        if not _shutdown:
            run_market_digest()

    print("[digest] Service stopped")

