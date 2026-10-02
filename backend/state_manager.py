"""SQLite state: jobs, candidates, status history, and pipeline events (for the live visualizer).

A fresh connection is opened per call, so this is safe to use from FastAPI worker threads
and CrewAI tool threads.
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from config import settings
from schemas import (
    CandidateScreeningSchema,
    CandidateStatus,
    EvaluationSchema,
    OutreachSchema,
    SchedulingSchema,
)

S = CandidateStatus

# Legal status transitions. Pass force=True to bypass (e.g. admin fixes).
ALLOWED_TRANSITIONS: dict[S, set[S]] = {
    S.PENDING_SCREENING: {S.SCREENED},
    S.SCREENED: {S.EMAILED_PENDING_REPLY, S.EVALUATED},
    S.EMAILED_PENDING_REPLY: {S.INTERVIEW_SCHEDULED},
    S.INTERVIEW_SCHEDULED: {S.RESCHEDULE_REQUESTED, S.EVALUATED},
    S.RESCHEDULE_REQUESTED: {S.INTERVIEW_SCHEDULED},
    S.EVALUATED: set(),
}

JSON_COLUMNS = ("screening_json", "outreach_json", "scheduling_json", "evaluation_json")


class InvalidTransition(Exception):
    pass


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    jd_text     TEXT NOT NULL,
    filename    TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidates (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id                    INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    name                      TEXT,
    email                     TEXT NOT NULL,
    resume_filename           TEXT,
    resume_text               TEXT NOT NULL,
    status                    TEXT NOT NULL DEFAULT 'PENDING_SCREENING',
    match_score               REAL,
    screening_json            TEXT,
    outreach_json             TEXT,
    scheduling_json           TEXT,
    evaluation_json           TEXT,
    interview_notes           TEXT,
    gmail_thread_id           TEXT,
    last_processed_message_id TEXT,
    calendar_event_id         TEXT,
    calendar_event_link       TEXT,
    agreed_timestamp          TEXT,
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL,
    UNIQUE (job_id, email)
);
CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidates(status);
CREATE INDEX IF NOT EXISTS idx_candidates_job ON candidates(job_id);

CREATE TABLE IF NOT EXISTS status_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id  INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    from_status   TEXT,
    to_status     TEXT NOT NULL,
    note          TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id  INTEGER REFERENCES candidates(id) ON DELETE CASCADE,
    agent         TEXT NOT NULL,
    message       TEXT NOT NULL,
    level         TEXT NOT NULL DEFAULT 'info',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_id ON events(id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_conn():
    settings.ensure_dirs()
    conn = sqlite3.connect(settings.sqlite_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA_SQL)


def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    d = dict(row)
    for col in JSON_COLUMNS:
        if d.get(col):
            d[col] = json.loads(d[col])
    return d


# ------------------------------------------------------------------ jobs

def create_job(title: str, jd_text: str, filename: Optional[str] = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO jobs (title, jd_text, filename, created_at) VALUES (?, ?, ?, ?)",
            (title, jd_text, filename, _now()),
        )
        return cur.lastrowid


def get_job(job_id: int) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        return _row_to_dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())


def list_jobs() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM jobs ORDER BY id DESC").fetchall()
        return [_row_to_dict(r) for r in rows]


# ------------------------------------------------------------ candidates

def create_candidate(
    job_id: int,
    email: str,
    resume_text: str,
    name: Optional[str] = None,
    resume_filename: Optional[str] = None,
) -> int:
    """Insert a candidate with status PENDING_SCREENING. Re-uploading the same
    (job, email) returns the existing candidate id instead of duplicating."""
    email = email.strip().lower()
    now = _now()
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM candidates WHERE job_id = ? AND email = ?", (job_id, email)
        ).fetchone()
        if existing:
            return existing["id"]
        cur = conn.execute(
            """INSERT INTO candidates
               (job_id, name, email, resume_filename, resume_text, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (job_id, name, email, resume_filename, resume_text, S.PENDING_SCREENING.value, now, now),
        )
        cid = cur.lastrowid
        conn.execute(
            "INSERT INTO status_history (candidate_id, from_status, to_status, note, created_at) "
            "VALUES (?, NULL, ?, 'ingested', ?)",
            (cid, S.PENDING_SCREENING.value, now),
        )
        return cid


def get_candidate(candidate_id: int) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        return _row_to_dict(
            conn.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
        )


def get_candidate_by_email(email: str, job_id: Optional[int] = None) -> Optional[dict[str, Any]]:
    email = email.strip().lower()
    with get_conn() as conn:
        if job_id is None:
            row = conn.execute(
                "SELECT * FROM candidates WHERE email = ? ORDER BY id DESC LIMIT 1", (email,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM candidates WHERE email = ? AND job_id = ?", (email, job_id)
            ).fetchone()
        return _row_to_dict(row)


def list_candidates(
    job_id: Optional[int] = None, status: Optional[S | str] = None
) -> list[dict[str, Any]]:
    query, params = "SELECT * FROM candidates WHERE 1=1", []
    if job_id is not None:
        query += " AND job_id = ?"
        params.append(job_id)
    if status is not None:
        query += " AND status = ?"
        params.append(S(status).value)
    query += " ORDER BY COALESCE(match_score, -1) DESC, id ASC"
    with get_conn() as conn:
        return [_row_to_dict(r) for r in conn.execute(query, params).fetchall()]


# ---------------------------------------------------------------- status

def _set_status(
    conn: sqlite3.Connection,
    candidate_id: int,
    new_status: S,
    note: Optional[str],
    force: bool,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    row = conn.execute("SELECT status FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
    if row is None:
        raise KeyError(f"Candidate {candidate_id} not found")
    current = S(row["status"])
    if not force and current != new_status and new_status not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTransition(f"{current.value} -> {new_status.value} is not allowed")

    now = _now()
    fields = {"status": new_status.value, "updated_at": now, **(extra or {})}
    assignments = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE candidates SET {assignments} WHERE id = ?", (*fields.values(), candidate_id)
    )
    conn.execute(
        "INSERT INTO status_history (candidate_id, from_status, to_status, note, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (candidate_id, current.value, new_status.value, note, now),
    )


def update_status(
    candidate_id: int, new_status: S | str, note: Optional[str] = None, force: bool = False
) -> None:
    with get_conn() as conn:
        _set_status(conn, candidate_id, S(new_status), note, force)


def get_status_history(candidate_id: int) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM status_history WHERE candidate_id = ? ORDER BY id", (candidate_id,)
        ).fetchall()
        return [dict(r) for r in rows]


# ----------------------------------------------- per-stage result savers

def save_screening(candidate_id: int, result: CandidateScreeningSchema) -> None:
    """Stage 2: store screening JSON + score and move to SCREENED (for SHORTLIST and REJECT;
    the decision itself lives in screening_json and gates Outreach)."""
    with get_conn() as conn:
        _set_status(
            conn, candidate_id, S.SCREENED, f"screening: {result.screening_decision}", False,
            {"screening_json": result.model_dump_json(), "match_score": result.match_score},
        )


def save_outreach(
    candidate_id: int, result: OutreachSchema, gmail_thread_id: Optional[str] = None
) -> None:
    """Stage 3: store outreach result and move to EMAILED_PENDING_REPLY."""
    extra: dict[str, Any] = {"outreach_json": result.model_dump_json()}
    if gmail_thread_id:
        extra["gmail_thread_id"] = gmail_thread_id
    with get_conn() as conn:
        _set_status(conn, candidate_id, S.EMAILED_PENDING_REPLY, "outreach email sent", False, extra)


def save_scheduling(
    candidate_id: int,
    result: SchedulingSchema,
    calendar_event_id: Optional[str] = None,
    last_processed_message_id: Optional[str] = None,
) -> None:
    """Stage 4 (and reschedule path): store booking and set INTERVIEW_SCHEDULED."""
    extra: dict[str, Any] = {
        "scheduling_json": result.model_dump_json(),
        "calendar_event_link": result.calendar_event_link,
        "agreed_timestamp": result.agreed_timestamp,
    }
    if calendar_event_id:
        extra["calendar_event_id"] = calendar_event_id
    if last_processed_message_id:
        extra["last_processed_message_id"] = last_processed_message_id
    with get_conn() as conn:
        _set_status(conn, candidate_id, S.INTERVIEW_SCHEDULED, "interview booked", False, extra)


def mark_reschedule_requested(candidate_id: int, reply_message_id: str) -> None:
    """Reschedule edge case. Called by the Gmail poller BEFORE any agent runs.
    Only valid for already-scheduled candidates; records the triggering message id so the
    same reply is never processed twice."""
    with get_conn() as conn:
        _set_status(
            conn, candidate_id, S.RESCHEDULE_REQUESTED, "reply received after scheduling", False,
            {"last_processed_message_id": reply_message_id},
        )


def set_last_processed_message(candidate_id: int, message_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE candidates SET last_processed_message_id = ?, updated_at = ? WHERE id = ?",
            (message_id, _now(), candidate_id),
        )


def save_interview_notes(candidate_id: int, notes: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE candidates SET interview_notes = ?, updated_at = ? WHERE id = ?",
            (notes, _now(), candidate_id),
        )


def save_evaluation(candidate_id: int, result: EvaluationSchema) -> None:
    """Stage 5: store the HIRE/REJECT recommendation and move to EVALUATED."""
    with get_conn() as conn:
        _set_status(
            conn, candidate_id, S.EVALUATED, f"evaluation: {result.recommendation}", False,
            {"evaluation_json": result.model_dump_json()},
        )


# -------------------------------------------------- orchestrator helpers

def candidates_awaiting_reply() -> list[dict[str, Any]]:
    """Candidates the Gmail poller should check: emailed (first reply) or scheduled (reschedules)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM candidates WHERE status IN (?, ?) ORDER BY id",
            (S.EMAILED_PENDING_REPLY.value, S.INTERVIEW_SCHEDULED.value),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def next_pipeline_stage(candidate_id: int) -> Optional[str]:
    """Routing table the orchestrator reads. RESCHEDULE_REQUESTED routes straight to
    scheduling, bypassing screening and outreach."""
    cand = get_candidate(candidate_id)
    if cand is None:
        raise KeyError(f"Candidate {candidate_id} not found")
    status = S(cand["status"])
    if status == S.PENDING_SCREENING:
        return "screening"
    if status == S.SCREENED:
        decision = (cand.get("screening_json") or {}).get("screening_decision")
        return "outreach" if decision == "SHORTLIST" else None
    if status in (S.EMAILED_PENDING_REPLY, S.RESCHEDULE_REQUESTED):
        return "scheduling"
    if status == S.INTERVIEW_SCHEDULED:
        return "evaluation" if cand.get("interview_notes") else None
    return None


# ---------------------------------------------------------------- events

def log_event(agent: str, message: str, candidate_id: Optional[int] = None, level: str = "info") -> int:
    """Append a pipeline event; the frontend visualizer streams these."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO events (candidate_id, agent, message, level, created_at) VALUES (?, ?, ?, ?, ?)",
            (candidate_id, agent, message, level, _now()),
        )
        return cur.lastrowid


def list_events(after_id: int = 0, limit: int = 200) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (after_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]


if __name__ == "__main__":
    init_db()
    print(f"SQLite initialised at {settings.sqlite_path}")
