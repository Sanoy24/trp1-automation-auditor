"""
state.py — The Constitution of the Digital Courtroom.

All data contracts for the Automaton Auditor swarm are defined here.
Every node reads from and writes to AgentState using these typed schemas.
Pydantic BaseModel enforces contracts at write time; reducers prevent
parallel-write race conditions at the LangGraph state-merge level.
"""

import operator
from typing import Annotated, Dict, List, Literal, Optional

from pydantic import BaseModel, Field
from typing_extensions import TypedDict


class Evidence(BaseModel):
    """
    The atomic output unit of every Detective node.
    Detectives NEVER opinionate — they only produce Evidence.
    Every claim must be tied to a specific, citable location.
    """

    goal: str = Field(description="The forensic goal this evidence addresses")
    found: bool = Field(description="Whether the artifact was located")
    content: Optional[str] = Field(
        default=None,
        description="Exact code snippet, commit message, or text excerpt confirming the finding",
    )
    location: str = Field(
        description="File path, line number, or commit hash — must be specific and citable"
    )
    rationale: str = Field(
        description="Rationale for the confidence score — what was examined and why"
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="0.0 = artifact not found / unverifiable, 1.0 = irrefutable evidence",
    )


class JudicialOpinion(BaseModel):
    """
    The output of a single Judge node for a single rubric criterion.
    Each judge produces one JudicialOpinion per criterion.
    With 3 judges × 10 criteria = 30 JudicialOpinion objects total.
    cited_evidence must reference Evidence.location values — not assertions.
    """

    judge: Literal["Prosecutor", "Defense", "TechLead"]
    criterion_id: str = Field(description="Must match a dimension id from rubric.json")
    score: int = Field(ge=1, le=5, description="Score on the 1–5 rubric scale")
    argument: str = Field(
        description="Full argument citing specific evidence locations"
    )
    cited_evidence: List[str] = Field(
        description="List of Evidence.location values that support this opinion"
    )


class CriterionResult(BaseModel):
    """
    The Chief Justice's final ruling on a single rubric criterion.
    Produced after applying constitutional rules to all three JudicialOpinions.
    dissent_summary is mandatory when max score variance > 2.
    """

    dimension_id: str
    dimension_name: str
    final_score: int = Field(ge=1, le=5)
    judge_opinions: List[JudicialOpinion]
    dissent_summary: Optional[str] = Field(
        default=None,
        description="Required when score variance > 2. Explains why one side was overruled.",
    )
    remediation: str = Field(
        description="Specific file-level instructions for improvement"
    )


class AuditReport(BaseModel):
    """
    The complete output of the Automaton Auditor swarm.
    Serialized to a structured Markdown file — never just console output.
    """

    repo_url: str
    executive_summary: str
    overall_score: float = Field(ge=1.0, le=5.0)
    criteria: List[CriterionResult]
    remediation_plan: str = Field(
        description="Consolidated remediation plan with file-level instructions per criterion"
    )


class AgentState(TypedDict):

    repo_url: str
    pdf_path: str
    rubric_dimensions: List[Dict]

    # Parallel-safe: dict merge — each detective writes under its own key
    evidences: Annotated[Dict[str, List[Evidence]], operator.ior]

    # Parallel-safe: list append — each judge appends its opinions
    opinions: Annotated[List[JudicialOpinion], operator.add]

    final_report: AuditReport
