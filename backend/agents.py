"""
agents.py — the four CrewAI agents for HireFlow AI.

    Screening  -> no tools (resume evidence is injected into the task by tasks.py)
    Outreach   -> no tools (writes the email; orchestrator.py sends it)
    Scheduling -> no tools (extracts proposed times from the reply text;
                  orchestrator.py checks FreeBusy and books the event)
    Evaluator  -> no tools (pure reasoning over JD + screening JSON + notes)

Agents are built by a factory (not at import time) so importing this module
never needs API keys or Google credentials.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Dict, Optional

from crewai import LLM, Agent

import config

import litellm


def _strip_cache_breakpoint(messages):
    if not isinstance(messages, list):
        return messages
    return [
        {k: v for k, v in m.items() if k != "cache_breakpoint"} if isinstance(m, dict) else m
        for m in messages
    ]


_orig_completion = litellm.completion
_orig_acompletion = litellm.acompletion


def _patched_completion(*args, **kwargs):
    if "messages" in kwargs:
        kwargs["messages"] = _strip_cache_breakpoint(kwargs["messages"])
    return _orig_completion(*args, **kwargs)


async def _patched_acompletion(*args, **kwargs):
    if "messages" in kwargs:
        kwargs["messages"] = _strip_cache_breakpoint(kwargs["messages"])
    return await _orig_acompletion(*args, **kwargs)


litellm.completion = _patched_completion
litellm.acompletion = _patched_acompletion

# CrewAI writes every task's output to a local sqlite "replay" db on each
# kickoff (crewai/utilities/task_output_storage_handler.py). This project
# never calls Crew.replay(), and the project folder lives inside OneDrive,
# whose background sync can briefly lock that sqlite file and crash a run
# with "disk I/O error". Patch the handler to a no-op so nothing is written
# there and a sync lock can never crash a run.
from crewai.utilities.task_output_storage_handler import TaskOutputStorageHandler


def _noop_update(self, *args, **kwargs) -> None:
    pass


TaskOutputStorageHandler.update = _noop_update

# LiteLLM (used by CrewAI) routes "groq/<model>" to the Groq API.
# llama-3.3-70b-versatile was retired by Groq on 16 Aug 2026.
# qwen/qwen3.6-27b is not available on this account; gpt-oss-120b works for screening.
GROQ_MODEL = "groq/openai/gpt-oss-120b"


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
            "You screen hundreds of resumes a week. You only trust the resume "
            "evidence given to you in the task. You never invent or assume a "
            "skill the resume does not show, and you check each requirement "
            "separately so exact keywords are not missed."
        ),
        tools=[],
        llm=llm,
        **common,
    )

    outreach = Agent(
        role="Candidate Outreach Specialist",
        goal=(
            "Write a warm, specific, professional email to a shortlisted "
            "candidate inviting them to interview. You only write the email; "
            "the system sends it."
        ),
        backstory=(
            "You write recruiting emails that candidates actually answer: "
            "short, personal, and concrete. You reference real strengths from "
            "the candidate's screening result. You never mention scores or "
            "gaps."
        ),
        tools=[],
        llm=llm,
        **common,
    )

    scheduling = Agent(
        role="Interview Scheduling Coordinator",
        goal=(
            "Read a candidate's reply and extract the interview time(s) they "
            "propose, resolved to exact ISO 8601 datetimes in the "
            "interviewer's time zone."
        ),
        backstory=(
            "You are meticulous about time zones and relative dates like "
            "'next Tuesday' or 'this Thursday afternoon'. You extract exactly "
            "what the candidate offered and never invent a time they did not "
            "mention. You do not check calendars or book anything yourself - "
            "that happens after you, in plain code."
        ),
        tools=[],
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