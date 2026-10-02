"""Gmail tools for the Outreach and Scheduling agents.

Layers:
  * plain functions (send_email, fetch_candidate_messages, poll_for_replies) - testable, no CrewAI
  * CrewAI tools (Gmail_Send_Tool, Gmail_Read_Tool) - thin wrappers around those functions

Mock mode (GOOGLE_MOCK_MODE=true, the default) stores a fake mailbox in data/mock_gmail.json so the
whole pipeline - including the reschedule edge case - runs with no Google credentials.
Use `mock_inject_reply()` to simulate a candidate replying.

This module also hosts the helpers shared with google_calendar.py (OAuth + the JSON mock store).

CLI (run from backend/):
    python -m tools.google_gmail auth          # one-time OAuth consent (real mode)
    python -m tools.google_gmail selftest      # mock round trip
    python -m tools.google_gmail reset-mock    # delete mock mailbox + mock calendar files
"""
import base64
import json
import re
import sys
import threading
import uuid
from datetime import datetime, timezone
from email.mime.text import MIMEText
from typing import Any, Callable, Optional, Type

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

import state_manager as db
from config import settings
from schemas import CandidateStatus

# ============================================================ shared: OAuth + mock store


def get_google_credentials(interactive: bool = False):
    """Load OAuth credentials from token.json (refreshing if needed). The browser consent flow only
    runs when interactive=True (CLI `auth`), never from inside the API server."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    scopes = list(settings.google_scopes)
    creds = None
    if settings.google_token_file.exists():
        creds = Credentials.from_authorized_user_file(str(settings.google_token_file), scopes)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        if not interactive:
            raise RuntimeError(
                "Google is not authorised yet. Run `python -m tools.google_gmail auth` from backend/, "
                "or set GOOGLE_MOCK_MODE=true."
            )
        from google_auth_oauthlib.flow import InstalledAppFlow

        if not settings.google_credentials_file.exists():
            raise FileNotFoundError(f"OAuth client file not found: {settings.google_credentials_file}")
        flow = InstalledAppFlow.from_client_secrets_file(str(settings.google_credentials_file), scopes)
        creds = flow.run_local_server(port=0)
    settings.google_token_file.write_text(creds.to_json())
    return creds


def _build_service(name: str, version: str):
    from googleapiclient.discovery import build

    # A fresh service per call: googleapiclient/httplib2 objects are not thread-safe.
    return build(name, version, credentials=get_google_credentials(), cache_discovery=False)


_mock_lock = threading.RLock()


class JsonStore:
    """Tiny JSON-file store backing the mock Gmail/Calendar (lives next to the SQLite file)."""

    def __init__(self, filename: str, default_factory: Callable[[], dict]):
        self.filename = filename
        self.default_factory = default_factory

    @property
    def path(self):
        return settings.sqlite_path.parent / self.filename

    def read(self) -> dict:
        with _mock_lock:
            if self.path.exists():
                return json.loads(self.path.read_text())
            return self.default_factory()

    def update(self, fn: Callable[[dict], Any]) -> Any:
        with _mock_lock:
            settings.ensure_dirs()
            data = self.read()
            result = fn(data)
            self.path.write_text(json.dumps(data, indent=2))
            return result

    def delete(self) -> None:
        with _mock_lock:
            if self.path.exists():
                self.path.unlink()


# ============================================================================ mock mailbox

_mock_mail = JsonStore("mock_gmail.json", lambda: {"messages": []})


def _utc_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _mock_add(
    direction: str, sender: str, to: str, subject: str, body: str, thread_id: Optional[str] = None
) -> dict[str, str]:
    def fn(data: dict) -> dict[str, str]:
        msgs = data["messages"]
        ts = _utc_ms()
        if msgs:
            ts = max(ts, msgs[-1]["internal_ts"] + 1)  # strictly increasing -> reliable ordering
        msg = {
            "id": f"mock-msg-{len(msgs) + 1}",
            "thread_id": thread_id or f"mock-thread-{uuid.uuid4().hex[:8]}",
            "direction": direction,
            "from": sender,
            "to": to,
            "subject": subject,
            "body": body,
            "internal_ts": ts,
        }
        msgs.append(msg)
        return {"message_id": msg["id"], "thread_id": msg["thread_id"]}

    return _mock_mail.update(fn)


def mock_inject_reply(
    candidate_email: str, body: str, thread_id: Optional[str] = None, subject: str = "Re: Interview invitation"
) -> dict[str, str]:
    """Simulate the candidate replying (tests / demo). Defaults to the latest thread we mailed them on."""
    email = candidate_email.strip().lower()
    if thread_id is None:
        sent = [m for m in _mock_mail.read()["messages"] if m["direction"] == "out" and m["to"].lower() == email]
        if not sent:
            raise ValueError(f"No outbound mock email to {email}; send outreach first.")
        thread_id = sent[-1]["thread_id"]
    return _mock_add("in", email, settings.recruiter_email or "recruiter@mock.local", subject, body, thread_id)


# ============================================================================ plain functions


def send_email(to: str, subject: str, body: str, thread_id: Optional[str] = None) -> dict[str, str]:
    """Send an email (optionally into an existing thread). Returns {message_id, thread_id}."""
    if settings.google_mock_mode:
        return _mock_add("out", settings.recruiter_email or "recruiter@mock.local", to, subject, body, thread_id)

    message = MIMEText(body)
    message["to"] = to
    message["subject"] = subject
    if settings.recruiter_email:
        message["from"] = settings.recruiter_email
    payload: dict[str, Any] = {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode()}
    if thread_id:
        payload["threadId"] = thread_id
    sent = _build_service("gmail", "v1").users().messages().send(userId="me", body=payload).execute()
    return {"message_id": sent["id"], "thread_id": sent["threadId"]}


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="ignore")


def _extract_body(payload: dict) -> str:
    """Recursively pull text out of a Gmail payload, preferring text/plain over HTML."""
    mime = payload.get("mimeType", "")
    data = payload.get("body", {}).get("data")
    if mime == "text/plain" and data:
        return _decode(data)
    for part in payload.get("parts") or []:
        text = _extract_body(part)
        if text:
            return text
    if mime == "text/html" and data:
        return re.sub(r"<[^>]+>", " ", _decode(data))
    return ""


def clean_reply_body(text: str) -> str:
    """Drop quoted history so the agent only reads what the candidate newly wrote."""
    text = re.split(r"\n\s*On [^\n]{0,200}(?:\n[^\n]{0,100})?wrote:", "\n" + text.replace("\r", ""))[0]
    text = re.split(r"\n\s*-{2,}\s*Original Message", text)[0]
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith(">")]
    return "\n".join(lines).strip()


def fetch_candidate_messages(candidate_email: str, thread_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Messages AUTHORED BY the candidate (oldest first): {id, thread_id, from, subject, body,
    received_at, internal_ts}. Restricted to one thread when thread_id is known."""
    email = candidate_email.strip().lower()

    if settings.google_mock_mode:
        out = []
        for m in _mock_mail.read()["messages"]:
            if m["direction"] != "in" or m["from"].lower() != email:
                continue
            if thread_id and m["thread_id"] != thread_id:
                continue
            out.append(
                {
                    "id": m["id"],
                    "thread_id": m["thread_id"],
                    "from": m["from"],
                    "subject": m["subject"],
                    "body": clean_reply_body(m["body"]),
                    "received_at": datetime.fromtimestamp(m["internal_ts"] / 1000, tz=timezone.utc).isoformat(),
                    "internal_ts": m["internal_ts"],
                }
            )
        return sorted(out, key=lambda m: m["internal_ts"])

    svc = _build_service("gmail", "v1")
    if thread_id:
        raw_msgs = svc.users().threads().get(userId="me", id=thread_id, format="full").execute().get("messages", [])
    else:
        refs = (
            svc.users().messages().list(userId="me", q=f"from:{email} newer_than:30d", maxResults=10)
            .execute().get("messages", [])
        )
        raw_msgs = [svc.users().messages().get(userId="me", id=r["id"], format="full").execute() for r in refs]

    out = []
    for m in raw_msgs:
        headers = {h["name"].lower(): h["value"] for h in m["payload"].get("headers", [])}
        sender = headers.get("from", "")
        if email not in sender.lower():
            continue
        ts = int(m.get("internalDate", "0"))
        out.append(
            {
                "id": m["id"],
                "thread_id": m["threadId"],
                "from": sender,
                "subject": headers.get("subject", ""),
                "body": clean_reply_body(_extract_body(m["payload"])),
                "received_at": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat(),
                "internal_ts": ts,
            }
        )
    return sorted(out, key=lambda m: m["internal_ts"])


def get_new_replies(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    """Candidate messages newer than the last one already processed for this candidate."""
    msgs = fetch_candidate_messages(candidate["email"], candidate.get("gmail_thread_id"))
    last = candidate.get("last_processed_message_id")
    if last:
        ref = next((m for m in msgs if m["id"] == last), None)
        if ref:
            return [m for m in msgs if m["internal_ts"] > ref["internal_ts"]]
    return msgs


def poll_for_replies() -> list[dict[str, Any]]:
    """Stage 4 trigger + reschedule edge case.

    Checks every candidate who is EMAILED_PENDING_REPLY or INTERVIEW_SCHEDULED:
      * emailed + new reply          -> {"kind": "first_reply"}  (status unchanged; scheduling agent runs)
      * ALREADY scheduled + new reply -> status set to RESCHEDULE_REQUESTED *here, before any agent
                                        runs*, then {"kind": "reschedule"} is returned.
    The orchestrator must pass the returned `message_id` to db.save_scheduling(...,
    last_processed_message_id=...) so the same reply is never handled twice.
    """
    found = []
    for cand in db.candidates_awaiting_reply():
        try:
            new = get_new_replies(cand)
            if not new:
                continue
            latest = new[-1]
            if cand["status"] == CandidateStatus.INTERVIEW_SCHEDULED.value:
                db.mark_reschedule_requested(cand["id"], latest["id"])  # state first, agents later
                kind = "reschedule"
                db.log_event("Gmail Poller", f"Reschedule request from {cand['email']}", cand["id"])
            else:
                kind = "first_reply"
                db.log_event("Gmail Poller", f"Reply received from {cand['email']}", cand["id"])
            found.append(
                {"candidate_id": cand["id"], "kind": kind, "message_id": latest["id"], "received_at": latest["received_at"]}
            )
        except Exception as exc:  # one broken mailbox lookup must not stop the whole poll
            db.log_event("Gmail Poller", f"Poll failed for {cand['email']}: {exc}", cand["id"], level="error")
    return found


# ============================================================================ CrewAI tools


def _json(**kwargs: Any) -> str:
    return json.dumps(kwargs, default=str)


class GmailSendInput(BaseModel):
    candidate_id: int = Field(..., description="Database id of the candidate to email.")
    subject: str = Field(..., description="Email subject line.")
    body: str = Field(..., description="Plain-text email body.")


class GmailSendTool(BaseTool):
    name: str = "Gmail_Send_Tool"
    description: str = (
        "Sends an email to a candidate (address looked up from the database, so you never supply it). "
        "Replies stay in the same Gmail thread. Returns JSON with status, message_id and thread_id."
    )
    args_schema: Type[BaseModel] = GmailSendInput

    def _run(self, candidate_id: int, subject: str, body: str) -> str:
        try:
            cand = db.get_candidate(candidate_id)
            if not cand:
                return _json(status="error", error=f"Candidate {candidate_id} not found")
            if cand["email"].endswith(".invalid"):
                return _json(status="error", error="Candidate has no valid email address on file")
            first_outreach = cand["status"] == CandidateStatus.SCREENED.value
            if first_outreach and (cand.get("screening_json") or {}).get("screening_decision") != "SHORTLIST":
                return _json(status="error", error="Outreach is only allowed for SHORTLISTed candidates")

            result = send_email(cand["email"], subject, body, thread_id=cand.get("gmail_thread_id"))
            db.set_gmail_thread_id(candidate_id, result["thread_id"])
            agent = "Outreach Agent" if first_outreach else "Scheduling Agent"
            db.log_event(agent, f"Emailed {cand.get('name') or cand['email']}", candidate_id)
            return _json(status="sent", to=cand["email"], **result)
        except Exception as exc:
            return _json(status="error", error=str(exc))


class GmailReadInput(BaseModel):
    candidate_id: int = Field(..., description="Database id of the candidate whose latest reply to read.")


class GmailReadTool(BaseTool):
    name: str = "Gmail_Read_Tool"
    description: str = (
        "Reads the candidate's most recent email reply (quoted history removed). Returns JSON with "
        "message_id, received_at, subject and body, or says no reply has arrived. Use it to find the "
        "interview times the candidate proposed."
    )
    args_schema: Type[BaseModel] = GmailReadInput

    def _run(self, candidate_id: int) -> str:
        try:
            cand = db.get_candidate(candidate_id)
            if not cand:
                return _json(status="error", error=f"Candidate {candidate_id} not found")
            db.log_event("Scheduling Agent", f"Reading reply from {cand.get('name') or cand['email']}", candidate_id)
            msgs = fetch_candidate_messages(cand["email"], cand.get("gmail_thread_id"))
            if not msgs:
                return _json(status="no_reply", detail="The candidate has not replied yet.")
            m = msgs[-1]
            return _json(status="ok", message_id=m["id"], received_at=m["received_at"], subject=m["subject"], body=m["body"])
        except Exception as exc:
            return _json(status="error", error=str(exc))


# ============================================================================ CLI


def _selftest() -> None:
    if not settings.google_mock_mode:
        sys.exit("selftest runs in mock mode only (set GOOGLE_MOCK_MODE=true).")
    sent = send_email("selftest@example.com", "Interview invitation", "Hi! When are you free?")
    mock_inject_reply(
        "selftest@example.com",
        "Tuesday 3pm works.\n\nOn Mon, 5 Oct 2026 at 9:00, Recruiter <r@x.com> wrote:\n> Hi! When are you free?",
    )
    msgs = fetch_candidate_messages("selftest@example.com", sent["thread_id"])
    assert len(msgs) == 1 and msgs[0]["body"] == "Tuesday 3pm works.", msgs
    print("gmail mock selftest OK ->", msgs[0]["body"])


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "selftest"
    if cmd == "auth":
        creds = get_google_credentials(interactive=True)
        profile = _build_service("gmail", "v1").users().getProfile(userId="me").execute()
        print("Authorised as", profile["emailAddress"], "- token saved to", settings.google_token_file)
    elif cmd == "reset-mock":
        _mock_mail.delete()
        JsonStore("mock_calendar.json", dict).delete()
        print("Mock Gmail + Calendar files removed.")
    else:
        _selftest()
