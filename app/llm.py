"""
app/llm.py — AI response generation: deterministic mock mode (default) and
an optional OpenAI-compatible provider mode.

ADR-005: mock mode is the default. Real provider calls only happen through
approved configuration (LLM_PROVIDER=openai_compatible + LLM_API_KEY set).

IMPORTANT: tool routing happens in app/tools.py / app/policy.py via
deterministic application logic BEFORE this module is invoked. This adapter
only generates natural-language text from an already-governed, already
redacted message + context. It never decides tool execution (ADR-006).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from app.config import settings


@dataclass
class LLMResult:
    reply: str
    provider: str
    model: str
    latency_ms: int
    error: Optional[str] = None


def _mock_reply(message: str, context: Optional[dict]) -> str:
    message_lower = (message or "").lower()

    if context and context.get("incident"):
        inc = context["incident"]
        return (
            f"Incident {inc['id']} ({inc['severity']}) — {inc['title']}. "
            f"Status: {inc['status']}. {inc['summary']}"
        )

    if context and context.get("service"):
        svc = context["service"]
        return (
            f"Service '{svc['name']}' is currently {svc['status']}. "
            f"P95 latency is {svc['p95_latency_ms']}ms with an error rate of "
            f"{svc['error_rate_pct']}%."
        )

    if context and context.get("remediation_created"):
        appr = context["remediation_created"]
        return (
            f"A pending remediation request (approval #{appr['id']}) has been "
            f"created for '{appr['target']}'. An administrator must approve "
            f"it before any further action is simulated. No real "
            f"infrastructure has been changed."
        )

    if "help" in message_lower or not message_lower.strip():
        return (
            "I can help summarize synthetic incidents, check synthetic "
            "service health, or create a pending (simulated) remediation "
            "request. Try: 'Summarize incident INC-1001' or 'Check service "
            "payments health'."
        )

    return (
        "I don't have specific governed context for that request in this "
        "demo. Try asking about a known incident (INC-1001, INC-1002) or a "
        "known service (payments, notifications, identity)."
    )


def generate(
    message: str,
    *,
    context: Optional[dict] = None,
) -> LLMResult:
    start = time.monotonic()

    if settings.llm_provider == "mock" or not settings.llm_api_key:
        reply = _mock_reply(message, context)
        latency_ms = int((time.monotonic() - start) * 1000)
        return LLMResult(
            reply=reply, provider="mock", model="trustgate-mock-v1", latency_ms=latency_ms
        )

    # Optional external provider mode.
    try:
        import httpx

        system_note = (
            "You are TrustGate AI's governed assistant. Only use the "
            "supplied context. Do not invent incident or service data."
        )
        context_note = f"\n\nContext: {context}" if context else ""
        payload = {
            "model": settings.llm_model,
            "messages": [
                {"role": "system", "content": system_note},
                {"role": "user", "content": f"{message}{context_note}"},
            ],
        }
        headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
        with httpx.Client(timeout=settings.llm_timeout_seconds) as client:
            resp = client.post(
                f"{settings.llm_base_url.rstrip('/')}/chat/completions",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            reply = data["choices"][0]["message"]["content"]
        latency_ms = int((time.monotonic() - start) * 1000)
        return LLMResult(
            reply=reply,
            provider="openai_compatible",
            model=settings.llm_model,
            latency_ms=latency_ms,
        )
    except Exception as exc:  # noqa: BLE001 - normalize all provider failures
        latency_ms = int((time.monotonic() - start) * 1000)
        return LLMResult(
            reply="",
            provider="openai_compatible",
            model=settings.llm_model,
            latency_ms=latency_ms,
            error="provider_unavailable",
        )
