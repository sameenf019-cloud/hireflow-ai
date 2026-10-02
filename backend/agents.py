"""
agents.py — the four CrewAI agents for HireFlow AI.

    Screening  -> Resume Search tool (Qdrant hybrid RAG)
    Outreach   -> Gmail send tool
    Scheduling -> Gmail read + Calendar FreeBusy + Calendar insert tools
    Evaluator  -> no tools (pure reasoning over JD + screening JSON + notes)

Agents are built by a factory (not at import time) so importing this module
never needs API keys or Google credentials.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Dict, Optional

from crewai import LLM, Agent

import config
from tools.google_calendar import CalendarFreeBusyTool, CalendarBookTool
from tools.google_gmail import GmailReadTool, GmailSendTool
from tools.resume_search import ResumeSearchTool

# LiteLLM (used by CrewAI) routes "groq/<model>" to the Groq API.
GROQ_MODEL = "groq/llama-3.3-70b-versatile"


def _cfg(name: str, default: Optional[str] = None) -> Optional[str]:
    """Read a setting from config.py first, then the environment."""
    return getattr(config, name, None) or os.getenv(name) or default


def build_llm(temperature: float = 0.1) -> LLM:
    api_key = _cfg("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is not set (check backend/.env).")
    return LLM(
        model=GROQ_MODEL,
        api_key=api_key,
        temperature=temperature,
        max_tokens=2048,
    )


def build_agents(
    step_callback: Optional[Callable[[Any], None]] = None,
    verbose: Optional[bool] = None,
) -> Dict[str, Agent]:
    """
    Returns {"screening", "outreach", "scheduling", "evaluator"}.

    step_callback is passed to every agent so the API layer (Phase 5) can
    stream "Agent X is calling tool Y" events to the frontend visualizer.
    """
    if verbose is None:
        verbose = os.getenv("CREW_VERBOSE", "0") == "1"

    llm = build_llm(temperature=0.1)
    eval_llm = build_llm(temperature=0.2)

    common = dict(
        allow_delegation=False,   # strict sequential handoff, no ad-hoc delegation
        verbose=verbose,
        max_iter=6,               # stop runaway tool loops
        max_rpm=15,               # stay under Groq rate limits
        step_callback=step_callback,
    )

    screening = Agent(
        role="Senior Technical Recruiter (Screening)",
        goal=(
            "Objectively score how well a candidate's resume matches a job "
            "description and list exactly which required skills are matched "
            "or missing."
        ),
        backstory=(
            "You screen hundreds of resumes a week. You only trust evidence "
            "retrieved from the resume with your search tool. You never invent "
            "or assume a skill the resume does not show, and you search for "
            "each requirement separately so exact keywords are not missed."
        ),
        tools=[ResumeSearchTool()],
        llm=llm,
        **common,
    )

    outreach = Agent(
        role="Candidate Outreach Specialist",
        goal=(
            "Write a warm, specific, professional email to a shortlisted "
            "candidate inviting them to interview, and send it exactly once."
        ),
        backstory=(
            "You write recruiting emails that candidates actually answer: "
            "short, personal, and concrete. You reference real strengths from "
            "the candidate's screening result. You never mention scores or "
            "gaps, and you never send an email without using the send tool."
        ),
        tools=[GmailSendTool()],
        llm=llm,
        **common,
    )

    scheduling = Agent(
        role="Interview Scheduling Coordinator",
        goal=(
            "Read the candidate's latest reply, find a proposed time that is "
            "free on the interviewer's calendar, and book it with a Google "
            "Meet link."
        ),
        backstory=(
            "You are meticulous about time zones and double-booking. You never "
            "book a slot without first checking FreeBusy for that exact "
            "window, and you never invent a time the candidate did not offer."
        ),
        tools=[GmailReadTool(), CalendarFreeBusyTool(), CalendarBookTool()],
        llm=llm,
        **common,
    )

    evaluator = Agent(
        role="Hiring Evaluator",
        goal=(
            "Produce a fair, evidence-based HIRE or REJECT recommendation from "
            "the job description, the screening result, and the interviewer's "
            "notes."
        ),
        backstory=(
            "You advise hiring managers. Interview evidence outweighs resume "
            "claims when they conflict. You cite concrete strengths and "
            "concerns, and you are honest when the evidence is thin."
        ),
        tools=[],
        llm=eval_llm,
        **common,
    )

    return {
        "screening": screening,
        "outreach": outreach,
        "scheduling": scheduling,
        "evaluator": evaluator,
    }
