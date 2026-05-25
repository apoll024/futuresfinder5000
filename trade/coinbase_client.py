"""
Coinbase Advanced Trade API client.

Handles JWT-based authentication (ES256) and order execution for crypto trading.
Coinbase is the primary execution venue for crypto; Alpaca is the fallback.

Required environment variables:
    COINBASE_API_KEY_NAME    — organizations/<org>/apiKeys/<key-id>
    COINBASE_API_PRIVATE_KEY — EC private key PEM; use \\n for newlines in .env files
"""
import os, time, secrets, re as _re
import requests
import jwt  # PyJWT with cryptography backend (PyJWT[crypto])

CB_KEY_NAME = os.getenv("COINBASE_API_KEY_NAME", "")


def _rebuild_b64_body(body: str) -> str:
    """Remove 'n' artifacts at 64-char line boundaries.

    When a PEM key is stored in a .env with \\n notation, some env-var
    pipelines drop the backslash, leaving bare 'n' chars where newlines
    should be.  Because 'n' is a valid base64 character, regex stripping
    won't catch it — we must remove it specifically at 64-char boundaries.
    """
    clean = []
    i = 0
    while i < len(body):
        chunk = body[i:i + 64]
        clean.append(chunk)
        i += 64
        if i < len(body) and body[i] == "n":
            i += 1  # skip newline-artifact 'n'
    return "".join(clean)


def _sanitize_pem(raw: str) -> str:
    """Normalize a PEM private key string.

    Handles common corruption from env-var copy-paste:
    - Restores real newlines from literal \\n sequences
    - Strips carriage returns (Windows CRLF)
    - Fixes bare 'n' used as newline substitute (backslash dropped in env pipeline)
    - Rebuilds proper 64-char-per-line PEM body
    """
    if not raw:
        return raw

    # Convert literal \n (backslash + n) to actual newlines
    pem = raw.replace("\\n", "\n").replace("\r", "")

    # If still no newlines, bare 'n' chars may have replaced them
    if "\n" not in pem and "-----BEGIN" in pem and "-----END" in pem:
        pem = _re.sub(r"(-----[A-Z ]+-----)\s*n\s*", r"\1\n", pem)
        pem = _re.sub(r"\s*n\s*(-----[A-Z ]+-----)", r"\n\1", pem)

    # Extract header / body / footer and rebuild with correct line wrapping
    m = _re.search(r"(-----BEGIN[^-]*-----)\s*(.*?)\s*(-----END[^-]*-----)",
                   pem, _re.DOTALL)
    if not m:
        return pem

    header, body_raw, footer = m.group(1), m.group(2), m.group(3)
    body = body_raw.replace("\n", "").replace("\r", "").replace(" ", "")
    body = _rebuild_b64_body(body)
    wrapped = "\n".join(body[i:i + 64] for i in range(0, len(body), 64))
    return f"{header}\n{wrapped}\n{footer}\n"


def _get_private_key() -> str:
    """Lazy-read private key so container restarts aren't needed after .env edits."""
    return _sanitize_pem(os.getenv("COINBASE_API_PRIVATE_KEY", ""))


CB_PRIVATE_KEY = _sanitize_pem(os.getenv("COINBASE_API_PRIVATE_KEY", ""))
CB_BASE        = "https://api.coinbase.com"


def is_configured() -> bool:
    return bool(CB_KEY_NAME and CB_PRIVATE_KEY)


def coinbase_symbol(alpaca_symbol: str) -> str:
    """Convert Alpaca/stream format (BTC/USD) → Coinbase product ID (BTC-USD)."""
    return alpaca_symbol.replace("/", "-")


def _build_jwt(method: str, path: str) -> str:
    now = int(time.time())
    payload = {
        "sub": CB_KEY_NAME,
        "iss": "coinbase-cloud",
        "nbf": now,
        "exp": now + 120,
        "aud": ["retail_rest_api_proxy"],
        "uri": f"{method.upper()} api.coinbase.com{path}",
    }
    return jwt.encode(
        payload,
        _get_private_key(),
        algorithm="ES256",
        headers={"kid": CB_KEY_NAME, "nonce": secrets.token_hex(10)},
    )


def _req(method: str, path: str, body: dict = None) -> dict:
    token = _build_jwt(method, path)
    r = requests.request(
        method,
        f"{CB_BASE}{path}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=body,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def place_market_buy(product_id: str, quote_usd: float) -> dict:
    """Buy crypto using a USD notional (quote_size). Returns the order response."""
    return _req("POST", "/api/v3/brokerage/orders", {
        "client_order_id": secrets.token_hex(16),
        "product_id": product_id,
        "side": "BUY",
        "order_configuration": {
            "market_market_ioc": {"quote_size": f"{quote_usd:.2f}"},
        },
    })


def place_market_sell(product_id: str, base_size: float) -> dict:
    """Sell a base-currency amount (e.g. 0.001 BTC). Returns the order response."""
    return _req("POST", "/api/v3/brokerage/orders", {
        "client_order_id": secrets.token_hex(16),
        "product_id": product_id,
        "side": "SELL",
        "order_configuration": {
            "market_market_ioc": {"base_size": f"{base_size:.8f}"},
        },
    })


def get_crypto_balance(currency: str) -> float:
    """Return available balance for a currency (e.g. 'BTC'). Returns 0.0 on error."""
    try:
        data = _req("GET", "/api/v3/brokerage/accounts")
        for acct in data.get("accounts", []):
            if acct.get("currency", "").upper() == currency.upper():
                return float(acct.get("available_balance", {}).get("value", "0"))
        return 0.0
    except Exception as e:
        print(f"  [coinbase] balance fetch error for {currency}: {e}")
        return 0.0


def get_spot_price(currency: str) -> float | None:
    """Return current USD spot price for a Coinbase currency, or None if unavailable."""
    try:
        if currency.upper() in ("USD", "USDC", "USDT", "DAI"):
            return 1.0
        data = _req("GET", f"/api/v3/brokerage/products/{currency.upper()}-USD/ticker")
        trades = data.get("trades") or []
        if trades:
            return float(trades[0].get("price") or 0)
        if data.get("price"):
            return float(data["price"])
    except Exception as e:
        print(f"  [coinbase] spot fetch error for {currency}: {e}")
    return None


def get_portfolio_summary() -> dict:
    """
    Return a summary of all non-zero Coinbase balances.
    Used by the dashboard wallet panel.
    """
    if not is_configured():
        return {"configured": False}
    try:
        data    = _req("GET", "/api/v3/brokerage/accounts")
        balances = []
        for acct in data.get("accounts", []):
            available = float(acct.get("available_balance", {}).get("value", "0") or 0)
            hold = float(acct.get("hold", {}).get("value", "0") or 0)
            total = available + hold
            if total > 0:
                balances.append({
                    "currency": acct.get("currency", ""),
                    "balance":  total,
                    "available_balance": available,
                    "hold_balance": hold,
                })
        return {"configured": True, "balances": balances, "broker": "Coinbase Advanced Trade"}
    except Exception as e:
        return {"configured": True, "error": str(e), "balances": []}
