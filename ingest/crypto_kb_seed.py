"""
One-time script to seed the FuturesFinder5000 Knowledge Base with all 146 crypto
guides from the cryptocurrency.cv open-source blog.

Run once (idempotent — skips articles already in the KB by source URL):
    python ingest/crypto_kb_seed.py

Or via Docker:
    docker compose run --rm crypto python ingest/crypto_kb_seed.py
"""
import re, sys, time, requests
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from db.models import init_db, Session, KnowledgeItem

GITHUB_API_URL  = "https://api.github.com/repos/nirholas/cryptocurrency.cv/contents/content/blog"
RAW_BASE        = "https://raw.githubusercontent.com/nirholas/cryptocurrency.cv/main/content/blog"
REQUEST_DELAY   = 0.5   # seconds between GitHub raw fetches (be polite)

# Tag map: filename prefix → KB tags
_TAG_HINTS = {
    "bitcoin":         "BTC,bitcoin,crypto",
    "ethereum":        "ETH,ethereum,crypto",
    "solana":          "SOL,solana,crypto",
    "defi":            "DeFi,crypto",
    "crypto-trading":  "trading,strategy,crypto",
    "crypto-market":   "market,analysis,crypto",
    "crypto-futures":  "futures,derivatives,crypto",
    "crypto-deriv":    "derivatives,crypto",
    "crypto-sentiment":"sentiment,analysis,crypto",
    "crypto-fear":     "fear-greed,sentiment,crypto",
    "stop-loss":       "risk,trading,strategy",
    "dollar-cost":     "DCA,strategy,crypto",
    "how-to-short":    "shorting,trading,strategy",
    "yield-farming":   "DeFi,yield,strategy",
    "on-chain":        "on-chain,analysis,crypto",
    "altcoin":         "altcoin,market,crypto",
    "ai-agent":        "AI,trading,automation",
    "llm-":            "AI,LLM,crypto",
    "rag-":            "AI,RAG,crypto",
    "impermanent":     "DeFi,risk,liquidity",
    "crypto-tax":      "tax,crypto",
    "crypto-scam":     "security,scam,crypto",
    "rug-pull":        "security,scam,DeFi",
    "hardware-wallet": "security,wallet,crypto",
    "cold-":           "security,wallet,crypto",
    "metamask":        "wallet,crypto",
    "meme-coin":       "memecoin,trading,crypto",
}

_FRONTMATTER_TITLE = re.compile(r'^title:\s*["\']?(.+?)["\']?\s*$', re.MULTILINE)
_FRONTMATTER_TAGS  = re.compile(r'^tags:\s*\[(.+?)\]', re.MULTILINE | re.DOTALL)
_FRONTMATTER_CAT   = re.compile(r'^category:\s*(\S+)', re.MULTILINE)


def _guess_tags(filename: str) -> str:
    fn = filename.lower().replace("_", "-")
    for prefix, tags in _TAG_HINTS.items():
        if prefix in fn:
            return tags
    return "crypto,education"


def _parse_frontmatter(raw: str, filename: str) -> tuple:
    """Returns (title, content_without_frontmatter, tags_str)."""
    content = raw
    title   = filename.replace("-", " ").replace(".md", "").title()
    tags    = _guess_tags(filename)

    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            fm      = parts[1]
            content = parts[2].strip()

            m = _FRONTMATTER_TITLE.search(fm)
            if m:
                title = m.group(1).strip('"').strip("'")

            m = _FRONTMATTER_TAGS.search(fm)
            if m:
                extracted = re.sub(r'[\[\]"\'\s]', '', m.group(1))
                if extracted:
                    tags = extracted

            m = _FRONTMATTER_CAT.search(fm)
            if m and not any(t in tags for t in ("trading", "DeFi", "AI")):
                tags = f"{tags},{m.group(1)}"

    return title, content, tags


def _already_seeded(session, source_url: str) -> bool:
    return session.query(KnowledgeItem).filter(
        KnowledgeItem.source_url == source_url
    ).first() is not None


def seed():
    init_db()
    db = Session()

    print("[kb-seed] Fetching blog file list from GitHub…")
    try:
        resp = requests.get(GITHUB_API_URL, timeout=15,
                            headers={"User-Agent": "FF5000-kb-seed/1.0"})
        resp.raise_for_status()
        files = resp.json()
    except Exception as e:
        print(f"[kb-seed] ERROR fetching file list: {e}")
        db.close()
        return

    md_files = [f for f in files if f["name"].endswith(".md")]
    print(f"[kb-seed] Found {len(md_files)} blog files")

    inserted = skipped = errors = 0

    for entry in md_files:
        filename   = entry["name"]
        source_url = f"{RAW_BASE}/{filename}"

        if _already_seeded(db, source_url):
            skipped += 1
            continue

        try:
            time.sleep(REQUEST_DELAY)
            r = requests.get(source_url, timeout=15,
                             headers={"User-Agent": "FF5000-kb-seed/1.0"})
            r.raise_for_status()
            raw = r.text
        except Exception as e:
            print(f"  [kb-seed] SKIP {filename}: fetch error: {e}")
            errors += 1
            continue

        title, content, tags = _parse_frontmatter(raw, filename)

        # Truncate very large articles to fit reasonable DB size
        content = content[:12000]

        item = KnowledgeItem(
            title=title[:300],
            content=content,
            source_url=source_url[:600],
            tags=tags[:300],
        )
        db.add(item)
        db.commit()
        inserted += 1
        print(f"  [kb-seed] ✓ {title[:70]}")

    db.close()
    print(f"\n[kb-seed] Done — inserted: {inserted}  skipped (dup): {skipped}  errors: {errors}")


if __name__ == "__main__":
    seed()
