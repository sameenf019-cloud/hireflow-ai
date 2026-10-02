"""
tasks.py — CrewAI task factories. Every task sets output_pydantic so each
agent hands the next stage validated, typed data.

Outreach is a ConditionalTask: it only runs when the screening task's
output says SHORTLIST (stage 3 of the workflow).
"""
from __future__ import annotations

import os
from typing import Callable, Optional

from crewai import Agent, Task
from crewai.tasks.conditional_task import ConditionalTask
from crewai.tasks.task_output import TaskOutput

import config
from schemas import (
    CandidateScreeningSchema,
    EvaluationSchema,
    OutreachSchema,
    SchedulingSchema,
)


def _cfg(name: str, default: str) -> str:
    return str(getattr(config, name, None) or os.getenv(name) or default)


SHORTLIST_THRESHOLD = int(_cfg("SHORTLIST_THRESHOLD", "70"))
INTERVIEW_MINUTES = int(_cfg("INTERVIEW_MINUTES", "30"))
INTERVIEW_TZ = _cfg("INTERVIEW_TIMEZONE", "UTC")
COMPANY_NAME = _cfg("COMPANY_NAME", "our team")

TaskCallback = Optional[Callable[[TaskOutput], None]]


def _safe(text: str, limit: int = 6000) -> str:
    """
    Clip long text and neutralise curly braces so CrewAI's {placeholder}
    interpolation can never choke on JD / notes that contain code or JSON.
    """
    text = (text or "").strip()
    if len(text) > limit:
        text = text[:limit] + "\n[...truncated...]"
    return text.replace("{", "(").replace("}", ")")


# --------------------------------------------------------------------------
# Stage 2 — Screening
# --------------------------------------------------------------------------
def screening_task(
    agent: Agent,
    candidate_email: str,
    jd_text: str,
    callback: TaskCallback = None,
) -> Task:
    description = f"""
Screen the candidate whose email is: {candidate_email}

JOB DESCRIPTION
---
{_safe(jd_text)}
---

Follow these steps:
1. List the JD's must-have requirements (hard skills, tools, years of
   experience) and its nice-to-haves.
2. Use the Resume Search tool, restricted to this candidate ({candidate_email}).
   Run a separate query for EACH must-have requirement (exact tool/keyword
   names like "PostgreSQL" or "Kubernetes"), plus one query for overall
   experience themes. Do not rely on a single broad query.
3. A skill counts as matched ONLY if the retrieved resume text shows it.
   Never infer or invent skills.
4. match_score (0-100): 60 points must-have coverage, 25 points depth and
   relevance of experience, 15 points nice-to-haves.
5. screening_decision is "SHORTLIST" if match_score >= {SHORTLIST_THRESHOLD},
   otherwise "REJECT".

Set candidate_email to exactly: {candidate_email}
""".strip()

    return Task(
        description=description,
        expected_output=(
            "A JSON object with candidate_email, match_score (0-100), "
            "matched_skills (list), missing_skills (list), and "
            'screening_decision ("SHORTLIST" or "REJECT").'
        ),
        agent=agent,
        output_pydantic=CandidateScreeningSchema,
        callback=callback,
    )


# --------------------------------------------------------------------------
# Stage 3 — Outreach (conditional on SHORTLIST)
# --------------------------------------------------------------------------
def _is_shortlist(output: TaskOutput) -> bool:
    parsed = getattr(output, "pydantic", None)
    return bool(parsed) and getattr(parsed, "screening_decision", "") == "SHORTLIST"


def outreach_task(
    agent: Agent,
    candidate_email: str,
    *,
    screening_task: Optional[Task] = None,
    screening: Optional[CandidateScreeningSchema] = None,
    candidate_name: Optional[str] = None,
    sender_name: str = "The Recruiting Team",
    callback: TaskCallback = None,
) -> Task:
    """
    Pass screening_task=<Task> to chain inside one crew (becomes a
    ConditionalTask), or screening=<result> to run outreach on its own
    (e.g. retrying a failed send).
    """
    if screening is not None:
        screening_block = f"SCREENING RESULT:\n{_safe(screening.model_dump_json(indent=2))}"
    else:
        screening_block = (
            "The SCREENING RESULT is provided in the context from the previous task."
        )

    greeting = candidate_name or "the candidate"

    description = f"""
Write and send an interview invitation to {greeting} <{candidate_email}>
on behalf of {COMPANY_NAME}.

{screening_block}

Email requirements:
- Subject line plus a body of roughly 120-170 words, friendly and professional.
- Mention 2-3 SPECIFIC strengths taken from matched_skills so it feels personal.
- Do NOT mention the match score or any missing skills.
- Invite them to a {INTERVIEW_MINUTES}-minute interview and ask them to reply
  with 2-3 time windows that suit them in the next 7 business days, including
  their time zone.
- Sign off as: {sender_name}

Use the Gmail send tool exactly once, with to={candidate_email}.

Output rules:
- candidate_email = {candidate_email}
- email_draft = the exact body text you sent
- action_taken = "EMAIL_SENT" if the tool confirmed success, otherwise
  "SEND_FAILED" (never claim EMAIL_SENT without a successful tool result).
""".strip()

    kwargs = dict(
        description=description,
        expected_output=(
            "A JSON object with candidate_email, email_draft, and action_taken "
            '("EMAIL_SENT" or "SEND_FAILED").'
        ),
        agent=agent,
        output_pydantic=OutreachSchema,
        callback=callback,
    )

    if screening_task is not None:
        return ConditionalTask(
            condition=_is_shortlist,
            context=[screening_task],
            **kwargs,
        )
    return Task(**kwargs)


# --------------------------------------------------------------------------
# Stage 4 — Scheduling (also handles reschedules)
# --------------------------------------------------------------------------
def scheduling_task(
    agent: Agent,
    candidate_email: str,
    *,
    now_iso: str,
    reschedule: bool = False,
    candidate_name: Optional[str] = None,
    role_title: Optional[str] = None,
    callback: TaskCallback = None,
) -> Task:
    reschedule_note = ""
    if reschedule:
        reschedule_note = (
            "\nThis is a RESCHEDULE. The candidate's previous interview event "
            "has ALREADY been cancelled. Their LATEST reply asks for a new "
            "time. Ignore any times mentioned in older messages.\n"
        )

    who = candidate_name or candidate_email
    title = f"Interview: {who}" + (f" - {role_title}" if role_title else "")

    description = f"""
Schedule an interview with {who} <{candidate_email}>.
{reschedule_note}
Current date/time: {now_iso}
Interviewer time zone (use for all times): {INTERVIEW_TZ}
Interview length: {INTERVIEW_MINUTES} minutes

Steps:
1. Use the Gmail read tool to fetch replies from {candidate_email}. Use only
   the MOST RECENT reply.
2. Extract the time(s) the candidate proposes and resolve them to absolute
   start/end datetimes in {INTERVIEW_TZ} (relative words like "Tuesday
   afternoon" are relative to the current date/time above). If the candidate
   gave a time zone, convert it.
3. For each proposed slot, in the order offered, use the Calendar FreeBusy tool
   to check that exact window. Pick the first slot that is free.
4. Use the Calendar event-insert tool to book it: title "{title}", the
   candidate ({candidate_email}) as attendee, a Google Meet link, duration
   {INTERVIEW_MINUTES} minutes.
5. Never book a slot you did not FreeBusy-check, and never invent a time the
   candidate did not offer.

Set status to exactly one of:
- "SCHEDULED"            event was created (set agreed_timestamp = start time
                         in ISO 8601 with UTC offset, and calendar_event_link)
- "NO_REPLY"             no reply from the candidate was found
- "NEEDS_CLARIFICATION"  the reply contains no usable time
- "NO_SLOT"              every proposed time is busy
For any status other than SCHEDULED, set agreed_timestamp and
calendar_event_link to an empty string "".
candidate_email = {candidate_email}
""".strip()

    return Task(
        description=description,
        expected_output=(
            "A JSON object with candidate_email, agreed_timestamp (ISO 8601), "
            "calendar_event_link, and status."
        ),
        agent=agent,
        output_pydantic=SchedulingSchema,
        callback=callback,
    )


# --------------------------------------------------------------------------
# Stage 5 — Evaluator
# --------------------------------------------------------------------------
def evaluation_task(
    agent: Agent,
    candidate_email: str,
    jd_text: str,
    screening_json: str,
    interview_notes: str,
    callback: TaskCallback = None,
) -> Task:
    description = f"""
Give a final hiring recommendation for {candidate_email}.

JOB DESCRIPTION
---
{_safe(jd_text, 4000)}
---

SCREENING RESULT (JSON)
---
{_safe(screening_json, 2000)}
---

HIRING MANAGER'S INTERVIEW NOTES
---
{_safe(interview_notes, 4000)}
---

Rules:
- recommendation is "HIRE" or "REJECT". Choose HIRE only if the interview
  notes support the JD's must-have requirements.
- If the resume screening and the interview notes disagree, the interview
  evidence wins.
- strengths and concerns: short, concrete bullet strings tied to evidence.
- confidence: integer 0-100 reflecting how strong the evidence is.
- summary: 120-200 words of plain prose (no markdown, no bullet characters),
  written for the hiring manager. It will be streamed to the UI word by word.
candidate_email = {candidate_email}
""".strip()

    return Task(
        description=description,
        expected_output=(
            "A JSON object with candidate_email, recommendation, confidence, "
            "strengths, concerns, and summary."
        ),
        agent=agent,
        output_pydantic=EvaluationSchema,
        callback=callback,
    )
