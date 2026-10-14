"""
app/security.py — Authentication, role-based access control, prompt-injection
heuristic, and PII redaction.

Design references:
  - ADR-008 (API key is demo-only)
  - TG-SEC-001..004 (governance/04-governance-policy.md)
  - T01, T02, T04, T06 (docs/03-threat-model.md)

IMPORTANT LIMITATIONS (stated deliberately, not hidden):
  - The injection detector is a small keyword/pattern heuristic. It is not a
    complete prompt-injection defense (see T04 / TG-AI-004).
  - PII redaction is regex-based and will miss context-dependent sensitive
    data (see T06).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, Header, HTTPException, status

from app.config import ROLE_RANK, settings


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Identity:
    api_key: str
    role: str

    @property
    def subject(self) -> str:
        """A stable, non-secret label for audit records. Never the raw key."""
        return f"demo:{self.role}"


def get_identity(x_api_key: Optional[str] = Header(default=None)) -> Identity:
    """FastAPI dependency: authenticate the caller via X-API-Key.

    TG-SEC-002: missing/invalid identity fails closed (401).
    """
    if not x_api_key or x_api_key not in settings.api_keys:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "unauthenticated",
                "reason_code": "TG-SEC-002-MISSING-OR-INVALID-KEY",
                "message": "Missing or invalid X-API-Key header.",
            },
        )
    role = settings.api_keys[x_api_key]
    return Identity(api_key=x_api_key, role=role)


def require_role(min_role: str):
    """FastAPI dependency factory enforcing a minimum role rank.

    Usage: `identity: Identity = Depends(require_role("admin"))`

    TG-SEC-001/003: authorization is enforced server-side, never by the UI.
    """

    def _checker(identity: Identity = Depends(get_identity)) -> Identity:
        if ROLE_RANK.get(identity.role, 0) < ROLE_RANK.get(min_role, 99):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "forbidden",
                    "reason_code": "TG-SEC-003-INSUFFICIENT-ROLE",
                    "message": f"Role '{identity.role}' does not meet minimum "
                    f"required role '{min_role}' for this operation.",
                },
            )
        return identity

    return _checker


# ---------------------------------------------------------------------------
# Prompt-injection heuristic (TG-AI-004 / T04)
# ---------------------------------------------------------------------------

_INJECTION_PATTERNS = [
    r"ignore (all )?(previous|prior|above) instructions",
    r"disregard (all )?(previous|prior|above) (instructions|rules)",
    r"reveal (the )?(system prompt|system instructions|your instructions)",
    r"reveal (any )?secrets?",
    r"bypass (the )?(safety|security|content) (policy|policies|filters?)",
    r"act as an? (unrestricted|jailbroken|uncensored) assistant",
    r"you are now (in )?(dan|developer) mode",
    r"override (the )?(developer|system) instructions",
    r"pretend (you have|to have) no (restrictions|rules|guidelines)",
    r"forget (everything|all) (you (were|have been) told|your instructions)",
]
_INJECTION_RE = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)


def detect_injection(text: str) -> Optional[str]:
    """Return a matched pattern label if the text looks like a known
    prompt-injection attempt, else None.

    LIMITATION: this is a small, labeled pattern set. It will miss
    paraphrased, translated, or encoded attacks, and can produce false
    positives on benign text that happens to match a phrase.
    """
    match = _INJECTION_RE.search(text or "")
    if match:
        return match.group(0).lower()
    return None


# ---------------------------------------------------------------------------
# PII redaction (TG-AI-003 / T06)
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_PHONE_RE = re.compile(
    r"(?<!\d)(\+?\d{1,3}[\s.\-]?)?(\(?\d{3}\)?[\s.\-]?)\d{3}[\s.\-]?\d{4}(?!\d)"
)
_CARD_RE = re.compile(r"(?<!\d)\d(?:[ \-]?\d){12,18}(?!\d)")


def redact_pii(text: str) -> tuple[str, list[str]]:
    """Redact email, phone-like, and card-like sequences.

    Returns (redacted_text, categories_redacted). Order matters: card-like
    sequences are checked before phone numbers would otherwise overlap, and
    email addresses are redacted first since they may contain digit-like
    substrings.
    """
    categories: list[str] = []
    out = text or ""

    if _EMAIL_RE.search(out):
        categories.append("email")
        out = _EMAIL_RE.sub("[REDACTED_EMAIL]", out)

    if _CARD_RE.search(out):
        categories.append("card")
        out = _CARD_RE.sub("[REDACTED_CARD]", out)

    if _PHONE_RE.search(out):
        categories.append("phone")
        out = _PHONE_RE.sub("[REDACTED_PHONE]", out)

    return out, categories
