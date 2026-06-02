"""
Central LLM gateway for FuturesFinder5000.

Owns provider calls, budget controls, short-lived caching, health checks, and
trade-decision review so analysis modules do not call model endpoints directly.
"""
import hashlib
from db.models import get_setting as _gs
import json
import os
import time
from datetime import datetime, timezone

import requests

from db.models import get_setting, set_setting, write_inbox


LLM_API_URL = os.getenv("LLM_API_URL", "https://models.github.ai/inference/chat/completions")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

_HEALTH_TTL = int(os.getenv("LLM_HEALTH_TTL_SECONDS", "120"))
_CACHE_TTL = int(os.getenv("LLM_CACHE_TTL_SECONDS", "300"))
_SYMBOL_COOLDOWN = int(os.getenv("LLM_SYMBOL_COOLDOWN_SECONDS", "180"))
_DAILY_CALL_CAP = int(os.getenv("LLM_DAILY_CALL_CAP", "750"))
_DAILY_CRITICAL_CAP = int(os.getenv("LLM_DAILY_CRITICAL_CAP", "500"))
_RISK_REVIEW_ENABLED = os.getenv("LLM_RISK_REVIEW_ENABLED", "true").lower() == "true"

_health_cache = {"ok": None, "ts": 0.0, "status": None}
_response_cache: dict[str, dict] = {}
_last_symbol_call: dict[str, float] = {}


class LLMBudgetError(RuntimeError):
    pass


class LLMUnavailableError(RuntimeError):
    pass


def _headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _setting_int(key: str, default: int) -> int:
    try:
        return int(get_setting(key, str(default)) or default)
    except Exception as exc:
        print(f"[llm_gateway] setting read failed for {key}: {exc}")
        return default


def _reset_budget_if_needed() -> None:
    today = _today_key()
    if get_setting("llm_budget_day", "") == today:
        return
    set_setting("llm_budget_day", today)
    set_setting("llm_calls_used", "0")
    set_setting("llm_critical_calls_used", "0")


def _consume_budget(critical: bool) -> None:
    try:
        _reset_budget_if_needed()
        calls = _setting_int("llm_calls_used", 0)
        critical_calls = _setting_int("llm_critical_calls_used", 0)
        if calls >= _DAILY_CALL_CAP:
            raise LLMBudgetError(f"daily LLM call cap reached ({calls}/{_DAILY_CALL_CAP})")
        if critical and critical_calls >= _DAILY_CRITICAL_CAP:
            raise LLMBudgetError(
                f"daily critical LLM call cap reached ({critical_calls}/{_DAILY_CRITICAL_CAP})"
            )
        set_setting("llm_calls_used", str(calls + 1))
        if critical:
            set_setting("llm_critical_calls_used", str(critical_calls + 1))
    except LLMBudgetError:
        raise
    except Exception as exc:
        print(f"[llm_gateway] budget update failed; allowing call: {exc}")


def _cache_key(purpose: str, symbol: str | None, messages: list[dict], max_tokens: int | None) -> str:
    payload = json.dumps(
        {"purpose": purpose, "symbol": symbol, "messages": messages, "max_tokens": max_tokens},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def invalidate_cache() -> None:
    _health_cache["ok"] = None
    _health_cache["ts"] = 0.0
    _health_cache["status"] = None


def llm_is_available(force: bool = False) -> bool:
    # Hard-off when user has disabled data ingestion
    try:
        if _gs("ingest_enabled", "true") == "false":
            return False
    except Exception:
        pass
    now = time.time()
    if not force and _health_cache["ok"] is not None and now - _health_cache["ts"] < _HEALTH_TTL:
        return bool(_health_cache["ok"])
    try:
        response = requests.post(
            LLM_API_URL,
            headers=_headers(),
            json={
                "model": LLM_MODEL,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
                "temperature": 0,
            },
            timeout=8,
        )
        ok = response.ok and response.status_code not in (429, 503, 504)
        _health_cache["status"] = response.status_code
    except Exception as exc:
        print(f"[llm_gateway] health check failed: {exc}")
        ok = False
        _health_cache["status"] = "error"
    _health_cache["ok"] = ok
    _health_cache["ts"] = now
    if not ok:
        print("[llm_gateway] WARNING: LLM endpoint unavailable - trade execution BLOCKED")
    return ok


def chat_completion(
    *,
    purpose: str,
    messages: list[dict],
    symbol: str | None = None,
    max_tokens: int | None = 300,
    temperature: float = 0.1,
    timeout: int = 60,
    critical: bool = False,
    cache_ttl: int | None = None,
    cooldown_seconds: int | None = None,
) -> tuple[str, dict]:
    if not llm_is_available():
        raise LLMUnavailableError("LLM unavailable")

    now = time.time()
    ttl = _CACHE_TTL if cache_ttl is None else cache_ttl
    key = _cache_key(purpose, symbol, messages, max_tokens)
    cached = _response_cache.get(key)
    if cached and now - cached["ts"] < ttl:
        meta = dict(cached["meta"])
        meta["cache_hit"] = True
        return cached["content"], meta

    if symbol:
        cooldown = _SYMBOL_COOLDOWN if cooldown_seconds is None else cooldown_seconds
        last_key = f"{purpose}:{symbol.upper()}"
        last = _last_symbol_call.get(last_key, 0.0)
        if cooldown > 0 and now - last < cooldown:
            raise LLMBudgetError(
                f"LLM cooldown active for {last_key}: {int(cooldown - (now - last))}s remaining"
            )

    _consume_budget(critical=critical)
    started = time.time()
    response = requests.post(
        LLM_API_URL,
        headers=_headers(),
        json={
            "model": LLM_MODEL,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
        timeout=timeout,
    )
    if response.status_code in (429, 503, 504):
        invalidate_cache()
    if response.status_code == 429:
        detail = response.json().get("error", {}).get("message", "daily limit reached")
        raise LLMBudgetError(f"LLM rate limited (429): {detail}")
    response.raise_for_status()

    content = response.json()["choices"][0]["message"]["content"].strip()
    meta = {
        "model": LLM_MODEL,
        "latency_ms": int((time.time() - started) * 1000),
        "status_code": response.status_code,
        "cache_hit": False,
    }
    _response_cache[key] = {"content": content, "meta": meta, "ts": time.time()}
    if symbol:
        _last_symbol_call[f"{purpose}:{symbol.upper()}"] = time.time()
    return content, meta


def extract_json_object(content: str) -> dict:
    start = content.find("{")
    end = content.rfind("}") + 1
    if start < 0 or end <= start:
        raise ValueError("LLM response did not contain a JSON object")
    return json.loads(content[start:end])


def risk_review_decision(
    *,
    service: str,
    symbol: str,
    decision: dict,
    context: dict,
) -> dict:
    action = str(decision.get("action", "hold")).lower()
    if action == "hold" or not _RISK_REVIEW_ENABLED:
        return decision

    review_prompt = {
        "service": service,
        "symbol": symbol,
        "proposed_decision": decision,
        "compact_context": context,
        "instructions": (
            "You are FF5000's risk officer. Approve only if the proposed trade has "
            "clear edge, enough confidence, and no obvious risk conflict. Respond as "
            "JSON only: {\"approved\":true|false,\"reasoning\":\"<=120 chars\"}."
        ),
    }
    try:
        content, _meta = chat_completion(
            purpose=f"{service}:risk_review",
            symbol=symbol,
            messages=[{"role": "user", "content": json.dumps(review_prompt, separators=(",", ":"))}],
            max_tokens=160,
            temperature=0,
            timeout=45,
            critical=True,
            cache_ttl=60,
            cooldown_seconds=0,
        )
        review = extract_json_object(content)
        if bool(review.get("approved")):
            decision["risk_review"] = review.get("reasoning", "approved")
            return decision
        veto = str(review.get("reasoning") or "risk officer veto")[:180]
        print(f"  [{symbol}] Risk officer veto: {veto}")
        return {
            **decision,
            "action": "hold",
            "confidence": min(float(decision.get("confidence", 0.0) or 0.0), 0.49),
            "reasoning": f"Risk officer veto: {veto}",
            "risk_review": veto,
        }
    except Exception as exc:
        write_inbox(
            "alert",
            "Risk Officer Review Failed - Trade Blocked",
            f"{service} recommendation for {symbol} could not be reviewed: {exc}",
            source="llm_gateway",
        )
        print(f"  [{symbol}] Risk officer unavailable: {exc} - forcing HOLD")
        return {
            **decision,
            "action": "hold",
            "confidence": 0.0,
            "reasoning": f"Risk officer unavailable: {exc}",
            "risk_review": "review_failed",
        }
