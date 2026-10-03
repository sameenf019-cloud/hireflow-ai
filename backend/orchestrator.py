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

Outreach: the Outreach agent only WRITES the email. This module sends it with
plain Python (tools.google_gmail.send_email), so "EMAIL_SENT" is only recorded
when the send really succeeded.

Scheduling: the Scheduling agent only READS the candidate's reply and
proposes time(s) as plain JSON text (no tools, and NO output_pydantic - CrewAI's
pydantic converter uses instructor tool-calling, which Groq models fail with
"Tool choice is required, but model did not call a tool"). This module parses
that JSON itself (_parse_proposal), checks FreeBusy and books the event with
plain Python (tools.google_calendar), bypassing google_calendar.book_interview()
because it calls a state_manager.set_calendar_event() function that does not
exist; the actual SQLite write happens here via _set_fields/_set_status instead.

All run_* functions are blocking (CrewAI kickoff is sync). In FastAPI call
them via asyncio.to_thread / BackgroundTasks. Pass on_event to receive
live progress dicts for the frontend agent-handoff visualizer.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime, timedelta, timezone
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
    SchedulingProposalSchema,
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
    return sm.get_candidate_by_email(email)


def _list(status: Optional[str] = None) -> List[dict]:
    return sm.list_candidates(status=status)


def _set_status(email: str, status: str) -> None:
    cand = _require(email)
    sm.update_status(cand["id"], status, force=True)


def _set_fields(email: str, **fields: Any) -> None:
    cand = _require(email)
    sm.update_candidate_fields(cand["id"], **fields)


def _fetch_replies(email: str, since_iso: Optional[str]) -> List[dict]:
    from tools import google_gmail

    # Real function is fetch_candidate_messages(email) — no since_iso param.
    # Filtering old replies already happens in _latest_real_reply using each
    # reply's "received_at" field.
    return google_gmail.fetch_candidate_messages(email)


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


def _parse_proposal(task: Task, candidate_email: str) -> Optional[SchedulingProposalSchema]:
    """
    Parse the Scheduling agent's raw text ourselves. The scheduling task has no
    output_pydantic, so CrewAI/instructor never gets involved (that was the
    source of the Groq "Tool choice is required" error).

    Status is derived from proposed_times, not trusted from the model.
    """
    out = getattr(task, "output", None)
    raw = (getattr(out, "raw", "") or "").strip()
    log.info("Scheduling agent raw output: %s", raw[:600])

    match = re.search(r"\{.*\}", raw, re.DOTALL)  # tolerate code fences / prose
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        log.warning("Scheduling agent output was not valid JSON: %s", raw[:300])
        return None
    if not isinstance(data, dict):
        return None

    times = data.get("proposed_times") or []
    if isinstance(times, str):
        times = [times]
    times = [str(t).strip() for t in times if t and str(t).strip()]

    return SchedulingProposalSchema(
        candidate_email=candidate_email,
        proposed_times=times,
        status="HAS_TIMES" if times else "NEEDS_CLARIFICATION",
    )


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


def _send_outreach_email(candidate_email: str, email_draft: str) -> bool:
    """
    Send the email the Outreach agent wrote, using plain Python (no LLM tool call).
    The draft's first line is "Subject: ...", then a blank line, then the body.
    Returns True only if the send really succeeded.
    """
    from tools import google_gmail

    cand = _require(candidate_email)
    address = cand.get("email") or candidate_email
    if address.endswith(".invalid"):
        log.warning("No valid email address on file for %s", candidate_email)
        return False

    draft = (email_draft or "").strip()
    subject = "Interview invitation"
    body = draft
    match = re.match(r"^\s*subject:\s*(.+?)\s*\n+(.*)$", draft, re.IGNORECASE | re.DOTALL)
    if match:
        subject, body = match.group(1).strip(), match.group(2).strip()
    if not body:
        log.warning("Outreach draft was empty for %s", candidate_email)
        return False

    try:
        result = google_gmail.send_email(address, subject, body, thread_id=cand.get("gmail_thread_id"))
        # state_manager has no set_gmail_thread_id function.
        # update_candidate_fields is the real function that stores it.
        sm.update_candidate_fields(cand["id"], gmail_thread_id=result["thread_id"])
        sm.log_event("Outreach Agent", f"Emailed {cand.get('name') or address}", cand["id"])
        return True
    except Exception:
        log.exception("Sending outreach email to %s failed", address)
        return False


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
        _set_fields(candidate_email, screening_json=parsed.model_dump_json(),
                    match_score=parsed.match_score)
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
        sent = _send_outreach_email(candidate_email, parsed.email_draft)
        if sent:
            _set_fields(candidate_email, emailed_at=_now_iso())
            _set_status(candidate_email, Status.EMAILED_PENDING_REPLY)
        _emit(on_event, "outreach", "Outreach Agent",
              "Email sent." if sent else "Email send failed - candidate stays SCREENED.",
              action_taken="EMAIL_SENT" if sent else "SEND_FAILED")

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
    cand = _require(candidate_email)
    if not cand.get("screening_json"):
        return _result(False, candidate_email, cand["status"], error="No screening result on file")
    raw_screening = cand["screening_json"]  # state_manager already returns it parsed
    screening = CandidateScreeningSchema.model_validate(
        json.loads(raw_screening) if isinstance(raw_screening, str) else raw_screening
    )
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
    if out and _send_outreach_email(candidate_email, out.email_draft):
        _set_fields(candidate_email, emailed_at=_now_iso())
        _set_status(candidate_email, Status.EMAILED_PENDING_REPLY)
        _emit(on_event, "outreach", "Outreach Agent", "Email sent.")
        return _result(True, candidate_email, Status.EMAILED_PENDING_REPLY, out)
    _emit(on_event, "error", "Outreach Agent", "Email send failed.")
    return _result(False, candidate_email, cand["status"], out, error="Send failed")


# ==========================================================================
# Stage 4 — Scheduling (initial booking AND reschedule; Scheduling Agent only
# extracts proposed times; FreeBusy + booking happen here in plain Python)
# ==========================================================================
def run_scheduling(candidate_email: str, on_event: EventCb = None,
                   reschedule: bool = False) -> dict:
    cand = _require(candidate_email)
    since = cand.get("scheduled_at") if reschedule else cand.get("emailed_at")
    reply = _latest_real_reply(candidate_email, since)
    fallback_status = cand["status"]

    if reply is None:
        return _result(True, candidate_email, fallback_status, note="No new reply to process")

    # DEBUG: confirms the reply really has a body the agent can read.
    log.info("Reply for %s: keys=%s subject=%r body_len=%d",
             candidate_email, list(reply.keys()), reply.get("subject"),
             len(reply.get("body") or ""))

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

    # --- Step 2: Scheduling Agent reads the reply and proposes time(s). No
    # tools and no output_pydantic, so neither the Groq tool-call error nor
    # the instructor converter error can happen here. ---
    agents = build_agents()
    task = scheduling_task(
        agents["scheduling"], candidate_email,
        reply_body=reply.get("body", ""),
        now_iso=_now_iso(), reschedule=reschedule,
        candidate_name=cand.get("name"),
    )
    _emit(on_event, "scheduling", "Scheduling Agent",
          f"Reading {candidate_email}'s reply and checking calendar availability...")
    try:
        _kickoff([agents["scheduling"]], [task])
    except Exception as exc:
        log.exception("Scheduling crew failed for %s", candidate_email)
        _emit(on_event, "error", "Scheduling Agent", f"Scheduling failed: {exc}")
        return _result(False, candidate_email, fallback_status, error=str(exc))

    proposal = _parse_proposal(task, candidate_email)
    if proposal is None:
        _emit(on_event, "error", "Scheduling Agent", "No valid scheduling output was produced.")
        return _result(False, candidate_email, fallback_status, error="No valid output")

    valid_times: List[datetime] = []
    if proposal.status == "HAS_TIMES":
        from tools import google_calendar

        for t in proposal.proposed_times:
            try:
                dt = google_calendar.parse_iso(t)
            except ValueError:
                log.warning("Could not parse proposed time %r", t)
                continue
            if dt > datetime.now(timezone.utc):
                valid_times.append(dt)
            else:
                log.info("Ignoring proposed time in the past: %s", t)

    if not valid_times:
        out = SchedulingSchema(candidate_email=candidate_email, agreed_timestamp="",
                               calendar_event_link="", status="NEEDS_CLARIFICATION")
        _set_fields(candidate_email, last_reply_id=reply.get("id"))
        _emit(on_event, "scheduling", "Scheduling Agent",
              "Not scheduled yet: NEEDS_CLARIFICATION.", status="NEEDS_CLARIFICATION")
        return _result(True, candidate_email, fallback_status, out, note="NEEDS_CLARIFICATION")

    # --- Step 3: plain Python checks FreeBusy and books the first free slot
    # (no LLM tool call - this is the deterministic part that broke before) ---
    from config import settings
    from tools import google_calendar

    booking: Optional[dict] = None
    for start in valid_times:
        end = start + timedelta(minutes=settings.interview_duration_minutes)
        free, _conflicts = google_calendar.is_slot_free(start, end)
        if free:
            who = cand.get("name") or candidate_email
            title = f"Interview: {who}"
            if cand.get("role_title"):
                title += f" - {cand['role_title']}"
            created = google_calendar.create_event(
                summary=title,
                description=f"Interview with {who} ({candidate_email}).",
                start=start, end=end,
                attendees=[candidate_email],
            )
            booking = {"agreed_timestamp": start.isoformat(), **created}
            break

    if booking is None:
        out = SchedulingSchema(candidate_email=candidate_email, agreed_timestamp="",
                               calendar_event_link="", status="NO_SLOT")
        _set_fields(candidate_email, last_reply_id=reply.get("id"))
        _emit(on_event, "scheduling", "Scheduling Agent",
              "Not scheduled yet: NO_SLOT.", status="NO_SLOT")
        return _result(True, candidate_email, fallback_status, out, note="NO_SLOT")

    out = SchedulingSchema(
        candidate_email=candidate_email,
        agreed_timestamp=booking["agreed_timestamp"],
        calendar_event_link=booking.get("event_link") or "",
        status="SCHEDULED",
    )
    _set_fields(
        candidate_email,
        calendar_event_link=out.calendar_event_link,
        calendar_event_id=booking.get("event_id"),
        scheduled_at=_now_iso(),
        last_reply_id=reply.get("id"),
    )
    _set_status(candidate_email, Status.INTERVIEW_SCHEDULED)
    _emit(on_event, "scheduling", "Scheduling Agent",
          f"Interview booked for {out.agreed_timestamp}.",
          agreed_timestamp=out.agreed_timestamp,
          calendar_event_link=out.calendar_event_link)
    return _result(True, candidate_email, Status.INTERVIEW_SCHEDULED, out)


# ==========================================================================
# Stage 5 — Evaluator
# ==========================================================================
def run_evaluation(candidate_email: str, jd_text: str, interview_notes: str,
                   on_event: EventCb = None) -> dict:
    cand = _require(candidate_email)
    if not cand.get("screening_json"):
        return _result(False, candidate_email, cand["status"],
                       error="No screening result on file; run screening first")

    screening_json = cand["screening_json"]
    if not isinstance(screening_json, str):  # state_manager returns it already parsed
        screening_json = json.dumps(screening_json)

    agents = build_agents()
    task = evaluation_task(
        agents["evaluator"], candidate_email, jd_text,
        screening_json, interview_notes,
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