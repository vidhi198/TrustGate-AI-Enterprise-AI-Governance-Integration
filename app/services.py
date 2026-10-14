"""
app/services.py — Synthetic enterprise data. No real system is queried.

See README §5 for the fixture rationale: this project demonstrates
governance patterns, not live service integration.
"""
from __future__ import annotations

from typing import Optional

INCIDENTS: dict[str, dict] = {
    "INC-1001": {
        "id": "INC-1001",
        "title": "Elevated payment latency",
        "severity": "SEV-2",
        "status": "Investigating",
        "service": "payments",
        "summary": (
            "Simulated increase in payment processing latency observed "
            "following a recent deployment. P95 latency elevated above "
            "baseline; error rate modestly increased."
        ),
    },
    "INC-1002": {
        "id": "INC-1002",
        "title": "Intermittent notification failures",
        "severity": "SEV-3",
        "status": "Monitoring",
        "service": "notifications",
        "summary": (
            "Simulated elevated retry activity on the notifications "
            "service. Impact is intermittent and currently being monitored."
        ),
    },
}

SERVICES: dict[str, dict] = {
    "payments": {
        "id": "payments",
        "name": "Payments",
        "status": "Degraded",
        "p95_latency_ms": 840,
        "error_rate_pct": 2.8,
    },
    "notifications": {
        "id": "notifications",
        "name": "Notifications",
        "status": "Healthy",
        "p95_latency_ms": 120,
        "error_rate_pct": 0.2,
    },
    "identity": {
        "id": "identity",
        "name": "Identity",
        "status": "Healthy",
        "p95_latency_ms": 95,
        "error_rate_pct": 0.1,
    },
}


def list_services() -> list[dict]:
    return list(SERVICES.values())


def get_service_health(service_id: str) -> Optional[dict]:
    return SERVICES.get(service_id)


def get_incident(incident_id: str) -> Optional[dict]:
    return INCIDENTS.get(incident_id)
