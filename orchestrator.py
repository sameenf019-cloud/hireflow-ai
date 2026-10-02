"""
orchestrator.py — state-driven routing for the HireFlow pipeline.

SQLite is the source of truth. The orchestrator reads a candidate's status
and decides which agent(s) run. Key rule for the reschedule edge case:

    1. A plain-Python Gmail poll (NO LLM) spots a new reply on an
       INTERVIEW_SCHEDULED candidate.
    2. Status is flipped to RESCHEDULE_REQUESTED and committed BEFORE any
       agent runs.
    3. The router sees RESCHEDULE_REQUESTED and runs the Scheduling Agent
       ONLY (Screening/Outreach are bypassed): old event is cancelled, a new
       one is booked, status resets to INTERVIEW_SCHEDULED.

All run_* functions are blocking (CrewAI kickoff is sync). In FastAPI call
them via asyncio.to_thread / BackgroundTasks. Pass on_event to receive
live progress dicts for the frontend agent-handoff visualizer.

------------------------------------------------------------------------
ASSUMED INTERFACES (adapt the small adapter block below if yours differ)
------------------------------------------------------------------------
state_manager:
    get_candidate(email) -> dict | None
    list_candidates(status=None) -> list[dict]
    update_status(email, status)
    update_candidate_fields(email, **fields)
  candidate columns used: name, status, screening_json, evaluation_json,
    emailed_at, scheduled_at, calendar_event_id, calendar_event_link,
    last_reply_id
tools.google_gmail:
    fetch_replies(candidate_email, since_iso=None) -> list[dict]
      each dict: id, subject, from, received_at (ISO 8601)
tools.google_calendar:
    cancel_event(event_id) -> None   (must treat 404/410 "already gone" as success)
"""
from __future__ import annotations

import base64
import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from crewai import Crew, Process, Task

import state_manager as sm
from agents import build_agents
from schemas import (
    CandidateScreeningSchema,
    EvaluationSchema,
    OutreachSchema,
    SchedulingSchema,
)
from tasks import (
    evaluation_task,
    outreach_task,
    scheduling_task,
    screening_task,
)

log = logging.getLogger("hireflow.orchestrator")

EventCb = Optional[Callable[[Dict[str, Any]], None]]


class Status:
    PENDING_SCREENING = "PENDING_SCREENING"
    SCREENED = "SCREENED"
    EMAILED_PENDING_REPLY = "EMAILED_PENDING_REPLY"
    INTERVIEW_SCHEDULED = "INTERVIEW_SCHEDULED"
    RESCHEDULE_REQUESTED = "RESCHEDULE_REQUESTED"
    EVALUATED = "EVALUATED"


# ==========================================================================
# Adapter block — the only place that touches state_manager / Google modules
# ==========================================================================
def _get(email: str) -> Optional[dict]:
    return sm.get_candidate(email)


def _list(status: Optional[str] = None) -> List[dict]:
    return sm.list_candidates(status=status)


def _set_status(email: str, status: str) -> None:
    sm.update_status(email, status)


def _set_fields(email: str, **fields: Any) -> None:
    sm.update_candidate_fields(email, **fields)


def _fetch_replies(email: str, since_iso: Optional[str]) -> List[dict]:
    from tools import google_gmail

    return google_gmail.fetch_replies(email, since_iso=since_iso)


def _cancel_event(event_id: str) -> None:
    from tools import google_calendar

    google_calendar.cancel_event(event_id)


# ==========================================================================
# Helpers
# ==========================================================================
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _emit(cb: EventCb, stage: str, agent: str, message: str, **extra: Any) -> None:
    event = {
        "ts": _now_iso(),
        "stage": stage,
        "agent": agent,
        "message": message,
        **extra,
    }
    log.info("[%s] %s: %s", stage, agent, message)
    if cb:
        try:
            cb(event)
        except Exception:  # a broken listener must never break the pipeline
            log.exception("on_event callback failed")


def _require(email: str) -> dict:
    cand = _get(email)
    if not cand:
        raise ValueError(f"Unknown candidate: {email}")
    return cand


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_output(task: Task, schema: Any) -> Optional[Any]:
    """Pull a validated pydantic object out of a finished task, if any."""
    out = getattr(task, "output", None)
    if out is None:
        return None
    parsed = getattr(out, "pydantic", None)
    if isinstance(parsed, schema):
        return parsed
    raw = getattr(out, "raw", None)
    if raw:
        match = re.search(r"\{.*\}", raw, re.DOTALL)  # tolerate code fences / prose
        if match:
            try:
                return schema.model_validate_json(match.group(0))
            except Exception:
                return None
    return None


def _kickoff(agents_list: list, tasks_list: list) -> None:
    Crew(
        agents=agents_list,
        tasks=tasks_list,
        process=Process.sequential,
        verbose=False,
    ).kickoff()


def _result(ok: bool, email: str, status: str, result: Any = None,
            error: Optional[str] = None, note: Optional[str] = None) -> dict:
    return {
        "ok": ok,
        "candidate_email": email,
        "status": status,
        "result": result.model_dump() if result is not None else None,
        "error": error,
        "note": note,
    }


# --- Gmail reply filtering -------------------------------------------------
# Google Calendar invite responses ("Accepted: ...") can arrive from the
# candidate's own address. They are NOT reschedule requests.
_CAL_NOISE = re.compile(
    r"^(accepted|declined|tentatively accepted|invitation|updated invitation|"
    r"canceled event|cancelled event)\b",
    re.IGNORECASE,
)


def _is_calendar_noise(reply: dict) -> bool:
    subject = (reply.get("subject") or "").strip()
    sender = (reply.get("from") or "").lower()
    return bool(_CAL_NOISE.match(subject)) or "calendar-notification@google.com" in sender


def _latest_real_reply(email: str, since_iso: Optional[str]) -> Optional[dict]:
    try:
        replies = _fetch_replies(email, since_iso)
    except Exception as exc:
        log.warning("Gmail poll failed for %s: %s", email, exc)
        return None

    since = _parse_ts(since_iso)
    floor = datetime.min.replace(tzinfo=timezone.utc)
    real = []
    for reply in replies or []:
        if _is_calendar_noise(reply):
            continue
        ts = _parse_ts(reply.get("received_at"))
        if since and ts and ts <= since:
            continue
        real.append((ts or floor, reply))
    return max(real, key=lambda pair: pair[0])[1] if real else None


def _event_id_from_link(link: Optional[str]) -> Optional[str]:
    """
    Google Calendar htmlLink carries ?eid=<base64("<eventId> <calendarId>")>.
    Used as a fallback when the row has no stored calendar_event_id.
    """
    if not link:
        return None
    try:
        eid = parse_qs(urlparse(link).query).get("eid", [None])[0]
        if not eid:
            return None
        raw = base64.urlsafe_b64decode(eid + "=" * (-len(eid) % 4)).decode()
        return raw.split(" ")[0]
    except Exception:
        return None


# ==========================================================================
# Stages 2 + 3 — Screening, then Outreach if SHORTLIST (one sequential crew)
# ==========================================================================
def run_intake(candidate_email: str, jd_text: str, on_event: EventCb = None) -> dict:
    cand = _require(candidate_email)
    agents = build_agents()
    state: Dict[str, Any] = {"shortlisted": False}

    def on_screened(output) -> None:
        parsed = getattr(output, "pydantic", None)
        if parsed is None:
            return
        _set_fields(candidate_email, screening_json=parsed.model_dump_json())
        _set_status(candidate_email, Status.SCREENED)
        state["shortlisted"] = parsed.screening_decision == "SHORTLIST"
        _emit(on_event, "screening", "Screening Agent",
              f"Scored {candidate_email}: {parsed.match_score}/100 -> {parsed.screening_decision}",
              match_score=parsed.match_score,
              decision=parsed.screening_decision,
              matched_skills=parsed.matched_skills,
              missing_skills=parsed.missing_skills)
        if state["shortlisted"]:
            _emit(on_event, "outreach", "Outreach Agent",
                  f"Drafting a personalised email to {candidate_email}...")

    def on_outreach(output) -> None:
        parsed = getattr(output, "pydantic", None)
        if parsed is None:
            return
        sent = parsed.action_taken.upper().startswith("EMAIL_SENT")
        if sent:
            _set_fields(candidate_email, emailed_at=_now_iso())
            _set_status(candidate_email, Status.EMAILED_PENDING_REPLY)
        _emit(on_event, "outreach", "Outreach Agent",
              "Email sent." if sent else "Email send failed - candidate stays SCREENED.",
              action_taken=parsed.action_taken)

    s_task = screening_task(agents["screening"], candidate_email, jd_text, callback=on_screened)
    o_task = outreach_task(
        agents["outreach"], candidate_email,
        screening_task=s_task,
        candidate_name=cand.get("name"),
        callback=on_outreach,
    )

    _emit(on_event, "screening", "Screening Agent", f"Reading resume for {candidate_email}...")
    try:
        _kickoff([agents["screening"], agents["outreach"]], [s_task, o_task])
    except Exception as exc:
        log.exception("Intake crew failed for %s", candidate_email)
        _emit(on_event, "error", "Orchestrator", f"Intake failed: {exc}")
        return _result(False, candidate_email, (_get(candidate_email) or {}).get("status", ""),
                       error=str(exc))

    screening = _parse_output(s_task, CandidateScreeningSchema)
    if screening is None:
        _emit(on_event, "error", "Screening Agent", "No valid screening output was produced.")
        return _result(False, candidate_email, Status.PENDING_SCREENING,
                       error="Screening produced no valid output")

    final_status = (_get(candidate_email) or {}).get("status", Status.SCREENED)
    return _result(True, candidate_email, final_status, screening)


def run_outreach_only(candidate_email: str, on_event: EventCb = None) -> dict:
    """Retry a failed send for a SCREENED + SHORTLIST candidate (no re-screening)."""
    import json

    cand = _require(candidate_email)
    if not cand.get("screening_json"):
        return _result(False, candidate_email, cand["status"], error="No screening result on file")
    screening = CandidateScreeningSchema.model_validate(json.loads(cand["screening_json"]))
    if screening.screening_decision != "SHORTLIST":
        return _result(True, candidate_email, cand["status"], note="Candidate was not shortlisted")

    agents = build_agents()
    task = outreach_task(agents["outreach"], candidate_email,
                         screening=screening, candidate_name=cand.get("name"))
    _emit(on_event, "outreach", "Outreach Agent", f"Drafting a personalised email to {candidate_email}...")
    try:
        _kickoff([agents["outreach"]], [task])
    except Exception as exc:
        _emit(on_event, "error", "Outreach Agent", f"Outreach failed: {exc}")
        return _result(False, candidate_email, cand["status"], error=str(exc))

    out = _parse_output(task, OutreachSchema)
    if out and out.action_taken.upper().startswith("EMAIL_SENT"):
        _set_fields(candidate_email, emailed_at=_now_iso())
        _set_status(candidate_email, Status.EMAILED_PENDING_REPLY)
        _emit(on_event, "outreach", "Outreach Agent", "Email sent.")
        return _result(True, candidate_email, Status.EMAILED_PENDING_REPLY, out)
    _emit(on_event, "error", "Outreach Agent", "Email send failed.")
    return _result(False, candidate_email, cand["status"], out, error="Send failed")


# ==========================================================================
# Stage 4 — Scheduling (initial booking AND reschedule; Scheduling Agent only)
# ==========================================================================
def run_scheduling(candidate_email: str, on_event: EventCb = None,
                   reschedule: bool = False) -> dict:
    cand = _require(candidate_email)
    since = cand.get("scheduled_at") if reschedule else cand.get("emailed_at")
    reply = _latest_real_reply(candidate_email, since)
    fallback_status = cand["status"]

    if reply is None:
        return _result(True, candidate_email, fallback_status, note="No new reply to process")

    # --- Reschedule step 1: cancel the old event deterministically (no LLM) ---
    if reschedule:
        old_id = cand.get("calendar_event_id") or _event_id_from_link(cand.get("calendar_event_link"))
        if old_id:
            try:
                _cancel_event(old_id)
                _emit(on_event, "scheduling", "Scheduling Agent",
                      f"Cancelled the previous interview event for {candidate_email}.")
            except Exception as exc:
                # Abort rather than risk a double-booking. Status stays
                # RESCHEDULE_REQUESTED so the next poll retries cleanly.
                log.exception("Could not cancel old event %s", old_id)
                _emit(on_event, "error", "Scheduling Agent", f"Could not cancel old event: {exc}")
                return _result(False, candidate_email, Status.RESCHEDULE_REQUESTED,
                               error=f"Cancel failed: {exc}")
        else:
            _emit(on_event, "scheduling", "Scheduling Agent",
                  "No previous event id on file; booking a new slot anyway.")

    # --- Step 2: Scheduling Agent books the new slot ---
    agents = build_agents()
    task = scheduling_task(
        agents["scheduling"], candidate_email,
        now_iso=_now_iso(), reschedule=reschedule,
        candidate_name=cand.get("name"), role_title=cand.get("role_title"),
    )
    _emit(on_event, "scheduling", "Scheduling Agent",
          f"Reading {candidate_email}'s reply and checking calendar availability...")
    try:
        _kickoff([agents["scheduling"]], [task])
    except Exception as exc:
        log.exception("Scheduling crew failed for %s", candidate_email)
        _emit(on_event, "error", "Scheduling Agent", f"Scheduling failed: {exc}")
        return _result(False, candidate_email, fallback_status, error=str(exc))

    out = _parse_output(task, SchedulingSchema)
    if out is None:
        _emit(on_event, "error", "Scheduling Agent", "No valid scheduling output was produced.")
        return _result(False, candidate_email, fallback_status, error="No valid output")

    out.candidate_email = candidate_email

    if out.status.upper() == "SCHEDULED" and out.calendar_event_link:
        _set_fields(
            candidate_email,
            calendar_event_link=out.calendar_event_link,
            calendar_event_id=_event_id_from_link(out.calendar_event_link),
            scheduled_at=_now_iso(),
            last_reply_id=reply.get("id"),
        )
        _set_status(candidate_email, Status.INTERVIEW_SCHEDULED)
        _emit(on_event, "scheduling", "Scheduling Agent",
              f"Interview booked for {out.agreed_timestamp}.",
              agreed_timestamp=out.agreed_timestamp,
              calendar_event_link=out.calendar_event_link)
        return _result(True, candidate_email, Status.INTERVIEW_SCHEDULED, out)

    # NO_REPLY / NEEDS_CLARIFICATION / NO_SLOT: remember we handled this reply
    # so the poller doesn't re-run the agent on it every tick. Status is left
    # alone (a failed reschedule stays RESCHEDULE_REQUESTED).
    if out.status.upper() != "NO_REPLY":
        _set_fields(candidate_email, last_reply_id=reply.get("id"))
    _emit(on_event, "scheduling", "Scheduling Agent",
          f"Not scheduled yet: {out.status}.", status=out.status)
    return _result(True, candidate_email, fallback_status, out, note=out.status)


# ==========================================================================
# Stage 5 — Evaluator
# ==========================================================================
def run_evaluation(candidate_email: str, jd_text: str, interview_notes: str,
                   on_event: EventCb = None) -> dict:
    cand = _require(candidate_email)
    if not cand.get("screening_json"):
        return _result(False, candidate_email, cand["status"],
                       error="No screening result on file; run screening first")

    agents = build_agents()
    task = evaluation_task(
        agents["evaluator"], candidate_email, jd_text,
        cand["screening_json"], interview_notes,
    )
    _emit(on_event, "evaluation", "Evaluator Agent",
          f"Weighing screening results against interview notes for {candidate_email}...")
    try:
        _kickoff([agents["evaluator"]], [task])
    except Exception as exc:
        log.exception("Evaluation crew failed for %s", candidate_email)
        _emit(on_event, "error", "Evaluator Agent", f"Evaluation failed: {exc}")
        return _result(False, candidate_email, cand["status"], error=str(exc))

    out = _parse_output(task, EvaluationSchema)
    if out is None:
        _emit(on_event, "error", "Evaluator Agent", "No valid evaluation output was produced.")
        return _result(False, candidate_email, cand["status"], error="No valid output")

    out.candidate_email = candidate_email
    _set_fields(candidate_email, evaluation_json=out.model_dump_json())
    _set_status(candidate_email, Status.EVALUATED)
    _emit(on_event, "evaluation", "Evaluator Agent",
          f"Recommendation: {out.recommendation}",
          recommendation=out.recommendation, summary=out.summary)
    return _result(True, candidate_email, Status.EVALUATED, out)


# ==========================================================================
# Routing
# ==========================================================================
def process_candidate(candidate_email: str, jd_text: str = "",
                      on_event: EventCb = None) -> dict:
    """
    Route ONE candidate by their SQLite status.

        PENDING_SCREENING     -> screening (+ outreach if SHORTLIST)
        SCREENED              -> retry outreach if shortlisted
        EMAILED_PENDING_REPLY -> scheduling (only if a new reply exists)
        RESCHEDULE_REQUESTED  -> scheduling ONLY, reschedule mode
        INTERVIEW_SCHEDULED / EVALUATED -> nothing to do
    """
    cand = _require(candidate_email)
    status = cand["status"]

    if status == Status.RESCHEDULE_REQUESTED:
        return run_scheduling(candidate_email, on_event, reschedule=True)
    if status == Status.PENDING_SCREENING:
        if not jd_text:
            return _result(False, candidate_email, status, error="jd_text is required for screening")
        return run_intake(candidate_email, jd_text, on_event)
    if status == Status.SCREENED:
        return run_outreach_only(candidate_email, on_event)
    if status == Status.EMAILED_PENDING_REPLY:
        return run_scheduling(candidate_email, on_event, reschedule=False)
    return _result(True, candidate_email, status, note="Nothing to do in this state")


def poll_and_route(on_event: EventCb = None) -> dict:
    """
    One polling tick (call it from a scheduler or a /poll endpoint).

    Order matters:
      1. Pure-Python Gmail check on INTERVIEW_SCHEDULED candidates; any new
         real reply flips status to RESCHEDULE_REQUESTED. No agent runs yet.
      2. RESCHEDULE_REQUESTED candidates -> Scheduling Agent only.
      3. EMAILED_PENDING_REPLY candidates with a new reply -> Scheduling Agent.
    """
    summary: Dict[str, list] = {"reschedule_flagged": [], "processed": [], "errors": []}

    # 1) State flip BEFORE any agent runs
    for cand in _list(Status.INTERVIEW_SCHEDULED):
        email = cand["email"] if "email" in cand else cand["candidate_email"]
        reply = _latest_real_reply(email, cand.get("scheduled_at"))
        if reply and reply.get("id") != cand.get("last_reply_id"):
            _set_status(email, Status.RESCHEDULE_REQUESTED)
            summary["reschedule_flagged"].append(email)
            _emit(on_event, "poll", "Orchestrator",
                  f"{email} replied after being scheduled -> RESCHEDULE_REQUESTED")

    # 2) + 3) Route by state
    for status in (Status.RESCHEDULE_REQUESTED, Status.EMAILED_PENDING_REPLY):
        for cand in _list(status):
            email = cand["email"] if "email" in cand else cand["candidate_email"]
            res = run_scheduling(email, on_event, reschedule=(status == Status.RESCHEDULE_REQUESTED))
            (summary["processed"] if res["ok"] else summary["errors"]).append(res)

    return summary
