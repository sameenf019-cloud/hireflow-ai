"""CrewAI tool: hybrid (dense + BM25) search over ONE candidate's resume chunks."""
from typing import Type

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

import rag_engine
import state_manager as db


class ResumeSearchInput(BaseModel):
    candidate_id: int = Field(..., description="Database id of the candidate whose resume to search.")
    query: str = Field(
        ...,
        description=(
            "What to look for: a skill, tool, role, certification or requirement, "
            "e.g. 'Kubernetes production experience' or 'AWS Certified'."
        ),
    )
    limit: int = Field(4, ge=1, le=10, description="Maximum number of passages to return.")


class ResumeSearchTool(BaseTool):
    name: str = "Resume_Search_Tool"
    description: str = (
        "Searches a single candidate's resume with hybrid retrieval (semantic match plus exact "
        "keyword match). Use it to find evidence for each job requirement before scoring. "
        "Returns the most relevant resume passages."
    )
    args_schema: Type[BaseModel] = ResumeSearchInput

    def _run(self, candidate_id: int, query: str, limit: int = 4) -> str:
        try:
            cand = db.get_candidate(candidate_id)
            label = (cand or {}).get("name") or (cand or {}).get("email") or f"candidate {candidate_id}"
            db.log_event("Screening Agent", f"Searching {label}'s resume for: {query[:70]}", candidate_id)
            hits = rag_engine.hybrid_search(query, candidate_id=candidate_id, limit=limit)
        except Exception as exc:  # tools must return text so the agent can recover
            return f"Resume search failed: {exc}"
        if not hits:
            return f"No passages in candidate {candidate_id}'s resume matched '{query}'."
        return "\n\n".join(f"[Passage {h['chunk_index'] + 1} | relevance {h['score']:.3f}]\n{h['text']}" for h in hits)
