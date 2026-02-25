import json
import logging
from pathlib import Path
from typing import Any, Dict, List

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from src.nodes.detectives import DocAnalyst, RepoInvestigator, vision_inspector_node
from src.state import AgentState

logger = logging.getLogger(__name__)

RUBRIC_PATH = Path(__file__).parent.parent / "rubric.json"


def context_builder_node(state: AgentState) -> Dict[str, Any]:

    if RUBRIC_PATH.exists():
        try:
            with open(RUBRIC_PATH, "r") as f:
                rubric = json.load(f)
            dimensions = rubric.get("dimensions", [])
            logger.info("ContextBuilder loaded %d rubric dimensions", len(dimensions))
        except Exception as exc:
            logger.warning(
                "Failed to parse rubric.json: %s — using empty dimensions", exc
            )
            dimensions = []
    else:
        logger.warning(
            "rubric.json not found at %s — proceeding without rubric context",
            RUBRIC_PATH,
        )
        dimensions = []

    return {
        "rubric_dimensions": dimensions,
        "evidences": {},
        "opinions": [],
    }


def fan_out_detectives(state: AgentState) -> List[Send]:

    return [
        Send("repo_investigator", state),
        Send("doc_analyst", state),
        Send("vision_inspector", state),
    ]


def EvidenceAggregator(state: AgentState) -> Dict[str, Any]:

    evidences = state.get("evidences", {})

    required_keys = {"repo", "doc"}
    missing = required_keys - set(evidences.keys())

    if missing:
        logger.warning(
            "EvidenceAggregator: missing evidence from detectives: %s. "
            "Proceeding with partial evidence.",
            missing,
        )
    else:
        logger.info("EvidenceAggregator: all required detective evidence received ✓")

    total_items = 0
    for key, ev_list in evidences.items():
        found_count = sum(1 for e in ev_list if e.found)
        not_found_count = len(ev_list) - found_count
        logger.info(
            "  [%s] %d items — %d confirmed / %d not found",
            key,
            len(ev_list),
            found_count,
            not_found_count,
        )
        total_items += len(ev_list)

    logger.info("EvidenceAggregator: %d total evidence items aggregated", total_items)

    return {}


def build_detective_graph():

    graph = StateGraph(AgentState)

    graph.add_node("context_builder", context_builder_node)
    graph.add_node("repo_investigator", RepoInvestigator)
    graph.add_node("doc_analyst", DocAnalyst)
    graph.add_node("vision_inspector", vision_inspector_node)
    graph.add_node("evidence_aggregator", EvidenceAggregator)

    # Entry
    graph.add_edge(START, "context_builder")

    graph.add_conditional_edges(
        "context_builder",
        fan_out_detectives,
        ["repo_investigator", "doc_analyst", "vision_inspector"],
    )

    # Fan-in: all three detectives converge on EvidenceAggregator
    graph.add_edge("repo_investigator", "evidence_aggregator")
    graph.add_edge("doc_analyst", "evidence_aggregator")
    graph.add_edge("vision_inspector", "evidence_aggregator")

    graph.add_edge("evidence_aggregator", END)

    return graph.compile()


def run_detective_audit(repo_url: str, pdf_path: str) -> AgentState:
    """
    Run the detective layer against a target repository and PDF report.

    Args:
        repo_url:  GitHub URL of the repository to audit
        pdf_path:  Local path to the PDF architectural report

    Returns:
        Final AgentState with evidences populated by all three detectives.

    Usage:
        from dotenv import load_dotenv
        load_dotenv()

        state = run_detective_audit(
            repo_url="https://github.com/username/their-repo",
            pdf_path="reports/their_interim_report.pdf",
        )

        for detective, evidence_list in state["evidences"].items():
            print(f"\\n=== {detective.upper()} ===")
            for ev in evidence_list:
                status = "✓" if ev.found else "✗"
                print(f"  [{status}] {ev.goal}")
                print(f"      confidence={ev.confidence:.2f}  location={ev.location}")
    """
    app = build_detective_graph()

    initial_state: AgentState = {
        "repo_url": repo_url,
        "pdf_path": pdf_path,
        "rubric_dimensions": [],
        "evidences": {},
        "opinions": [],
        "final_report": None,
    }

    logger.info("Starting detective audit → %s", repo_url)
    final_state = app.invoke(initial_state)
    logger.info("Detective audit complete.")

    return final_state
