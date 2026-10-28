"""
db.py — AutoHeal AI
SQLite persistence layer:
  • run_history   — every attempt with full context
  • fix_memory    — error_signature → fix (self-learning cache)
  • dead_letter_queue — escalated failures awaiting human
  • audit_log     — immutable record of every action
"""

from __future__ import annotations
import sqlite3, json, os
from datetime import datetime
from typing import Optional

DB_PATH = os.path.join(os.path.dirname(__file__), "autoheal.db")

_DDL = """
CREATE TABLE IF NOT EXISTS run_history (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           TEXT    UNIQUE,
    run_at           TEXT    NOT NULL,
    workflow         TEXT    NOT NULL,
    failure_mode     TEXT    NOT NULL,
    attempt          INTEGER NOT NULL DEFAULT 1,
    status           TEXT    NOT NULL,        -- SUCCESS | FAILED | ESCALATED
    failed_step      TEXT,
    error_type       TEXT,
    error_signature  TEXT,
    root_cause       TEXT,
    fix_action       TEXT,
    fix_source       TEXT,                    -- memory | llm | fallback | none
    confidence       REAL,
    original_payload TEXT,
    patched_payload  TEXT,
    output           TEXT,
    duration_ms      REAL,
    mttr_ms          REAL
);

CREATE TABLE IF NOT EXISTS fix_memory (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    error_signature TEXT    UNIQUE NOT NULL,
    workflow        TEXT    NOT NULL,
    error_type      TEXT    NOT NULL,
    field           TEXT,
    fix_action      TEXT    NOT NULL,
    patch           TEXT    NOT NULL DEFAULT '{}',
    use_count       INTEGER DEFAULT 1,
    success_count   INTEGER DEFAULT 0,
    last_used       TEXT,
    created_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS dead_letter_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    queued_at   TEXT NOT NULL,
    run_id      TEXT,
    workflow    TEXT NOT NULL,
    failure_mode TEXT NOT NULL,
    error_type  TEXT,
    root_cause  TEXT,
    payload     TEXT,
    reason      TEXT,
    status      TEXT DEFAULT 'pending',   -- pending | resolved | dismissed
    resolved_at TEXT,
    resolved_by TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at   TEXT NOT NULL,
    run_id      TEXT,
    event_type  TEXT NOT NULL,
    actor       TEXT NOT NULL,   -- system | ai | human
    description TEXT NOT NULL,
    metadata    TEXT
);
"""


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with _conn() as con:
        con.executescript(_DDL)


# ══════════════════════════════════════════════════════════════
# Fix Memory  (self-learning cache)
# ══════════════════════════════════════════════════════════════

def lookup_fix(signature: str) -> Optional[dict]:
    """Return cached fix dict or None on miss."""
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM fix_memory WHERE error_signature = ?", (signature,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["patch"] = json.loads(d["patch"])
    return d


def store_fix(
    signature: str, workflow: str, error_type: str,
    field: Optional[str], fix_action: str, patch: dict,
):
    now = datetime.now().isoformat(timespec="seconds")
    with _conn() as con:
        con.execute(
            """INSERT INTO fix_memory
               (error_signature, workflow, error_type, field, fix_action, patch,
                use_count, success_count, last_used, created_at)
               VALUES (?,?,?,?,?,?,1,0,?,?)
               ON CONFLICT(error_signature) DO UPDATE SET
                 use_count    = use_count + 1,
                 last_used    = excluded.last_used""",
            (signature, workflow, error_type, field,
             fix_action, json.dumps(patch), now, now),
        )


def mark_fix_success(signature: str):
    with _conn() as con:
        con.execute(
            "UPDATE fix_memory SET success_count = success_count + 1 WHERE error_signature = ?",
            (signature,),
        )


def fetch_fix_memory() -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM fix_memory ORDER BY use_count DESC"
        ).fetchall()
    return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════
# Run History
# ══════════════════════════════════════════════════════════════

def insert_run(
    run_id: str, workflow: str, failure_mode: str, attempt: int,
    status: str, failed_step: Optional[str],
    error_type: Optional[str], error_signature: Optional[str],
    root_cause: Optional[str], fix_action: Optional[str], fix_source: Optional[str],
    confidence: Optional[float],
    original_payload: Optional[dict], patched_payload: Optional[dict],
    output: Optional[dict],
    duration_ms: float, mttr_ms: Optional[float],
):
    with _conn() as con:
        con.execute(
            """INSERT OR REPLACE INTO run_history
               (run_id, run_at, workflow, failure_mode, attempt, status, failed_step,
                error_type, error_signature, root_cause, fix_action, fix_source,
                confidence, original_payload, patched_payload, output, duration_ms, mttr_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                datetime.now().isoformat(timespec="seconds"),
                workflow, failure_mode, attempt, status, failed_step,
                error_type, error_signature, root_cause, fix_action, fix_source,
                confidence,
                json.dumps(original_payload) if original_payload else None,
                json.dumps(patched_payload)  if patched_payload  else None,
                json.dumps(output)           if output           else None,
                duration_ms, mttr_ms,
            ),
        )


def fetch_history(limit: int = 60) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM run_history ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def fetch_metrics() -> dict:
    with _conn() as con:
        total    = con.execute("SELECT COUNT(*) FROM run_history").fetchone()[0]
        success  = con.execute("SELECT COUNT(*) FROM run_history WHERE status='SUCCESS'").fetchone()[0]
        escalated= con.execute("SELECT COUNT(*) FROM run_history WHERE status='ESCALATED'").fetchone()[0]
        by_type  = con.execute(
            "SELECT error_type, COUNT(*) cnt FROM run_history WHERE error_type IS NOT NULL GROUP BY error_type"
        ).fetchall()
        by_src   = con.execute(
            "SELECT fix_source, COUNT(*) cnt FROM run_history WHERE fix_source IS NOT NULL GROUP BY fix_source"
        ).fetchall()
        avg_mttr = con.execute(
            "SELECT AVG(mttr_ms) FROM run_history WHERE status='SUCCESS' AND mttr_ms IS NOT NULL"
        ).fetchone()[0]
        mem_rows = con.execute(
            "SELECT SUM(use_count) total, SUM(use_count-1) hits, COUNT(*) patterns FROM fix_memory"
        ).fetchone()

    total_mem   = mem_rows["total"] or 0
    mem_hits    = mem_rows["hits"]  or 0
    mem_patterns= mem_rows["patterns"] or 0

    return {
        "total":          total,
        "success":        success,
        "escalated":      escalated,
        "failed":         total - success - escalated,
        "auto_heal_rate": round(success / total * 100, 1) if total else 0,
        "by_type":        {r["error_type"]: r["cnt"] for r in by_type},
        "by_source":      {r["fix_source"]: r["cnt"] for r in by_src},
        "avg_mttr_s":     round((avg_mttr or 0) / 1000, 1),
        "mem_patterns":   mem_patterns,
        "mem_hits":       mem_hits,
        "llm_calls_saved": mem_hits,
    }


def clear_all():
    with _conn() as con:
        for tbl in ("run_history", "fix_memory", "dead_letter_queue", "audit_log"):
            con.execute(f"DELETE FROM {tbl}")


# ══════════════════════════════════════════════════════════════
# Dead-Letter Queue
# ══════════════════════════════════════════════════════════════

def push_dlq(
    run_id: str, workflow: str, failure_mode: str,
    error_type: Optional[str], root_cause: Optional[str],
    payload: Optional[dict], reason: str,
):
    with _conn() as con:
        con.execute(
            """INSERT INTO dead_letter_queue
               (queued_at, run_id, workflow, failure_mode, error_type, root_cause, payload, reason)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                datetime.now().isoformat(timespec="seconds"),
                run_id, workflow, failure_mode, error_type, root_cause,
                json.dumps(payload) if payload else None, reason,
            ),
        )


def fetch_dlq() -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM dead_letter_queue ORDER BY id DESC"
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        if d.get("payload"):
            try:
                d["payload"] = json.loads(d["payload"])
            except Exception:
                pass
        result.append(d)
    return result


def resolve_dlq(dlq_id: int, resolved_by: str = "human"):
    with _conn() as con:
        con.execute(
            "UPDATE dead_letter_queue SET status='resolved', resolved_at=?, resolved_by=? WHERE id=?",
            (datetime.now().isoformat(timespec="seconds"), resolved_by, dlq_id),
        )


# ══════════════════════════════════════════════════════════════
# Audit Log  (immutable)
# ══════════════════════════════════════════════════════════════

def audit(
    run_id: Optional[str],
    event_type: str,
    actor: str,             # system | ai | human
    description: str,
    metadata: Optional[dict] = None,
):
    with _conn() as con:
        con.execute(
            """INSERT INTO audit_log (logged_at, run_id, event_type, actor, description, metadata)
               VALUES (?,?,?,?,?,?)""",
            (
                datetime.now().isoformat(timespec="seconds"),
                run_id, event_type, actor, description,
                json.dumps(metadata) if metadata else None,
            ),
        )


def fetch_audit(run_id: Optional[str] = None, limit: int = 100) -> list[dict]:
    with _conn() as con:
        if run_id:
            rows = con.execute(
                "SELECT * FROM audit_log WHERE run_id=? ORDER BY id DESC LIMIT ?",
                (run_id, limit),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


init_db()
