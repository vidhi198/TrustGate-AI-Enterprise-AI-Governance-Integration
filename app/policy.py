"""
app/policy.py — Central policy pipeline for governed assistant requests.

Combines: input-size limits, prompt-injection heuristic, PII redaction, and
daily budget checks into one structured decision object, per
governance/04-governance-policy.md ("Structured policy decision") and
docs/02-architecture.md (request flow steps 2-6).

The policy module NEVER calls the model or a tool itself — it only decides
whether the caller is permitted to proceed, and with what (redacted) input.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional

from app.config import settings
from app.limits import check_budget
from app.security import detect_injection, redact_pii

Decision = Literal["allow", "redact_and_continue", "block", "deny_authorization"]


@dataclass
class PolicyDecision:
    decision: Decision
    policy_id: str
    policy_version: str
    reason_code: str
    request_id: Optional[str]
    evaluated_at: str
    redaction_categories: list[str] = field(default_factory=list)
    clean_message: str = ""
    message: str = ""  # human-readable explanation, safe to return to caller


def evaluate_assistant_request(
    *, message: str, request_id: Optional[str]
) -> PolicyDecision:
    now = datetime.now(timezone.utc).isoformat()

    if not message or not message.strip():
        return PolicyDecision(
            decision="block",
            policy_id=settings.policy_id,
            policy_version=settings.policy_version,
            reason_code="TG-POLICY-EMPTY-INPUT",
            request_id=request_id,
            evaluated_at=now,
            message="Message must not be empty.",
        )

    if len(message) > settings.max_input_chars:
        return PolicyDecision(
            decision="block",
            policy_id=settings.policy_id,
            policy_version=settings.policy_version,
            reason_code="TG-POLICY-INPUT-TOO-LARGE",
            request_id=request_id,
            evaluated_at=now,
            message=(
                f"Message exceeds the maximum of {settings.max_input_chars} "
                "characters."
            ),
        )

    injection_match = detect_injection(message)
    if injection_match:
        return PolicyDecision(
            decision="block",
            policy_id=settings.policy_id,
            policy_version=settings.policy_version,
            reason_code="TG-POLICY-INJECTION-SUSPECTED",
            request_id=request_id,
            evaluated_at=now,
            message=(
                "This request matched a known prompt-injection pattern and "
                "was blocked before reaching the model adapter. This is a "
                "heuristic check, not a complete defense."
            ),
        )

    budget = check_budget()
    if not budget.allowed:
        return PolicyDecision(
            decision="block",
            policy_id=settings.policy_id,
            policy_version=settings.policy_version,
            reason_code="TG-POLICY-BUDGET-EXCEEDED",
            request_id=request_id,
            evaluated_at=now,
            message=(
                f"Estimated daily budget (${budget.budget:.2f}) reached "
                f"(${budget.spent_today:.4f} spent today). This is an "
                "estimate-then-write check, not an atomic reservation."
            ),
        )

    redacted, categories = redact_pii(message)
    decision: Decision = "redact_and_continue" if categories else "allow"
    return PolicyDecision(
        decision=decision,
        policy_id=settings.policy_id,
        policy_version=settings.policy_version,
        reason_code="TG-POLICY-OK" if decision == "allow" else "TG-POLICY-REDACTED",
        request_id=request_id,
        evaluated_at=now,
        redaction_categories=categories,
        clean_message=redacted,
        message="Request permitted." if decision == "allow" else "Request permitted after redaction.",
    )
