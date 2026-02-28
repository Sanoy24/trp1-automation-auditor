"""
LangGraph StateGraph for the Automaton Auditor Swarm.

Implements the complete Digital Courtroom architecture:

    START
      ├──► repo_investigator  ──┐
      ├──► doc_analyst         ──┤
      └──► vision_inspector    ──┤
                                 │
                    evidence_aggregator  (fan-in sync point)
                                 │
                    [route_after_aggregation]
                       ├── error ──► END
                       └── ok    ──► judge_dispatch
                                        │
                        ┌───────────────┼───────────────┐
                        ▼               ▼               ▼
                   prosecutor       defense         tech_lead    (fan-out)
                        │               │               │
                        └───────────────┼───────────────┘
                                        ▼
                                 chief_justice  (synthesis)
                                        │
                                   report_node
                                        │
                                       END

Both detective and judge layers use parallel fan-out / fan-in.
State reducers (operator.add, operator.ior) ensure parallel writes
merge safely without data loss.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Literal, Optional

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from src.nodes.detectives import doc_analyst, repo_investigator, vision_inspector_node
from src.nodes.judges import defense_node, prosecutor_node, tech_lead_node
from src.nodes.justice import chief_justice_node
from src.report_generator import render_audit_report
from src.state import AgentState, Evidence

logger = logging.getLogger(__name__)

DEFAULT_RUBRIC_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "rubric", "rubric.json"
)


# ---------------------------------------------------------------------------
# Utility nodes
# ---------------------------------------------------------------------------


def evidence_aggregator(state: AgentState) -> dict:
    """Fan-in synchronisation point for the detective layer.

    Collects all Evidence objects (already merged into state via the
    operator.ior reducer), performs cross-reference checks between
    DocAnalyst and RepoInvestigator evidence, and logs a summary.
    """
    evidences = state.get("evidences", {})
    total = sum(len(v) for v in evidences.values())
    logger.info(
        "EvidenceAggregator: %d evidence items from %d sources.",
        total,
        len(evidences),
    )
    if total == 0:
        return {"error": "No evidence collected from any detective."}

    # --- Cross-reference: DocAnalyst paths vs RepoInvestigator file manifest ---
    # GAP 3 FIX: The doc requires DocAnalyst to cross-reference file paths
    # against repo evidence to detect "hallucinations" in the PDF report.
    doc_evidence = evidences.get("doc", [])
    repo_evidence = evidences.get("repo", [])

    # Build a set of known files from repo evidence
    repo_files: set = set()
    for ev in repo_evidence:
        if ev.goal and "file" in ev.goal.lower() and ev.found:
            # Extract file paths from location or content
            if ev.location:
                repo_files.add(ev.location)
            if ev.content:
                # Content may list file paths
                for line in ev.content.split("\n"):
                    stripped = line.strip()
                    if stripped and ("/" in stripped or "\\" in stripped):
                        repo_files.add(stripped)

    # Check doc-cited paths against repo evidence
    cross_ref_evidence = []
    for ev in doc_evidence:
        if ev.goal and "cross_reference" in ev.goal.lower():
            # This evidence item contains file paths from the PDF
            if ev.content:
                cited_paths = [p.strip() for p in ev.content.split(",") if p.strip()]
                verified = [p for p in cited_paths if any(p in rf for rf in repo_files)]
                hallucinated = [
                    p for p in cited_paths if not any(p in rf for rf in repo_files)
                ]

                if hallucinated:
                    cross_ref_evidence.append(
                        Evidence(
                            goal="Cross-reference: Hallucinated file paths in PDF",
                            found=True,
                            content=f"Hallucinated paths: {hallucinated}. Verified: {verified}",
                            location="evidence_aggregator/cross_reference",
                            rationale=(
                                f"PDF report cited {len(cited_paths)} paths. "
                                f"{len(verified)} verified, {len(hallucinated)} not found in repo."
                            ),
                            confidence=0.9,
                        )
                    )
                    logger.warning(
                        "Cross-reference found %d hallucinated paths: %s",
                        len(hallucinated),
                        hallucinated,
                    )

    if cross_ref_evidence:
        return {"evidences": {"cross_ref": cross_ref_evidence}}

    return {}


def judge_dispatch(state: AgentState) -> dict:
    """Fan-out synchronisation point before the judicial layer.

    This node is the single source from which the three judge edges
    fan out.  It performs no state mutation — it exists purely so that
    LangGraph can dispatch to the three judges in parallel from a
    common predecessor node.
    """
    evidence_count = sum(
        len(ev_list) for ev_list in state.get("evidences", {}).values()
    )
    logger.info(
        "Judge dispatch: forwarding %d evidence items to the judicial bench.",
        evidence_count,
    )
    return {}


def judge_sync(state: AgentState) -> dict:
    """Fan-in synchronisation point after the judicial layer.

    Collects all JudicialOpinion objects (already merged into state
    via the operator.add reducer) and logs a summary before handing
    off to the Chief Justice.
    """
    opinions = state.get("opinions", [])
    logger.info(
        "Judge sync: %d judicial opinions collected, forwarding to Chief Justice.",
        len(opinions),
    )
    return {}


def report_node(state: AgentState) -> dict:
    """Final node: logs completion of the audit.

    Actual report rendering is handled by main.py to allow
    dynamic output path configuration via CLI.
    """
    report = state.get("final_report")
    if report is None:
        logger.error("report_node received no final_report in state.")
        return {}

    logger.info(
        "Audit complete. Overall score: %.1f/5 across %d criteria.",
        report.overall_score,
        len(report.criteria),
    )
    return {}


# ---------------------------------------------------------------------------
# Conditional Routing
# ---------------------------------------------------------------------------


def route_after_aggregation(
    state: AgentState,
) -> Literal["judge_dispatch", "__end__"]:
    """Route after evidence aggregation.

    If a fatal error occurred (e.g. repo could not be cloned), short-
    circuit directly to END.  Otherwise proceed to the judicial layer.
    """
    if state.get("error"):
        logger.warning(
            "Fatal error detected; skipping judicial phase: %s", state["error"]
        )
        return "__end__"
    return "judge_dispatch"


# ---------------------------------------------------------------------------
# Graph Construction
# ---------------------------------------------------------------------------


def build_auditor_graph(checkpointer: Optional[MemorySaver] = None) -> StateGraph:
    """Build and compile the complete Automaton Auditor StateGraph.

    Architecture:
        Detective Fan-Out:  START → [repo_investigator, doc_analyst, vision_inspector]
        Detective Fan-In:   → evidence_aggregator
        Routing:            evidence_aggregator →[conditional]→ judge_dispatch | END
        Judge Fan-Out:      judge_dispatch → [prosecutor, defense, tech_lead]
        Judge Fan-In:       → judge_sync
        Synthesis:          judge_sync → chief_justice → report_node → END

    Args:
        checkpointer: Optional MemorySaver for crash recovery.

    Returns:
        Compiled LangGraph application.
    """
    builder = StateGraph(AgentState)

    # --- Register ALL nodes --------------------------------------------------
    # Detective layer
    builder.add_node("repo_investigator", repo_investigator)
    builder.add_node("doc_analyst", doc_analyst)
    builder.add_node("vision_inspector", vision_inspector_node)
    builder.add_node("evidence_aggregator", evidence_aggregator)

    # Judicial layer
    builder.add_node("judge_dispatch", judge_dispatch)
    builder.add_node("prosecutor", prosecutor_node)
    builder.add_node("defense", defense_node)
    builder.add_node("tech_lead", tech_lead_node)
    builder.add_node("judge_sync", judge_sync)

    # Supreme Court
    builder.add_node("chief_justice", chief_justice_node)
    builder.add_node("report_node", report_node)

    # --- Detective Fan-Out: START → all detectives in parallel ---------------
    builder.add_edge(START, "repo_investigator")
    builder.add_edge(START, "doc_analyst")
    builder.add_edge(START, "vision_inspector")

    # --- Detective Fan-In: all detectives → aggregator -----------------------
    builder.add_edge("repo_investigator", "evidence_aggregator")
    builder.add_edge("doc_analyst", "evidence_aggregator")
    builder.add_edge("vision_inspector", "evidence_aggregator")

    # --- Conditional routing: skip judges on fatal error ---------------------
    builder.add_conditional_edges(
        "evidence_aggregator",
        route_after_aggregation,
        {
            "judge_dispatch": "judge_dispatch",
            "__end__": END,
        },
    )

    # --- Judge Fan-Out: dispatch → all judges in parallel --------------------
    builder.add_edge("judge_dispatch", "prosecutor")
    builder.add_edge("judge_dispatch", "defense")
    builder.add_edge("judge_dispatch", "tech_lead")

    # --- Judge Fan-In: all judges → sync point -------------------------------
    builder.add_edge("prosecutor", "judge_sync")
    builder.add_edge("defense", "judge_sync")
    builder.add_edge("tech_lead", "judge_sync")

    # --- Synthesis: sync → chief justice → report → END ----------------------
    builder.add_edge("judge_sync", "chief_justice")
    builder.add_edge("chief_justice", "report_node")
    builder.add_edge("report_node", END)

    # --- Compile -------------------------------------------------------------
    if checkpointer is None:
        checkpointer = MemorySaver()

    graph = builder.compile(checkpointer=checkpointer)
    logger.info("Auditor graph compiled successfully (full courtroom).")
    return graph


# Backward-compatible alias
build_detective_graph = build_auditor_graph


# ---------------------------------------------------------------------------
# Convenience runner
# ---------------------------------------------------------------------------


def load_rubric(rubric_path: Optional[str] = None) -> list:
    """Load rubric dimensions from rubric.json.

    Returns:
        List of dimension dicts from the rubric JSON.
    """
    path = rubric_path or DEFAULT_RUBRIC_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            rubric = json.load(f)
        dimensions = rubric.get("dimensions", [])
        logger.info("Loaded %d rubric dimensions from %s", len(dimensions), path)
        return dimensions
    except Exception as exc:
        logger.error("Failed to load rubric from %s: %s", path, exc)
        return []


def run_auditor_graph(
    repo_url: str,
    pdf_path: Optional[str] = None,
    rubric_path: Optional[str] = None,
    thread_id: str = "audit_session_1",
    output_dir: Optional[str] = None,
) -> dict:
    """Run the full auditor graph against a target repository.

    Args:
        repo_url: HTTPS URL of the GitHub repository to audit.
        pdf_path: Local path to the PDF report (optional).
        rubric_path: Path to rubric.json (defaults to rubric/rubric.json).
        thread_id: Unique thread ID for checkpointing.

    Returns:
        The final AgentState dict after execution.
    """
    graph = build_auditor_graph()
    # if caller requested a diagram, render and save
    if output_dir:
        try:
            mermaid_png = graph.get_graph().draw_mermaid_png()
            out_path = Path(output_dir) / "auditor_graph.png"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "wb") as f:
                f.write(mermaid_png)
            logger.info("Saved graph diagram to %s", out_path)
        except Exception as exc:
            logger.warning("Failed to render graph diagram: %s", exc)
    dimensions = load_rubric(rubric_path)

    initial_state = {
        "repo_url": repo_url,
        "pdf_path": pdf_path or "",
        "rubric_dimensions": dimensions,
        "evidences": {},
        "opinions": [],
        "final_report": None,
        "error": None,
    }

    config = {"configurable": {"thread_id": thread_id}}

    logger.info("Starting full auditor graph for %s", repo_url)
    final_state = graph.invoke(initial_state, config)
    logger.info(
        "Auditor graph complete — overall score: %s",
        (
            final_state.get("final_report").overall_score
            if final_state.get("final_report")
            else "N/A"
        ),
    )
    return final_state
