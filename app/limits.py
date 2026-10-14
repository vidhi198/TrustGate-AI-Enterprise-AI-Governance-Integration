"""
app/limits.py — Rate limiting, token/cost estimation, and daily budget checks.

LIMITATIONS (stated deliberately):
  - The rate limiter is an in-process sliding window keyed by API key. It is
    reset on restart and not shared across workers/machines (T10). A
    production deployment should use a distributed limiter (e.g. Redis) or a
    gateway's native rate-limit policy (see docs/06-ibm-api-connect-mapping.md).
  - Token estimates use a simple character-based approximation, not a real
    tokenizer, and do not reflect actual provider billing (T11).
  - The budget check is estimate-then-write, not an atomic reservation, so
    concurrent requests can exceed the configured daily budget (ADR + T11).
"""
from __future__ import annotations

import math
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from app.config import settings
from app.database import get_usage_today


class RateLimiter:
    """Simple process-local sliding-window limiter, one deque per key."""

    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, int]:
        """Returns (allowed, retry_after_seconds)."""
        now = time.monotonic()
        with self._lock:
            dq = self._hits[key]
            cutoff = now - self.window_seconds
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= self.max_requests:
                retry_after = max(1, int(self.window_seconds - (now - dq[0])))
                return False, retry_after
            dq.append(now)
            return True, 0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


rate_limiter = RateLimiter(settings.rate_limit_requests, settings.rate_limit_window_seconds)


def estimate_tokens(text: str) -> int:
    """Estimated tokens ≈ ceil(characters / 4). Not a real tokenizer."""
    if not text:
        return 0
    return math.ceil(len(text) / 4)


def estimate_cost(input_tokens: int, output_tokens: int) -> float:
    cost = (input_tokens / 1000) * settings.estimated_input_cost_per_1k
    cost += (output_tokens / 1000) * settings.estimated_output_cost_per_1k
    return round(cost, 6)


@dataclass
class BudgetCheck:
    allowed: bool
    spent_today: float
    budget: float


def check_budget() -> BudgetCheck:
    usage = get_usage_today()
    spent = float(usage["estimated_cost"])
    return BudgetCheck(
        allowed=spent < settings.daily_budget_usd,
        spent_today=spent,
        budget=settings.daily_budget_usd,
    )
