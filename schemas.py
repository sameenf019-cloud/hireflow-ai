"""Pydantic models used as CrewAI `output_pydantic` targets, plus shared enums."""
from enum import Enum
from typing import List, Literal

from pydantic import BaseModel, Field


class CandidateStatus(str, Enum):
    PENDING_SCREENING = "PENDING_SCREENING"
    SCREENED = "SCREENED"
    EMAILED_PENDING_REPLY = "EMAILED_PENDING_REPLY"
    INTERVIEW_SCHEDULED = "INTERVIEW_SCHEDULED"
    RESCHEDULE_REQUESTED = "RESCHEDULE_REQUESTED"
    EVALUATED = "EVALUATED"


# ---- Specified schemas (exact fields from the project spec) ----

class CandidateScreeningSchema(BaseModel):
    candidate_email: str
    match_score: float = Field(ge=0, le=100, description="Overall JD fit, 0-100")
    matched_skills: List[str] = Field(default_factory=list)
    missing_skills: List[str] = Field(default_factory=list)
    screening_decision: Literal["SHORTLIST", "REJECT"]


class OutreachSchema(BaseModel):
    candidate_email: str
    email_draft: str
    action_taken: str


class SchedulingSchema(BaseModel):
    candidate_email: str
    agreed_timestamp: str = Field(description="ISO 8601 timestamp of the booked interview")
    calendar_event_link: str
    status: str


# ---- Addition: the Evaluator agent also needs an output_pydantic target ----

class EvaluationSchema(BaseModel):
    candidate_email: str
    recommendation: Literal["HIRE", "REJECT"]
    summary: str = Field(description="Final report text; streamed word-by-word in the UI")
