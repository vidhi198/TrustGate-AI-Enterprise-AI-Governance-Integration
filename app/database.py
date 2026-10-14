"""
app/database.py — SQLite persistence for audit events, approvals, and daily
usage estimates.

LIMITATION (stated deliberately — ADR-004, T12, TG-AUD-003): SQLite here is a
single local file with no access control beyond the filesystem. It is not
tamper-proof and is not a substitute for a production audit sink with
retention and integrity guarantees.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from app.config import settings

_LOCAL = threading.local()
_INIT_LOCK = threading.Lock()
_INITIALIZED = False


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.database_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_conn():
    """Thread-local connection; SQLite is fine for this single-process demo
    (ADR-004: concurrency/HA limits are known and documented)."""
    if not hasattr(_LOCAL, "conn"):
        _LOCAL.conn = _connect()
    try:
        yield _LOCAL.conn
        _LOCAL.conn.commit()
    except Exception:
        _LOCAL.conn.rollback()
        raise


def init_db() -> None:
    global _INITIALIZED
    with _INIT_LOCK:
        if _INITIALIZED:
            return
        with get_conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    request_id TEXT,
                    actor_role TEXT,
                    event_type TEXT NOT NULL,
                    decision TEXT,
                    detail TEXT,
                    metadata_json TEXT
                );

                CREATE TABLE IF NOT EXISTS approvals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    requested_by TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target TEXT,
                    reason TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    policy_version TEXT,
                    expires_at TEXT,
                    idempotency_key TEXT,
                    decided_by TEXT,
                    decided_at TEXT,
                    decision_note TEXT,
                    state_version INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS usage_daily (
                    day TEXT PRIMARY KEY,
                    estimated_cost REAL NOT NULL DEFAULT 0,
                    requests INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_events(timestamp);
                CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);
                """
            )
        _INITIALIZED = True


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def record_audit(
    *,
    request_id: Optional[str],
    actor_role: Optional[str],
    event_type: str,
    decision: Optional[str] = None,
    detail: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> int:
    """Insert an audit event. TG-AUD-001: never pass secrets or raw prompts
    in `detail` or `metadata` — callers are responsible for passing only
    allowlisted, already-redacted fields."""
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO audit_events
               (timestamp, request_id, actor_role, event_type, decision, detail, metadata_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                _now_iso(),
                request_id,
                actor_role,
                event_type,
                decision,
                detail,
                json.dumps(metadata or {}),
            ),
        )
        return cur.lastrowid


def list_audit(limit: int = 100) -> list[dict]:
    limit = max(1, min(limit, 1000))
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["metadata"] = json.loads(d.pop("metadata_json") or "{}")
        except json.JSONDecodeError:
            d["metadata"] = {}
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------

def create_approval(
    *,
    requested_by: str,
    action: str,
    target: str,
    reason: str,
    policy_version: str,
    idempotency_key: Optional[str] = None,
) -> dict:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO approvals
               (timestamp, requested_by, action, target, reason, status,
                policy_version, idempotency_key)
               VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (_now_iso(), requested_by, action, target, reason, policy_version, idempotency_key),
        )
        approval_id = cur.lastrowid
        row = conn.execute(
            "SELECT * FROM approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        return dict(row)


def list_approvals(status_filter: Optional[str] = None) -> list[dict]:
    with get_conn() as conn:
        if status_filter:
            rows = conn.execute(
                "SELECT * FROM approvals WHERE status = ? ORDER BY id DESC", (status_filter,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM approvals ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def get_approval(approval_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    return dict(row) if row else None


class ApprovalConflict(Exception):
    """Raised when an approval is not in a state that allows the requested
    transition (T09 — replay/duplicate decision protection)."""


def decide_approval(
    approval_id: int, *, decision: str, decided_by: str, note: Optional[str] = None
) -> dict:
    if decision not in {"approved", "rejected"}:
        raise ValueError("decision must be 'approved' or 'rejected'")

    with get_conn() as conn:
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            raise KeyError(f"approval {approval_id} not found")
        current = dict(row)
        if current["status"] != "pending":
            raise ApprovalConflict(
                f"approval {approval_id} is already '{current['status']}'; "
                "cannot transition again (idempotent replay rejected)."
            )
        cur = conn.execute(
            """UPDATE approvals
               SET status = ?, decided_by = ?, decided_at = ?, decision_note = ?,
                   state_version = state_version + 1
               WHERE id = ? AND status = 'pending'""",
            (decision, decided_by, _now_iso(), note, approval_id),
        )
        if cur.rowcount == 0:
            # Lost a race against another decision between SELECT and UPDATE.
            raise ApprovalConflict(
                f"approval {approval_id} changed state concurrently; retry."
            )
        updated = conn.execute(
            "SELECT * FROM approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        return dict(updated)


# ---------------------------------------------------------------------------
# Usage / budget
# ---------------------------------------------------------------------------

def get_usage_today() -> dict:
    day = _today_key()
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM usage_daily WHERE day = ?", (day,)).fetchone()
    if row is None:
        return {
            "day": day,
            "estimated_cost": 0.0,
            "requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        }
    return dict(row)


def record_usage(*, input_tokens: int, output_tokens: int, cost: float) -> dict:
    day = _today_key()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO usage_daily (day, estimated_cost, requests, input_tokens, output_tokens)
               VALUES (?, ?, 1, ?, ?)
               ON CONFLICT(day) DO UPDATE SET
                 estimated_cost = estimated_cost + excluded.estimated_cost,
                 requests = requests + 1,
                 input_tokens = input_tokens + excluded.input_tokens,
                 output_tokens = output_tokens + excluded.output_tokens
            """,
            (day, cost, input_tokens, output_tokens),
        )
        row = conn.execute("SELECT * FROM usage_daily WHERE day = ?", (day,)).fetchone()
        return dict(row)


def get_metrics() -> dict:
    usage = get_usage_today()
    with get_conn() as conn:
        total_events = conn.execute("SELECT COUNT(*) AS c FROM audit_events").fetchone()["c"]
        decision_rows = conn.execute(
            "SELECT decision, COUNT(*) AS c FROM audit_events "
            "WHERE decision IS NOT NULL GROUP BY decision"
        ).fetchall()
        recent = conn.execute(
            "SELECT COUNT(*) AS c FROM audit_events WHERE id > (SELECT COALESCE(MAX(id),0) - 50 FROM audit_events)"
        ).fetchone()["c"]
    return {
        "today": usage,
        "total_audit_events": total_events,
        "recent_event_count": recent,
        "counts_by_decision": {r["decision"]: r["c"] for r in decision_rows},
    }
