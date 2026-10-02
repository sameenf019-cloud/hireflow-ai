"""Google Calendar tools for the Scheduling Agent: FreeBusy check, Meet-enabled event insert,
and cancel (used by the reschedule edge case).

Plain functions (get_busy, is_slot_free, create_event, cancel_event, book_interview) hold the
logic; the CrewAI tools are thin wrappers. In mock mode (GOOGLE_MOCK_MODE=true, default) events
live in data/mock_calendar.json - use `mock_add_busy()` to simulate a conflicting meeting.

CLI (run from backend/):  python -m tools.google_calendar selftest
"""
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Optional, Type

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

import state_manager as db
from config import settings
from tools.google_gmail import JsonStore, _build_service

_mock_cal = JsonStore("mock_calendar.json", lambda: {"events": [], "busy": []})

# ================================================================== time helpers


def _tz() -> tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(settings.timezone)
    except Exception:
        return timezone.utc


def parse_iso(value: str) -> datetime:
    """Parse ISO 8601 (accepts a trailing Z). Naive values are read in the configured TIMEZONE."""
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=_tz())


Interval = tuple[datetime, datetime]

# ==================================================================== plain functions


def mock_add_busy(start_iso: str, end_iso: str) -> None:
    """Simulate an existing meeting on the recruiter's calendar (tests / demo)."""
    _mock_cal.update(lambda d: d["busy"].append({"start": parse_iso(start_iso).isoformat(), "end": parse_iso(end_iso).isoformat()}))


def get_busy(start: datetime, end: datetime) -> list[Interval]:
    """Busy intervals overlapping [start, end) via the Calendar FreeBusy API."""
    if settings.google_mock_mode:
        data = _mock_cal.read()
        raw = [(e["start"], e["end"]) for e in data["events"] if e["status"] == "confirmed"]
        raw += [(b["start"], b["end"]) for b in data["busy"]]
        blocks = [(parse_iso(s), parse_iso(e)) for s, e in raw]
    else:
        body = {
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "timeZone": settings.timezone,
            "items": [{"id": settings.calendar_id}],
        }
        resp = _build_service("calendar", "v3").freebusy().query(body=body).execute()
        busy = resp["calendars"][settings.calendar_id].get("busy", [])
        blocks = [(parse_iso(b["start"]), parse_iso(b["end"])) for b in busy]
    return sorted((s, e) for s, e in blocks if s < end and e > start)


def is_slot_free(start: datetime, end: datetime, ignore: Optional[Interval] = None) -> tuple[bool, list[Interval]]:
    """Free iff no busy block overlaps. Blocks lying fully inside `ignore` (the candidate's own
    existing interview, when rescheduling) don't count as conflicts."""
    conflicts = get_busy(start, end)
    if ignore:
        conflicts = [(s, e) for s, e in conflicts if not (s >= ignore[0] and e <= ignore[1])]
    return (not conflicts), conflicts


def create_event(
    summary: str, description: str, start: datetime, end: datetime, attendees: list[str]
) -> dict[str, Optional[str]]:
    """Insert a calendar event with a Google Meet link and invite attendees."""
    if settings.google_mock_mode:
        event_id = f"mock-evt-{uuid.uuid4().hex[:8]}"
        meet = f"https://meet.google.com/mock-{uuid.uuid4().hex[:3]}-{uuid.uuid4().hex[:4]}-{uuid.uuid4().hex[:3]}"
        link = f"https://calendar.google.com/mock/event/{event_id}"
        _mock_cal.update(
            lambda d: d["events"].append(
                {
                    "id": event_id, "summary": summary, "description": description,
                    "start": start.isoformat(), "end": end.isoformat(), "attendees": attendees,
                    "status": "confirmed", "html_link": link, "meet_link": meet,
                }
            )
        )
        return {"event_id": event_id, "event_link": link, "meet_link": meet}

    body = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start.isoformat(), "timeZone": settings.timezone},
        "end": {"dateTime": end.isoformat(), "timeZone": settings.timezone},
        "attendees": [{"email": a} for a in attendees if a],
        "conferenceData": {
            "createRequest": {"requestId": uuid.uuid4().hex, "conferenceSolutionKey": {"type": "hangoutsMeet"}}
        },
    }
    ev = (
        _build_service("calendar", "v3").events()
        .insert(calendarId=settings.calendar_id, body=body, conferenceDataVersion=1, sendUpdates="all")
        .execute()
    )
    return {"event_id": ev["id"], "event_link": ev.get("htmlLink"), "meet_link": ev.get("hangoutLink")}


def cancel_event(event_id: str) -> bool:
    """Cancel an event and notify attendees. Returns False if it no longer exists."""
    if settings.google_mock_mode:
        def fn(d: dict) -> bool:
            for e in d["events"]:
                if e["id"] == event_id and e["status"] == "confirmed":
                    e["status"] = "cancelled"
                    return True
            return False

        return _mock_cal.update(fn)

    from googleapiclient.errors import HttpError

    try:
        _build_service("calendar", "v3").events().delete(
            calendarId=settings.calendar_id, eventId=event_id, sendUpdates="all"
        ).execute()
        return True
    except HttpError as exc:
        if exc.resp.status in (404, 410):  # already gone / already cancelled
            return False
        raise


def book_interview(candidate_id: int, start_iso: str, duration_minutes: Optional[int] = None) -> dict[str, Any]:
    """Book (or move) the interview for a candidate.

    Safe by construction: re-checks FreeBusy itself, refuses past times, and - if the candidate
    already has an event (reschedule) - books the NEW event first and only then cancels the old one,
    so a failure never leaves the candidate with no interview. Persists event id/link to SQLite.
    """
    cand = db.get_candidate(candidate_id)
    if not cand:
        return {"ok": False, "reason": f"Candidate {candidate_id} not found"}
    try:
        start = parse_iso(start_iso)
    except ValueError:
        return {"ok": False, "reason": f"'{start_iso}' is not a valid ISO 8601 timestamp"}
    duration = timedelta(minutes=duration_minutes or settings.interview_duration_minutes)
    end = start + duration
    if start <= datetime.now(timezone.utc):
        return {"ok": False, "reason": "That time is in the past"}

    old_event_id = cand.get("calendar_event_id")
    ignore: Optional[Interval] = None
    if old_event_id and cand.get("agreed_timestamp"):
        old_start = parse_iso(cand["agreed_timestamp"])
        ignore = (old_start, old_start + duration)

    free, conflicts = is_slot_free(start, end, ignore)
    if not free:
        return {
            "ok": False,
            "reason": "Slot is busy",
            "busy": [{"start": s.isoformat(), "end": e.isoformat()} for s, e in conflicts],
        }

    job = db.get_job(cand["job_id"]) or {}
    who = cand.get("name") or cand["email"]
    created = create_event(
        summary=f"Interview: {who}" + (f" - {job['title']}" if job.get("title") else ""),
        description=f"Interview with {who} ({cand['email']}).",
        start=start,
        end=end,
        attendees=[cand["email"], settings.recruiter_email],
    )
    db.set_calendar_event(candidate_id, created["event_id"], created["event_link"] or "", start.isoformat())

    replaced = None
    if old_event_id and old_event_id != created["event_id"]:
        cancel_event(old_event_id)
        replaced = old_event_id
        db.log_event("Scheduling Agent", f"Cancelled old interview slot for {who}", candidate_id)
    db.log_event("Scheduling Agent", f"Booked {who} for {start.strftime('%a %d %b %H:%M %Z')}", candidate_id)
    return {"ok": True, "agreed_timestamp": start.isoformat(), "replaced_event_id": replaced, **created}


# ==================================================================== CrewAI tools


class FreeBusyInput(BaseModel):
    start_iso: str = Field(..., description="Window start, ISO 8601 (e.g. 2026-10-06T14:00:00+05:00).")
    end_iso: str = Field(..., description="Window end, ISO 8601. For a 30-minute interview, start + 30 min.")


class CalendarFreeBusyTool(BaseTool):
    name: str = "Calendar_FreeBusy_Tool"
    description: str = (
        "Checks the recruiter's Google Calendar FreeBusy for a time window. Returns JSON: "
        "free (true/false) and the busy intervals. Always call this before booking."
    )
    args_schema: Type[BaseModel] = FreeBusyInput

    def _run(self, start_iso: str, end_iso: str) -> str:
        try:
            start, end = parse_iso(start_iso), parse_iso(end_iso)
            if end <= start:
                return json.dumps({"error": "end must be after start"})
            free, busy = is_slot_free(start, end)
            db.log_event("Scheduling Agent", f"Checking calendar {start.strftime('%a %d %b %H:%M')}: {'free' if free else 'busy'}")
            return json.dumps({"free": free, "busy": [{"start": s.isoformat(), "end": e.isoformat()} for s, e in busy]})
        except Exception as exc:
            return json.dumps({"error": str(exc)})


class BookInput(BaseModel):
    candidate_id: int = Field(..., description="Database id of the candidate being interviewed.")
    start_iso: str = Field(..., description="Interview start, ISO 8601 with UTC offset.")


class CalendarBookTool(BaseTool):
    name: str = "Calendar_Book_Tool"
    description: str = (
        "Books the interview: creates a Google Calendar event with a Meet link and invites the "
        "candidate. If the candidate already has an interview booked (reschedule), the old event is "
        "cancelled automatically. Returns JSON with ok, agreed_timestamp, event_link, meet_link; or "
        "ok=false with a reason (e.g. slot busy) so you can propose another time."
    )
    args_schema: Type[BaseModel] = BookInput

    def _run(self, candidate_id: int, start_iso: str) -> str:
        try:
            return json.dumps(book_interview(candidate_id, start_iso))
        except Exception as exc:
            return json.dumps({"ok": False, "reason": str(exc)})


# ==================================================================== CLI


def _selftest() -> None:
    if not settings.google_mock_mode:
        sys.exit("selftest runs in mock mode only (set GOOGLE_MOCK_MODE=true).")
    start = (datetime.now(timezone.utc) + timedelta(days=3)).replace(hour=10, minute=0, second=0, microsecond=0)
    end = start + timedelta(minutes=30)
    assert is_slot_free(start, end)[0]
    ev = create_event("selftest", "", start, end, ["selftest@example.com"])
    assert not is_slot_free(start, end)[0]
    assert cancel_event(ev["event_id"]) and is_slot_free(start, end)[0]
    print("calendar mock selftest OK ->", ev["meet_link"])


if __name__ == "__main__":
    _selftest()
