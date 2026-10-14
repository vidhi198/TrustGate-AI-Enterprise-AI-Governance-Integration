"""
app/tools.py — Allowlisted tool catalog and handlers.

ADR-006: deterministic, application-controlled tool routing. The model never
selects or executes a tool on its own; application code matches intent
keywords and invokes a fixed registry. Unknown tools/arguments are rejected
outright (TG-AI-002 / T07).

ADR-010: `/mcp/*` endpoints in app/main.py are MCP-STYLE only — this module
implements a simple internal tool registry, not the Model Context Protocol.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from app.config import ROLE_RANK
from app.database import create_approval
from app.services import get_incident, get_service_health


class ToolError(Exception):
    def __init__(self, message: str, reason_code: str):
        super().__init__(message)
        self.reason_code = reason_code


@dataclass
class ToolSpec:
    name: str
    description: str
    min_role: str
    handler: Callable[[dict, str], dict]  # (arguments, requester_role) -> result
    required_args: tuple[str, ...]


def _handle_get_incident(args: dict, role: str) -> dict:
    incident_id = args.get("incident_id")
    incident = get_incident(incident_id) if incident_id else None
    if incident is None:
        raise ToolError(f"Unknown incident_id '{incident_id}'", "TG-TOOL-NOT-FOUND")
    return {"incident": incident}


def _handle_get_service_health(args: dict, role: str) -> dict:
    service_id = args.get("service_id")
    service = get_service_health(service_id) if service_id else None
    if service is None:
        raise ToolError(f"Unknown service_id '{service_id}'", "TG-TOOL-NOT-FOUND")
    return {"service": service}


def _handle_request_remediation(args: dict, role: str) -> dict:
    target = args.get("target")
    reason = args.get("reason", "Requested via assistant")
    if not target:
        raise ToolError("Missing required argument 'target'", "TG-TOOL-BAD-ARGS")
    approval = create_approval(
        requested_by=role,
        action="remediate",
        target=target,
        reason=reason,
        policy_version="0.1-draft",
    )
    return {"remediation_created": approval}


TOOL_REGISTRY: dict[str, ToolSpec] = {
    "get_incident": ToolSpec(
        name="get_incident",
        description="Retrieve a synthetic incident by ID.",
        min_role="employee",
        handler=_handle_get_incident,
        required_args=("incident_id",),
    ),
    "get_service_health": ToolSpec(
        name="get_service_health",
        description="Retrieve synthetic service health metrics.",
        min_role="employee",
        handler=_handle_get_service_health,
        required_args=("service_id",),
    ),
    "request_remediation": ToolSpec(
        name="request_remediation",
        description="Create a pending (simulated) remediation request.",
        min_role="support",
        handler=_handle_request_remediation,
        required_args=("target",),
    ),
}


def list_tools_for_role(role: str) -> list[dict]:
    rank = ROLE_RANK.get(role, 0)
    return [
        {"name": t.name, "description": t.description, "min_role": t.min_role}
        for t in TOOL_REGISTRY.values()
        if ROLE_RANK.get(t.min_role, 99) <= rank
    ]


def invoke_tool(name: str, arguments: dict, *, role: str) -> dict:
    """Deny-by-default tool invocation.

    Raises ToolError for unknown tools, missing args, role violations, or
    lookup failures — callers map this to a structured 4xx response and an
    audit event. No tool here ever executes a shell command, raw SQL, or an
    arbitrary URL (T07 / TG-AI-002).
    """
    spec = TOOL_REGISTRY.get(name)
    if spec is None:
        raise ToolError(f"Unknown tool '{name}'", "TG-TOOL-UNKNOWN")

    if ROLE_RANK.get(role, 0) < ROLE_RANK.get(spec.min_role, 99):
        raise ToolError(
            f"Role '{role}' is not authorized to invoke tool '{name}'",
            "TG-TOOL-FORBIDDEN",
        )

    missing = [a for a in spec.required_args if not arguments.get(a)]
    if missing:
        raise ToolError(
            f"Missing required argument(s) for '{name}': {', '.join(missing)}",
            "TG-TOOL-BAD-ARGS",
        )

    # Reject unexpected extra arguments rather than silently forwarding them.
    extra = set(arguments.keys()) - set(spec.required_args) - {"reason"}
    if extra:
        raise ToolError(
            f"Unexpected argument(s) for '{name}': {', '.join(sorted(extra))}",
            "TG-TOOL-BAD-ARGS",
        )

    return spec.handler(arguments, role)


def route_tool_from_message(message: str) -> Optional[tuple[str, dict]]:
    """Deterministic keyword-based routing from a chat message to a tool
    call. This is intentionally simple and NOT a model-driven decision
    (ADR-006) — the assistant endpoint uses this before calling the LLM
    adapter so tool execution stays predictable and inspectable.
    """
    text = (message or "").lower()

    import re

    inc_match = re.search(r"inc-\d+", text)
    if inc_match and ("remediat" not in text):
        return "get_incident", {"incident_id": inc_match.group(0).upper()}

    if "remediat" in text:
        target = inc_match.group(0).upper() if inc_match else None
        svc_match = None
        if not target:
            for svc_id in ("payments", "notifications", "identity"):
                if svc_id in text:
                    svc_match = svc_id
                    break
        return "request_remediation", {
            "target": target or svc_match or "unspecified",
            "reason": message.strip()[:200],
        }

    for svc_id in ("payments", "notifications", "identity"):
        if svc_id in text and ("health" in text or "status" in text or "service" in text):
            return "get_service_health", {"service_id": svc_id}

    return None
