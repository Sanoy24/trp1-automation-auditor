import logging
import os
from typing import Any, Dict, List, Optional

from src.state import AgentState, Evidence
from src.tools.doc_tools import (
    extract_file_paths_from_text,
    extract_images_from_pdf,
    ingest_pdf,
    verify_all_forensic_concepts,
)
from src.tools.repo_tools import (
    analyze_graph_structure,
    analyze_state_schema,
    cleanup_repo,
    clone_repo_sandboxed,
    extract_git_history,
    file_exists,
    list_repo_files,
    read_file,
    scan_for_security_violations,
)

logger = logging.getLogger(__name__)

REQUIRED_INTERIM_FILES = [
    "src/state.py",
    "src/tools/repo_tools.py",
    "src/tools/doc_tools.py",
    "src/nodes/detectives.py",
    "src/graph.py",
    "pyproject.toml",
    ".env.example",
    "README.md",
]

REQUIRED_FINAL_FILES = REQUIRED_INTERIM_FILES + [
    "src/nodes/judges.py",
    "src/nodes/justice.py",
    "rubric.json",
]


def confidence_git_history(git_data: Dict) -> float:

    if git_data.get("total_commits", 0) == 0:
        return 0.0  # git log returned nothing — no evidence at all

    score = 0.0

    # Base: git log ran and returned something
    score += 0.4

    # Number of commits adds credibility — more history = richer evidence
    commit_count = git_data["total_commits"]
    if commit_count >= 10:
        score += 0.3
    elif commit_count >= 5:
        score += 0.2
    elif commit_count >= 2:
        score += 0.1
    # 1 commit = no addition

    # Clear progression across phases = strong structural signal
    if git_data.get("progression_detected"):
        score += 0.2

    # Bulk upload = all timestamps clustered = history is less trustworthy
    if git_data.get("bulk_upload_flag"):
        score -= 0.2

    return round(min(max(score, 0.0), 1.0), 2)


def confidence_file_exists(present: bool, is_critical: bool = True) -> float:
    return 1.0  # deterministic — file presence is a fact, not an estimate


def confidence_pydantic_schema(schema_analysis: Dict) -> float:

    if schema_analysis.get("parse_error"):
        return 0.0  # file cannot be parsed — no structural evidence possible

    score = 0.0

    # Each required model class confirmed via AST adds confidence
    if schema_analysis.get("evidence_found"):
        score += 0.25
    if schema_analysis.get("judicial_opinion_found"):
        score += 0.25
    if schema_analysis.get("agent_state_found"):
        score += 0.25

    # Reducers are the most critical piece — parallel safety depends on them
    if schema_analysis.get("has_reducers"):
        score += 0.25

    return round(score, 2)


def confidence_graph_structure(graph_analysis: Dict) -> float:

    if graph_analysis.get("parse_error"):
        return 0.0

    if not graph_analysis.get("has_state_graph"):
        # No StateGraph at all — everything else is moot
        return 0.1  # small residual: file exists and parsed, but no graph

    score = 0.3  # base: StateGraph confirmed

    # Edge count: more wiring = stronger evidence of real orchestration
    edge_count = len(graph_analysis.get("add_edge_calls", []))
    if edge_count >= 6:
        score += 0.3
    elif edge_count >= 3:
        score += 0.2
    elif edge_count >= 1:
        score += 0.1

    # Fan-out is the key structural requirement — parallel branches confirmed
    if graph_analysis.get("fan_out_detected"):
        score += 0.3

    # Conditional edges = error handling = production-grade design
    if graph_analysis.get("conditional_edges"):
        score += 0.1

    return round(min(score, 1.0), 2)


def confidence_security_scan(
    violations: List[Dict],
    tempfile_used: bool,
    subprocess_used: bool,
    os_system_in_source: bool,
    file_was_readable: bool,
) -> float:

    if not file_was_readable:
        return 0.0  # can't scan what we can't read

    if violations:
        return 1.0  # os.system() found via AST — irrefutable

    # No violations found — how confident are we the scan was complete?
    score = 0.5  # base: file was readable and no AST violations found

    if tempfile_used:
        score += 0.2  # sandboxing pattern present
    if subprocess_used:
        score += 0.2  # safe API pattern present
    if not os_system_in_source:
        score += 0.1  # text-level check also clean

    return round(min(score, 1.0), 2)


def confidence_pdf_concept(concept_data: Dict) -> float:

    if not concept_data.get("found"):
        return 0.0

    if concept_data.get("keyword_drop_warning"):
        # Every hit was on page 1 — exec summary only, body not engaged
        return 0.2

    score = 0.3  # base: found somewhere in the document body

    # Co-occurrence of concept keyword + explanatory marker in the same chunk
    # is the strongest signal of a real explanation (not just a mention)
    if concept_data.get("substantive_explanation"):
        score += 0.35

    # Normalised score of the best chunk — how keyword-rich is the best passage?
    top_chunks = concept_data.get("top_chunks", [])
    if top_chunks:
        best_norm = top_chunks[0].get("normalised_score", 0.0)
        # Contributes up to 0.2 based on how saturated the best chunk is
        score += round(best_norm * 0.2, 3)

    # Breadth: concept discussed across multiple chunks = richer coverage
    chunks_with_hits = concept_data.get("chunks_with_hits", 0)
    if chunks_with_hits >= 5:
        score += 0.15
    elif chunks_with_hits >= 2:
        score += 0.07

    return round(min(score, 1.0), 2)


def confidence_path_crossref(crossref_result: Dict) -> float:

    total_claimed = len(crossref_result.get("verified", [])) + len(
        crossref_result.get("hallucinated", [])
    )
    if total_claimed == 0:
        return 0.5  # nothing to verify — neutral

    return round(crossref_result.get("accuracy_score", 0.0), 2)


def RepoInvestigator(state: AgentState) -> List[Evidence]:
    repo_url = state["repo_url"]
    rubric_dimensions = state.get("rubric_dimensions", [])
    repo_dims = [
        d for d in rubric_dimensions if d.get("target_artifact") == "github_repo"
    ]

    evidences: List[Evidence] = []

    tmp_dir, clone_error = clone_repo_sandboxed(repo_url)

    if clone_error:
        evidences.append(
            Evidence(
                goal="Clone repository for analysis",
                found=False,
                content=None,
                location=repo_url,
                rationale=f"Clone failed: {clone_error}",
                confidence=0.0,
            )
        )
        return {"evidences": {"repo": evidences}}

    try:
        git_data = extract_git_history(tmp_dir)
        conf = confidence_git_history(git_data)

        evidences.append(
            Evidence(
                goal="Verify iterative development via git commit history",
                found=git_data["total_commits"] > 3
                and not git_data["bulk_upload_flag"],
                content="\n".join(
                    f"{c['hash'][:8]} {c['timestamp'][:10]} {c['message']}"
                    for c in git_data["commits"][:20]
                ),
                location=f"{repo_url}/commits",
                rationale=(
                    f"{git_data['narrative_summary']} "
                    f"[signals: commits={git_data['total_commits']}, "
                    f"progression={git_data['progression_detected']}, "
                    f"bulk_upload={git_data['bulk_upload_flag']}]"
                ),
                confidence=conf,
            )
        )

        for required_file in REQUIRED_INTERIM_FILES:
            present = file_exists(tmp_dir, required_file)
            evidences.append(
                Evidence(
                    goal=f"Verify required file exists: {required_file}",
                    found=present,
                    content=required_file if present else None,
                    location=required_file,
                    rationale=(
                        f"File {'found' if present else 'NOT FOUND'} via filesystem check. "
                        "[signal: deterministic — os.path.exists()]"
                    ),
                    confidence=confidence_file_exists(present),
                )
            )

        state_file: Optional[str] = None
        for candidate in ["src/state.py", "src/graph.py"]:
            if file_exists(tmp_dir, candidate):
                state_file = candidate
                break

        if state_file:
            schema_analysis = analyze_state_schema(os.path.join(tmp_dir, state_file))
            conf = confidence_pydantic_schema(schema_analysis)

            # One evidence item covering the full schema picture
            all_models = schema_analysis.get("pydantic_models", [])
            evidences.append(
                Evidence(
                    goal="Verify typed state schema: Evidence, JudicialOpinion, AgentState with reducers",
                    found=(
                        schema_analysis.get("evidence_found", False)
                        and schema_analysis.get("agent_state_found", False)
                        and schema_analysis.get("has_reducers", False)
                    ),
                    content=schema_analysis.get("code_snippet"),
                    location=state_file,
                    rationale=(
                        f"AST scan results — "
                        f"BaseModel subclasses: {all_models}, "
                        f"Evidence class: {schema_analysis.get('evidence_found')}, "
                        f"JudicialOpinion class: {schema_analysis.get('judicial_opinion_found')}, "
                        f"AgentState(TypedDict): {schema_analysis.get('agent_state_found')}, "
                        f"operator.ior/add reducers: {schema_analysis.get('has_reducers')}. "
                        f"[confidence driven by: which schema components confirmed via AST]"
                    ),
                    confidence=conf,
                )
            )

            if schema_analysis.get("parse_error"):
                evidences.append(
                    Evidence(
                        goal="AST parse state file",
                        found=False,
                        content=schema_analysis["parse_error"],
                        location=state_file,
                        rationale="SyntaxError — file cannot be parsed. Confidence 1.0: this is a deterministic failure.",
                        confidence=1.0,
                    )
                )
        else:
            evidences.append(
                Evidence(
                    goal="Locate state definition file (src/state.py or src/graph.py)",
                    found=False,
                    content=None,
                    location="src/state.py",
                    rationale="Neither src/state.py nor src/graph.py found. [signal: deterministic filesystem check]",
                    confidence=1.0,
                )
            )

        if file_exists(tmp_dir, "src/graph.py"):
            graph_analysis = analyze_graph_structure(
                os.path.join(tmp_dir, "src/graph.py")
            )
            conf = confidence_graph_structure(graph_analysis)

            evidences.append(
                Evidence(
                    goal="Verify StateGraph with parallel fan-out/fan-in architecture",
                    found=(
                        graph_analysis.get("has_state_graph", False)
                        and graph_analysis.get("fan_out_detected", False)
                    ),
                    content=str(graph_analysis.get("add_edge_calls", [])[:10]),
                    location="src/graph.py",
                    rationale=(
                        f"AST scan results — "
                        f"StateGraph(): {graph_analysis.get('has_state_graph')}, "
                        f"add_edge calls: {len(graph_analysis.get('add_edge_calls', []))}, "
                        f"fan_out_detected: {graph_analysis.get('fan_out_detected')}, "
                        f"conditional_edges: {graph_analysis.get('conditional_edges')}. "
                        f"[confidence driven by: StateGraph present + edge count + fan-out + conditional routing]"
                    ),
                    confidence=conf,
                )
            )

            if graph_analysis.get("parse_error"):
                evidences.append(
                    Evidence(
                        goal="AST parse graph file",
                        found=False,
                        content=graph_analysis["parse_error"],
                        location="src/graph.py",
                        rationale="SyntaxError in src/graph.py. Confidence 1.0: deterministic parse failure.",
                        confidence=1.0,
                    )
                )

        violations = scan_for_security_violations(tmp_dir)

        repo_tools_source = read_file(tmp_dir, "src/tools/repo_tools.py")
        file_was_readable = repo_tools_source is not None
        tempfile_used = "tempfile" in (repo_tools_source or "")
        subprocess_used = "subprocess.run" in (repo_tools_source or "")
        os_system_in_source = "os.system" in (repo_tools_source or "")

        conf = confidence_security_scan(
            violations=violations,
            tempfile_used=tempfile_used,
            subprocess_used=subprocess_used,
            os_system_in_source=os_system_in_source,
            file_was_readable=file_was_readable,
        )

        if violations:
            for v in violations:
                evidences.append(
                    Evidence(
                        goal="Verify no os.system() shell injection vectors",
                        found=True,  # found a VIOLATION — this is bad
                        content=v["violation"],
                        location=f"{v['file']}:{v['line']}",
                        rationale=(
                            "SECURITY VIOLATION: os.system() confirmed via AST Call node walk. "
                            "Confidence 1.0: AST cannot false-positive on os.system() calls."
                        ),
                        confidence=1.0,
                    )
                )
        else:
            evidences.append(
                Evidence(
                    goal="Verify sandboxed git clone: tempfile + subprocess.run, no os.system()",
                    found=tempfile_used and subprocess_used and not os_system_in_source,
                    content=(
                        f"tempfile import: {'✓' if tempfile_used else '✗'}  |  "
                        f"subprocess.run: {'✓' if subprocess_used else '✗'}  |  "
                        f"os.system: {'✗ (violation)' if os_system_in_source else '✓ (absent)'}"
                    ),
                    location="src/tools/repo_tools.py",
                    rationale=(
                        f"AST full-repo scan: 0 os.system() violations found. "
                        f"Text scan of repo_tools.py: tempfile={tempfile_used}, "
                        f"subprocess.run={subprocess_used}, os.system={os_system_in_source}. "
                        f"[confidence driven by: file readable + violation count + sandboxing patterns]"
                    ),
                    confidence=conf,
                )
            )

    finally:
        cleanup_repo(tmp_dir)

    logger.info("RepoInvestigator collected %d evidence items", len(evidences))
    return {"evidences": {"repo": evidences}}


def DocAnalyst(state: AgentState) -> List[Evidence]:
    """
    LangGraph node: DocAnalyst (The Paperwork Detective).

    Forensic protocols executed:
    A. Concept Verification  — Dialectical Synthesis, Fan-In/Fan-Out,
                               Metacognition, State Synchronization
    B. Cross-Reference       — extract claimed file paths, detect hallucinations

    Returns partial AgentState update: {"evidences": {"doc": [Evidence, ...]}}
    """
    pdf_path = state.get("pdf_path", "")
    evidences: List[Evidence] = []

    if not pdf_path:
        evidences.append(
            Evidence(
                goal="Locate PDF report for analysis",
                found=False,
                content=None,
                location="pdf_path not set in AgentState",
                rationale="No pdf_path provided. Confidence 1.0: this is a deterministic state check.",
                confidence=1.0,
            )
        )
        return {"evidences": {"doc": evidences}}

    ingested = ingest_pdf(pdf_path)

    if ingested.get("error"):
        evidences.append(
            Evidence(
                goal="Ingest and parse PDF report",
                found=False,
                content=ingested["error"],
                location=pdf_path,
                rationale="PDF ingestion failed. Confidence 0.0: no evidence can be collected.",
                confidence=0.0,
            )
        )
        return {"evidences": {"doc": evidences}}

    evidences.append(
        Evidence(
            goal="Verify PDF report exists and is parseable",
            found=True,
            content=f"{ingested['total_pages']} pages, {ingested['total_chunks']} chunks",
            location=pdf_path,
            rationale="PDF parsed successfully. Confidence 1.0: parse either succeeds or fails deterministically.",
            confidence=1.0,
        )
    )

    concept_results = verify_all_forensic_concepts(ingested)

    concept_display_names = {
        "dialectical_synthesis": "Dialectical Synthesis",
        "fan_in_fan_out": "Fan-In / Fan-Out",
        "metacognition": "Metacognition",
        "state_synchronization": "State Synchronization",
    }

    for concept_key, concept_data in concept_results.items():
        display_name = concept_display_names.get(concept_key, concept_key)
        conf = confidence_pdf_concept(concept_data)

        excerpt = None
        if concept_data.get("top_chunks"):
            best = concept_data["top_chunks"][0]
            excerpt = f"[Page {best['page']}, keyword_score={best.get('score',0)}] {best['text'][:300]}..."

        # Build rationale that exposes every signal driving the confidence score
        top_norm = (
            concept_data["top_chunks"][0].get("normalised_score", 0.0)
            if concept_data.get("top_chunks")
            else 0.0
        )
        signals = (
            f"found={concept_data.get('found')}, "
            f"keyword_drop_warning={concept_data.get('keyword_drop_warning')}, "
            f"substantive_explanation={concept_data.get('substantive_explanation')}, "
            f"chunks_with_hits={concept_data.get('chunks_with_hits', 0)}, "
            f"top_chunk_normalised_score={top_norm:.3f}, "
            f"total_keywords={concept_data.get('total_keywords', 0)}"
        )

        if not concept_data.get("found"):
            rationale = (
                f"'{display_name}' not found anywhere in the document. [{signals}]"
            )
        elif concept_data.get("keyword_drop_warning"):
            rationale = (
                f"KEYWORD DROP: '{display_name}' appears only on page 1 — executive summary only. "
                f"Not explained in body. [{signals}]"
            )
        elif concept_data.get("substantive_explanation"):
            rationale = (
                f"'{display_name}' found with explanatory language nearby — "
                f"substantive explanation likely. [{signals}]"
            )
        else:
            rationale = (
                f"'{display_name}' found in document body but explanatory markers weak. "
                f"Ambiguous — LLM verification recommended. [{signals}]"
            )

        evidences.append(
            Evidence(
                goal=f"Verify substantive explanation of '{display_name}' in PDF",
                found=concept_data.get("found", False)
                and not concept_data.get("keyword_drop_warning", False),
                content=excerpt,
                location=(
                    f"{pdf_path}:page_{concept_data['top_chunks'][0]['page']}"
                    if concept_data.get("top_chunks")
                    else pdf_path
                ),
                rationale=rationale,
                confidence=conf,
            )
        )

    claimed_paths = extract_file_paths_from_text(ingested["full_text"])

    if claimed_paths:

        conf = 1.0  # extraction is deterministic regex — either found or not
        evidences.append(
            Evidence(
                goal="Extract file paths claimed in PDF for hallucination cross-reference",
                found=True,
                content="\n".join(claimed_paths),
                location=pdf_path,
                rationale=(
                    f"Regex extraction found {len(claimed_paths)} file path(s) in PDF text: "
                    f"{', '.join(claimed_paths[:8])}{'...' if len(claimed_paths) > 8 else ''}. "
                    f"[confidence 1.0: regex extraction is deterministic]"
                ),
                confidence=conf,
            )
        )
    else:
        evidences.append(
            Evidence(
                goal="Extract file paths claimed in PDF for hallucination cross-reference",
                found=False,
                content=None,
                location=pdf_path,
                rationale="No src/*.py file paths found in PDF text. [confidence 1.0: deterministic]",
                confidence=1.0,
            )
        )

    logger.info("DocAnalyst collected %d evidence items", len(evidences))
    return {"evidences": {"doc": evidences}}


def vision_inspector_node(state: AgentState) -> Dict[str, Any]:
    """
    LangGraph node: VisionInspector (The Diagram Detective).

    Forensic protocol:
    - Extract images from the PDF
    - Classify each image: LangGraph StateGraph diagram, sequence diagram,
      or generic flowchart?
    - Verify the diagram explicitly shows parallel fan-out/fan-in

    INTERIM: Image extraction runs. Multimodal LLM classification is stubbed.
    Confidence is derived from the number of images extracted — not hardcoded.
    """
    pdf_path = state.get("pdf_path", "")
    evidences: List[Evidence] = []

    if not pdf_path:
        evidences.append(
            Evidence(
                goal="Extract and classify architectural diagrams from PDF",
                found=False,
                content=None,
                location="pdf_path not set in AgentState",
                rationale="No PDF path. Confidence 1.0: deterministic state check.",
                confidence=1.0,
            )
        )
        return {"evidences": {"vision": evidences}}

    images = extract_images_from_pdf(pdf_path)
    image_count = len(images)

    # Confidence reflects how much we actually know:
    # - 0 images: we found nothing to classify — low confidence (ambiguous)
    # - images found but not classified: we know they exist, but not what they show
    # The stub cannot claim high confidence because classification hasn't run.
    if image_count == 0:
        conf = 0.4  # PDF parsed but no images — could mean no diagrams exist
        rationale = (
            "No images extracted from PDF. Either no diagrams are present or "
            "extraction failed. [signal: image_count=0, classification=not_run]"
        )
        found = False
    else:
        # We found images but haven't classified them — we know they exist,
        # we don't know if they show parallel flow. Confidence stays low.
        conf = round(min(0.2 + (image_count * 0.05), 0.4), 2)
        rationale = (
            f"{image_count} image(s) extracted from PDF. "
            f"Multimodal LLM classification not yet executed — "
            f"cannot determine if diagrams show parallel StateGraph flow. "
            f"[signals: image_count={image_count}, classification=stub, "
            f"confidence grows slightly with image count but stays low until classification runs]"
        )
        found = True

    evidences.append(
        Evidence(
            goal="Classify architectural diagrams for parallel StateGraph flow",
            found=found,
            content=f"{image_count} image(s) extracted. Classification deferred to final submission.",
            location=pdf_path,
            rationale=rationale,
            confidence=conf,
        )
    )

    return {"evidences": {"vision": evidences}}
