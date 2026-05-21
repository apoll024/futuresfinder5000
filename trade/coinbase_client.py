"""
Coinbase Advanced Trade API client.

Handles JWT-based authentication (ES256) and order execution for crypto trading.
Coinbase is the primary execution venue for crypto; Alpaca is the fallback.

Required environment variables:
    COINBASE_API_KEY_NAME    — organizations/<org>/apiKeys/<key-id>
    COINBASE_API_PRIVATE_KEY — EC private key PEM; use \\n for newlines in .env files
"""
import os, time, secrets
import requests
import jwt  # PyJWT with cryptography backend (PyJWT[crypto])

CB_KEY_NAME    = os.getenv("COINBASE_API_KEY_NAME", "")
# .env stores the PEM with literal \n — restore real newlines at load time
CB_PRIVATE_KEY = os.getenv("COINBASE_API_PRIVATE_KEY", "").replace("\\n", "\n")
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
        CB_PRIVATE_KEY,
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
            val = float(acct.get("available_balance", {}).get("value", "0") or 0)
            if val > 0:
                balances.append({
                    "currency": acct.get("currency", ""),
                    "balance":  val,
                })
        return {"configured": True, "balances": balances, "broker": "Coinbase Advanced Trade"}
    except Exception as e:
        return {"configured": True, "error": str(e), "balances": []}
