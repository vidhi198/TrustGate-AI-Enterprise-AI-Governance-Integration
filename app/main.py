"""
app/main.py — TrustGate AI FastAPI gateway.

Implements the endpoint inventory from README §7.1 / docs/01-api-strategy.md.
Request lifecycle matches docs/02-architecture.md "Request flow".

This file is intentionally the integration point: it wires identity,
policy, tools, the LLM adapter, and persistence together, and maps outcomes
to a consistent error envelope (docs/02-architecture.md "Failure behavior
baseline").
"""
from __future__ import annotations

import re
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.database import (
    ApprovalConflict,
    get_approval,
    get_metrics,
    init_db,
    list_approvals,
    list_audit,
    decide_approval,
    record_audit,
    record_usage,
)
from app.limits import estimate_cost, estimate_tokens, rate_limiter
from app.llm import generate
from app.policy import evaluate_assistant_request
from app.security import Identity, get_identity, require_role
from app.services import get_incident, get_service_health, list_services
from app.tools import ToolError, invoke_tool, list_tools_for_role, route_tool_from_message

REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9\-_]{1,64}$")

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(
    lifespan=lifespan,
    title=settings.app_name,
    description=(
        "TrustGate AI — a governed application boundary between consumers, "
        "an AI assistant, and allowlisted enterprise tools. Portfolio "
        "prototype; see /health and project docs for scope and limitations."
    ),
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)




# ---------------------------------------------------------------------------
# Request ID middleware (TG-OPS-001 / T18)
# ---------------------------------------------------------------------------

@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    incoming = request.headers.get("X-Request-ID")
    if incoming and REQUEST_ID_RE.match(incoming):
        request_id = incoming
    else:
        request_id = uuid.uuid4().hex
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", uuid.uuid4().hex)


# ---------------------------------------------------------------------------
# Error envelope (TG-REL-002)
# ---------------------------------------------------------------------------

def _error_body(request: Request, error: str, reason_code: str, message: str) -> dict:
    return {
        "error": error,
        "reason_code": reason_code,
        "message": message,
        "request_id": _request_id(request),
    }


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    detail = exc.detail
    if isinstance(detail, dict):
        body = {**detail, "request_id": _request_id(request)}
    else:
        body = _error_body(request, "error", "TG-ERROR", str(detail))
    return JSONResponse(status_code=exc.status_code, content=body)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # TG-REL-002: never leak stack traces or internals to the caller.
    record_audit(
        request_id=_request_id(request),
        actor_role=None,
        event_type="unhandled_error",
        decision="error",
        detail="Unhandled exception (details withheld from response).",
        metadata={"exception_type": type(exc).__name__},
    )
    return JSONResponse(
        status_code=500,
        content=_error_body(
            request, "internal_error", "TG-INTERNAL-ERROR", "An unexpected error occurred."
        ),
    )


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=20000)


class ToolCallRequest(BaseModel):
    name: str
    arguments: dict = Field(default_factory=dict)


class ApprovalDecisionRequest(BaseModel):
    decision: str
    note: Optional[str] = None


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok", "app": settings.app_name, "mode": settings.app_env}


# ---------------------------------------------------------------------------
# Authenticated reads
# ---------------------------------------------------------------------------

@app.get("/services")
def get_services(identity: Identity = Depends(get_identity)):
    return {"services": list_services()}


@app.get("/services/{service_id}/health")
def get_service_health_endpoint(service_id: str, identity: Identity = Depends(get_identity)):
    svc = get_service_health(service_id)
    if svc is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "reason_code": "TG-NOT-FOUND", "message": "Unknown service_id."},
        )
    return svc


@app.get("/incidents/{incident_id}")
def get_incident_endpoint(incident_id: str, identity: Identity = Depends(get_identity)):
    inc = get_incident(incident_id)
    if inc is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "reason_code": "TG-NOT-FOUND", "message": "Unknown incident_id."},
        )
    return inc


# ---------------------------------------------------------------------------
# Assistant chat (policy-governed)
# ---------------------------------------------------------------------------

@app.post("/assistant/chat")
def assistant_chat(
    body: ChatRequest, request: Request, identity: Identity = Depends(get_identity)
):
    request_id = _request_id(request)

    allowed, retry_after = rate_limiter.check(identity.api_key)
    if not allowed:
        record_audit(
            request_id=request_id, actor_role=identity.role, event_type="rate_limited",
            decision="block", detail="Rate limit exceeded.",
        )
        raise HTTPException(
            status_code=429,
            detail={
                "error": "rate_limited",
                "reason_code": "TG-OPS-002-RATE-LIMIT",
                "message": "Too many requests. Please retry later.",
                "retry_after": retry_after,
            },
            headers={"Retry-After": str(retry_after)},
        )

    decision = evaluate_assistant_request(message=body.message, request_id=request_id)

    record_audit(
        request_id=request_id,
        actor_role=identity.role,
        event_type="policy_check",
        decision=decision.decision,
        detail=decision.message,
        metadata={
            "reason_code": decision.reason_code,
            "policy_version": decision.policy_version,
            "redaction_categories": decision.redaction_categories,
        },
    )

    if decision.decision == "block":
        status_code = 429 if decision.reason_code == "TG-POLICY-BUDGET-EXCEEDED" else 422
        raise HTTPException(
            status_code=status_code,
            detail={
                "error": "policy_blocked",
                "reason_code": decision.reason_code,
                "message": decision.message,
                "policy_version": decision.policy_version,
            },
        )

    clean_message = decision.clean_message

    # Deterministic tool routing (ADR-006) — never model-selected.
    context: dict = {}
    tool_result = None
    routed = route_tool_from_message(clean_message)
    if routed:
        tool_name, tool_args = routed
        try:
            tool_result = invoke_tool(tool_name, tool_args, role=identity.role)
            context.update(tool_result)
            record_audit(
                request_id=request_id, actor_role=identity.role, event_type="tool_call",
                decision="allow", detail=f"Invoked tool '{tool_name}'.",
                metadata={"tool": tool_name, "arguments": tool_args},
            )
            if "remediation_created" in tool_result:
                record_audit(
                    request_id=request_id, actor_role=identity.role,
                    event_type="approval_created", decision="pending",
                    detail=f"Pending approval #{tool_result['remediation_created']['id']} created.",
                    metadata={"approval_id": tool_result["remediation_created"]["id"]},
                )
        except ToolError as te:
            record_audit(
                request_id=request_id, actor_role=identity.role, event_type="tool_call",
                decision="deny", detail=str(te), metadata={"reason_code": te.reason_code},
            )
            # A denied/failed tool lookup does not block the whole chat reply —
            # the assistant falls back to a generic governed response.

    llm_result = generate(clean_message, context=context)

    if llm_result.error:
        record_audit(
            request_id=request_id, actor_role=identity.role, event_type="model_error",
            decision="error", detail="Provider unavailable.",
        )
        raise HTTPException(
            status_code=503,
            detail={
                "error": "provider_unavailable",
                "reason_code": "TG-REL-001-PROVIDER-TIMEOUT",
                "message": "The AI provider did not return a response in time.",
            },
        )

    input_tokens = estimate_tokens(clean_message)
    output_tokens = estimate_tokens(llm_result.reply)
    cost = estimate_cost(input_tokens, output_tokens)
    record_usage(input_tokens=input_tokens, output_tokens=output_tokens, cost=cost)

    record_audit(
        request_id=request_id, actor_role=identity.role, event_type="assistant_response",
        decision="allow", detail="Response generated.",
        metadata={
            "provider": llm_result.provider,
            "model": llm_result.model,
            "latency_ms": llm_result.latency_ms,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "estimated_cost": cost,
        },
    )

    return {
        "request_id": request_id,
        "reply": llm_result.reply,
        "redactions": decision.redaction_categories,
        "tool_result": tool_result,
        "estimated_usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "estimated_cost": cost,
            "provider": llm_result.provider,
            "model": llm_result.model,
        },
    }


# ---------------------------------------------------------------------------
# MCP-style tool interface (ADR-010: style only, not full protocol)
# ---------------------------------------------------------------------------

@app.get("/mcp/tools")
def mcp_tools(identity: Identity = Depends(get_identity)):
    return {"tools": list_tools_for_role(identity.role), "protocol_note": "MCP-style REST shape; not a full MCP SDK/transport implementation (ADR-010)."}


@app.post("/mcp/call")
def mcp_call(
    body: ToolCallRequest, request: Request, identity: Identity = Depends(get_identity)
):
    request_id = _request_id(request)
    try:
        result = invoke_tool(body.name, body.arguments, role=identity.role)
    except ToolError as te:
        status_code = 403 if te.reason_code == "TG-TOOL-FORBIDDEN" else (
            404 if te.reason_code == "TG-TOOL-NOT-FOUND" else 422
        )
        record_audit(
            request_id=request_id, actor_role=identity.role, event_type="tool_call",
            decision="deny", detail=str(te), metadata={"reason_code": te.reason_code, "tool": body.name},
        )
        raise HTTPException(
            status_code=status_code,
            detail={"error": "tool_error", "reason_code": te.reason_code, "message": str(te)},
        )

    record_audit(
        request_id=request_id, actor_role=identity.role, event_type="tool_call",
        decision="allow", detail=f"Invoked tool '{body.name}' via /mcp/call.",
        metadata={"tool": body.name, "arguments": body.arguments},
    )
    return {"request_id": request_id, "result": result}


# ---------------------------------------------------------------------------
# Admin: audit, approvals, metrics
# ---------------------------------------------------------------------------

@app.get("/audit")
def get_audit(limit: int = 100, identity: Identity = Depends(require_role("admin"))):
    return {"events": list_audit(limit)}


@app.get("/approvals")
def get_approvals(
    status_filter: Optional[str] = None, identity: Identity = Depends(require_role("admin"))
):
    return {"approvals": list_approvals(status_filter)}


@app.post("/approvals/{approval_id}/decision")
def post_approval_decision(
    approval_id: int,
    body: ApprovalDecisionRequest,
    request: Request,
    identity: Identity = Depends(require_role("admin")),
):
    request_id = _request_id(request)
    decision_map = {"approve": "approved", "reject": "rejected"}
    if body.decision not in decision_map:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_decision",
                "reason_code": "TG-APPROVAL-INVALID-DECISION",
                "message": "decision must be 'approve' or 'reject'.",
            },
        )

    existing = get_approval(approval_id)
    if existing is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "reason_code": "TG-NOT-FOUND", "message": "Unknown approval_id."},
        )

    try:
        updated = decide_approval(
            approval_id,
            decision=decision_map[body.decision],
            decided_by=identity.subject,
            note=body.note,
        )
    except ApprovalConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "conflict",
                "reason_code": "TG-APP-003-STALE-APPROVAL",
                "message": str(exc),
            },
        )

    record_audit(
        request_id=request_id, actor_role=identity.role, event_type="approval_decision",
        decision=updated["status"], detail=f"Approval #{approval_id} {updated['status']}.",
        metadata={"approval_id": approval_id, "note": body.note},
    )
    return {"request_id": request_id, "approval": updated}


@app.get("/metrics")
def get_metrics_endpoint(identity: Identity = Depends(require_role("admin"))):
    return get_metrics()
