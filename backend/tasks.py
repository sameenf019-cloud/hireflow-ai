"""
tasks.py — CrewAI task factories. Every task sets output_pydantic so each
agent hands the next stage validated, typed data.

EXCEPTION: scheduling_task does NOT use output_pydantic. CrewAI's pydantic
converter calls Groq through instructor in tool-call mode, and Groq models
often answer with plain JSON instead of a tool call, which raises
"Tool choice is required, but model did not call a tool". Instead the Scheduling
agent returns plain JSON text and orchestrator.py parses it itself.

Outreach is a ConditionalTask: it only runs when the screening task's
output says SHORTLIST (stage 3 of the workflow). The Outreach agent only
WRITES the email; orchestrator.py sends it.

Scheduling: the agent has NO tools. It only reads the candidate's reply
text (handed to it below) and extracts proposed interview time(s) as plain
JSON. orchestrator.py then checks calendar FreeBusy and books the event with
plain Python (tools.google_calendar).
"""
from __future__ import annotations

import os
from typing import Callable, List, Literal, Optional

from crewai import Agent, Task
from crewai.tasks.conditional_task import ConditionalTask
from crewai.tasks.task_output import TaskOutput
from pydantic import BaseModel, Field

import config
from schemas import (
    CandidateScreeningSchema,
    EvaluationSchema,
    OutreachSchema,
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
    import rag_engine
    import state_manager as db

    _cand = db.get_candidate_by_email(candidate_email)
    candidate_id = _cand["id"] if _cand else None
    resume_text = (_cand or {}).get("resume_text") or ""

    # Preferred: hybrid retrieval (Qdrant + FastEmbed). Fallback: the full resume
    # text stored in SQLite, used when retrieval fails (e.g. onnxruntime DLL error).
    try:
        evidence = rag_engine.build_screening_context(candidate_id, jd_text) if candidate_id else ""
    except Exception as exc:
        print(f"[DEBUG] retrieval failed, using stored resume text: {exc}", flush=True)
        evidence = ""
    if not evidence:
        evidence = resume_text
    if not evidence:
        evidence = "No resume passages found for this candidate."

    print(f"[DEBUG] candidate_id={candidate_id} evidence_len={len(evidence)}", flush=True)
    print("[DEBUG] evidence_start=" + evidence[:300].replace("\n", " | "), flush=True)

    description = f"""
Screen the candidate whose email is: {candidate_email}
Their database candidate_id is: {candidate_id}

JOB DESCRIPTION
---
{_safe(jd_text)}
---

RESUME EVIDENCE (text from this candidate's resume)
---
{_safe(evidence, 7000)}
---

Follow these steps:
1. List the JD's must-have requirements (hard skills, tools, years of
   experience) and its nice-to-haves.
2. Check each requirement against the RESUME EVIDENCE above. You have no tools; use only that evidence.
3. A skill counts as matched ONLY if the resume evidence shows it.
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
# Stage 3 — Outreach (conditional on SHORTLIST). The agent only WRITES the
# email; orchestrator.py sends it.
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
Write an interview invitation email to {greeting} <{candidate_email}>
on behalf of {COMPANY_NAME}. You do NOT send it and you have no tools:
the system sends the email after you finish.

{screening_block}

Email requirements:
- A subject line plus a body of roughly 120-170 words, friendly and professional.
- Mention 2-3 SPECIFIC strengths taken from matched_skills so it feels personal.
- Do NOT mention the match score or any missing skills.
- Invite them to a {INTERVIEW_MINUTES}-minute interview and ask them to reply
  with 2-3 time windows that suit them in the next 7 business days, including
  their time zone.
- Sign off as: {sender_name}

Output rules:
- candidate_email = {candidate_email}
- email_draft = the full email in this exact layout: the first line is
  "Subject: <your subject line>", then one blank line, then the email body.
- action_taken = "SEND_FAILED" (always; the system sets the real result itself).
""".strip()

    kwargs = dict(
        description=description,
        expected_output=(
            "A JSON object with candidate_email, email_draft (first line "
            '"Subject: ...", blank line, then the body), and action_taken '
            '("SEND_FAILED").'
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
class SchedulingProposalSchema(BaseModel):
    """What the Scheduling agent produces (no tools). orchestrator.py parses
    the agent's raw JSON text into this schema itself, then checks FreeBusy
    and books the event in plain Python."""

    candidate_email: str
    proposed_times: List[str] = Field(
        default_factory=list,
        description=(
            "ISO 8601 datetimes with a UTC offset, in the order the candidate "
            "offered them. Empty if the reply has no usable time."
        ),
    )
    status: Literal["HAS_TIMES", "NEEDS_CLARIFICATION"]


def scheduling_task(
    agent: Agent,
    candidate_email: str,
    *,
    reply_body: str,
    now_iso: str,
    reschedule: bool = False,
    candidate_name: Optional[str] = None,
    callback: TaskCallback = None,
) -> Task:
    reschedule_note = ""
    if reschedule:
        reschedule_note = (
            "\nThis is a RESCHEDULE. The candidate's previous interview event "
            "has ALREADY been cancelled. This reply asks for a new time.\n"
        )

    who = candidate_name or candidate_email

    description = f"""
Read this candidate's reply and figure out what interview time(s) they are
proposing. You have NO TOOLS - work only from the reply text below. You do
NOT check any calendar and you do NOT book anything; that happens after you.

Candidate: {who} <{candidate_email}>
{reschedule_note}
Current date/time: {now_iso}
Interviewer time zone (resolve every time into this zone): {INTERVIEW_TZ}
Interview length: {INTERVIEW_MINUTES} minutes

CANDIDATE'S REPLY
---
{_safe(reply_body, 3000)}
---

Steps:
1. Find every time the candidate proposes (they may offer more than one).
   Resolve relative words like "Tuesday afternoon" or "next week" relative
   to the current date/time above. If the candidate gave their own time
   zone, convert it to {INTERVIEW_TZ}.
2. List them in proposed_times, in the order the candidate offered them,
   each as a full ISO 8601 datetime WITH a UTC offset
   (e.g. 2026-10-06T14:00:00+05:00).
3. If the reply contains no usable time at all, leave proposed_times empty.

Set status to "HAS_TIMES" if proposed_times is non-empty, otherwise
"NEEDS_CLARIFICATION".
Set candidate_email to exactly: {candidate_email}

Respond with ONLY the JSON object. No prose, no markdown, no code fences.
""".strip()

    # NOTE: no output_pydantic here on purpose (see module docstring).
    return Task(
        description=description,
        expected_output=(
            "ONLY a JSON object with candidate_email, proposed_times (list of "
            "ISO 8601 datetimes with UTC offset, in the order offered), and "
            'status ("HAS_TIMES" or "NEEDS_CLARIFICATION").'
        ),
        agent=agent,
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