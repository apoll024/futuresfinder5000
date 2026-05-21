"""
FuturesFinder5000 — Ethereum hot wallet module

Private key is stored ONLY in the WALLET_PRIVATE_KEY environment variable.
The public address is derived at runtime and cached in the 'wallet_address' DB setting.

Supports:
  - ETH balance via free public Cloudflare RPC (no API key required)
  - ERC-20 token balances (USDC, USDT, WBTC, WETH, DAI)
  - Sepolia testnet for safe testing

Environment variables:
  WALLET_PRIVATE_KEY   — 0x-prefixed hex private key (NEVER commit to source)
  WALLET_NETWORK       — 'mainnet' (default) or 'sepolia'
  ETH_RPC_URL          — optional override for RPC endpoint
  ETHERSCAN_API_KEY    — optional, for future tx history
"""
import os, time, functools

try:
    from web3 import Web3
    from eth_account import Account
    _WEB3_OK = True
except ImportError:
    _WEB3_OK = False

# ── RPC endpoints (free, no API key) ────────────────────────────────────────
_RPC = {
    "mainnet": os.getenv("ETH_RPC_URL", "https://cloudflare-eth.com"),
    "sepolia": os.getenv("ETH_RPC_URL", "https://rpc.sepolia.org"),
}

NETWORK = os.getenv("WALLET_NETWORK", "mainnet")

# ── ERC-20 minimal ABI ───────────────────────────────────────────────────────
_ERC20_ABI = [
    {"constant": True,
     "inputs":  [{"name": "_owner", "type": "address"}],
     "name":    "balanceOf",
     "outputs": [{"name": "balance", "type": "uint256"}],
     "type":    "function"},
    {"constant": True,
     "inputs":  [],
     "name":    "decimals",
     "outputs": [{"name": "", "type": "uint8"}],
     "type":    "function"},
]

# ── Well-known token contracts ───────────────────────────────────────────────
_TOKENS = {
    "mainnet": {
        "USDC":  "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        "USDT":  "0xdAC17F958D2ee523a2206206994597C13D831ec7",
        "WETH":  "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
        "WBTC":  "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",
        "DAI":   "0x6B175474E89094C44Da98b954EedeAC495271d0F",
    },
    "sepolia": {},   # add Sepolia testnet ERC-20s here if needed
}

# ── Balance cache (5 min TTL to avoid hammering RPC) ────────────────────────
_balance_cache: dict = {"data": None, "ts": 0.0}
_CACHE_TTL = 300


@functools.lru_cache(maxsize=1)
def _get_web3() -> "Web3 | None":
    """Return a connected Web3 instance or None if unavailable."""
    if not _WEB3_OK:
        return None
    url = _RPC.get(NETWORK, _RPC["mainnet"])
    w3  = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 10}))
    return w3


def load_wallet() -> tuple:
    """
    Returns (private_key_hex, checksummed_address) from WALLET_PRIVATE_KEY env.
    Returns (None, None) if not configured or key is invalid.
    """
    if not _WEB3_OK:
        return None, None
    pk = os.getenv("WALLET_PRIVATE_KEY", "").strip()
    if not pk:
        return None, None
    if not pk.startswith("0x"):
        pk = "0x" + pk
    try:
        acct = Account.from_key(pk)
        return pk, acct.address
    except Exception as e:
        print(f"[wallet] Invalid WALLET_PRIVATE_KEY: {e}")
        return None, None


def get_address() -> str | None:
    """Return the wallet's public address, or None if not configured."""
    _, addr = load_wallet()
    return addr


def get_eth_balance(address: str) -> float | None:
    """Return ETH balance as a float, or None on error."""
    w3 = _get_web3()
    if not w3:
        return None
    try:
        checksum = Web3.to_checksum_address(address)
        wei      = w3.eth.get_balance(checksum)
        return float(w3.from_wei(wei, "ether"))
    except Exception as e:
        print(f"[wallet] ETH balance error: {e}")
        return None


def get_token_balances(address: str) -> list:
    """
    Return a list of dicts for ERC-20 tokens with non-zero balance:
    [{"symbol": "USDC", "balance": 500.0, "contract": "0x..."}]
    """
    w3 = _get_web3()
    if not w3:
        return []
    tokens   = _TOKENS.get(NETWORK, {})
    checksum = Web3.to_checksum_address(address)
    results  = []
    for sym, contract_addr in tokens.items():
        try:
            contract = w3.eth.contract(
                address=Web3.to_checksum_address(contract_addr),
                abi=_ERC20_ABI,
            )
            raw      = contract.functions.balanceOf(checksum).call()
            decimals = contract.functions.decimals().call()
            balance  = raw / (10 ** decimals)
            if balance > 0:
                results.append({
                    "symbol":   sym,
                    "balance":  round(balance, 6),
                    "contract": contract_addr,
                })
        except Exception:
            pass
    return results


def get_wallet_summary(force: bool = False) -> dict:
    """
    Return a complete wallet summary dict (cached 5 min unless force=True).

    Returns:
      {
        "configured": bool,
        "network": str,
        "address": str | None,
        "eth": float | None,
        "tokens": list,
        "error": str | None,
        "cached": bool,
        "fetched_at": float,
      }
    """
    now = time.time()
    if (not force
            and _balance_cache["data"] is not None
            and now - _balance_cache["ts"] < _CACHE_TTL):
        result = dict(_balance_cache["data"])
        result["cached"] = True
        return result

    _, address = load_wallet()
    if not address:
        return {
            "configured": False,
            "network":    NETWORK,
            "address":    None,
            "eth":        None,
            "tokens":     [],
            "error":      "WALLET_PRIVATE_KEY not set in environment",
            "cached":     False,
            "fetched_at": now,
        }

    eth    = get_eth_balance(address)
    tokens = get_token_balances(address)

    result = {
        "configured": True,
        "network":    NETWORK,
        "address":    address,
        "eth":        eth,
        "tokens":     tokens,
        "error":      None if eth is not None else "RPC connection failed",
        "cached":     False,
        "fetched_at": now,
    }
    _balance_cache["data"] = result
    _balance_cache["ts"]   = now
    return result
