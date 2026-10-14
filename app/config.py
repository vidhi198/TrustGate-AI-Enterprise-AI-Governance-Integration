"""
app/config.py — Environment configuration and demo identity mapping.

Loads settings from the environment (via python-dotenv in local dev) and
exposes a single `settings` object used throughout the application.

SECURITY NOTE: TG_API_KEYS defaults to well-known, public demo credentials.
These are explicitly NOT production secrets — see docs/03-threat-model.md
(T01) and ADR-008. A startup check refuses to run with demo keys if
APP_ENV=production.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv is a declared dependency
    pass


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _default_demo_keys() -> dict:
    return {
        "tg-employee-demo": "employee",
        "tg-support-demo": "support",
        "tg-admin-demo": "admin",
    }


@dataclass
class Settings:
    app_name: str = os.getenv("APP_NAME", "TrustGate AI")
    app_env: str = os.getenv("APP_ENV", "demo")  # demo | production
    database_path: str = os.getenv("DATABASE_PATH", "trustgate.db")

    # --- AI adapter ---
    llm_provider: str = os.getenv("LLM_PROVIDER", "mock")  # mock | openai_compatible
    llm_base_url: str = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    llm_model: str = os.getenv("LLM_MODEL", "gpt-4o-mini")
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_timeout_seconds: float = float(os.getenv("LLM_TIMEOUT_SECONDS", "15"))

    # --- Policy / input limits ---
    max_input_chars: int = int(os.getenv("MAX_INPUT_CHARS", "4000"))

    # --- Rate limiting (process-local; see ADR / threat T10) ---
    rate_limit_requests: int = int(os.getenv("RATE_LIMIT_REQUESTS", "20"))
    rate_limit_window_seconds: int = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))

    # --- Cost governance ---
    daily_budget_usd: float = float(os.getenv("DAILY_BUDGET_USD", "5.00"))
    estimated_input_cost_per_1k: float = float(
        os.getenv("ESTIMATED_INPUT_COST_PER_1K", "0.001")
    )
    estimated_output_cost_per_1k: float = float(
        os.getenv("ESTIMATED_OUTPUT_COST_PER_1K", "0.002")
    )

    # --- Policy metadata ---
    policy_id: str = "TG-GOV-001"
    policy_version: str = "0.1-draft"

    api_keys: dict = field(default_factory=dict)

    def __post_init__(self):
        raw = os.getenv("TG_API_KEYS")
        if raw:
            try:
                parsed = json.loads(raw)
                # Accept {"employee": "key", ...} and normalize to {key: role}
                self.api_keys = {v: k for k, v in parsed.items()}
            except (json.JSONDecodeError, AttributeError):
                self.api_keys = _default_demo_keys()
        else:
            self.api_keys = _default_demo_keys()

        if self.app_env == "production" and self.api_keys == _default_demo_keys():
            raise RuntimeError(
                "TG-SEC-004 violation: refusing to start with built-in demo API "
                "keys while APP_ENV=production. Set TG_API_KEYS to production "
                "identities or use a real identity provider."
            )


settings = Settings()

BASE_DIR = Path(__file__).resolve().parent.parent

ROLE_RANK = {"employee": 1, "support": 2, "admin": 3}
