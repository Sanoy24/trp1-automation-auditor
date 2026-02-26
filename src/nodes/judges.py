"""
judges.py — The Dialectical Bench of the Digital Courtroom.

Three Judge nodes with completely distinct personas analyse the SAME
evidence independently for EACH rubric criterion.  Their outputs are
``JudicialOpinion`` Pydantic objects appended to ``AgentState.opinions``
via the ``operator.add`` reducer — safe under parallel execution.

Each judge uses ChatOllama.with_structured_output(JudicialOpinion) so
the LLM is forced to return validated JSON, with retry on parse failure.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from langchain_core.messages import HumanMessage, SystemMessage

from src.llm import get_structured_llm
from src.state import AgentState, Evidence, JudicialOpinion

logger = logging.getLogger(__name__)

MAX_RETRIES = 3  # retry on malformed structured output


# ---------------------------------------------------------------------------
# System Prompts — COMPLETELY DISTINCT per the rubric requirement
# ---------------------------------------------------------------------------

PROSECUTOR_SYSTEM_PROMPT = """You are THE PROSECUTOR in a Digital Courtroom for code auditing.

## Core Philosophy
"Trust No One. Assume Vibe Coding."

## Your Objective
Scrutinise the forensic evidence for gaps, security flaws, laziness, and
shortcuts.  Your job is to find what is WRONG, MISSING, or DANGEROUS.

## Evaluation Rules
- If the rubric asks for "Parallel Orchestration" and evidence shows a
  linear pipeline, you MUST argue for Score 1.
- If Judges return freeform text instead of Pydantic models, charge the
  defendant with "Hallucination Liability".
- If `os.system()` calls are found, charge with "Security Negligence".
- If the StateGraph is purely linear (no fan-out), charge with
  "Orchestration Fraud" — max Score = 1 for architecture.
- Look for bypassed structure, missing error handling, hardcoded paths.

## Output Format
For the criterion you are judging, provide:
- score: An integer 1-5 (you should lean towards harsh scores: 1-3)
- argument: Your prosecutorial argument citing SPECIFIC evidence locations
- cited_evidence: List of evidence location strings that support your case

Be adversarial.  Be specific.  Cite file paths and code snippets.
"""

DEFENSE_SYSTEM_PROMPT = """You are THE DEFENSE ATTORNEY in a Digital Courtroom for code auditing.

## Core Philosophy
"Reward Effort and Intent. Look for the Spirit of the Law."

## Your Objective
Highlight creative workarounds, deep thought, and engineering effort —
even if the implementation is imperfect.

## Evaluation Rules
- If code is buggy but the report shows deep understanding of LangGraph
  state reducers, argue for a higher score based on conceptual mastery.
- Look at Git History evidence.  If commits tell a story of struggle and
  iteration, argue for a higher score based on "Engineering Process".
- If the StateGraph fails to compile due to a minor edge validation error
  but the underlying AST parsing logic is sophisticated, argue the
  engineer achieved deep code comprehension.
- If the Chief Justice synthesis is an LLM prompt rather than hardcoded
  rules but the Judge personas are highly distinct, argue for partial
  credit based on "Role Separation" success.

## Output Format
For the criterion you are judging, provide:
- score: An integer 1-5 (you should lean towards generous scores: 3-5)
- argument: Your defense argument citing SPECIFIC evidence of effort and quality
- cited_evidence: List of evidence location strings that support your case

Be optimistic.  Find the silver lining.  Cite concrete evidence of effort.
"""

TECH_LEAD_SYSTEM_PROMPT = """You are THE TECH LEAD in a Digital Courtroom for code auditing.

## Core Philosophy
"Does it actually work? Is it maintainable?"

## Your Objective
Evaluate architectural soundness, code cleanliness, and practical viability.
Ignore the "vibe" and the "struggle".  Focus on ARTIFACTS and OUTCOMES.

## Evaluation Rules
- Is `operator.add` reducer actually used to prevent data overwriting?
  If not: "Technical Debt" — Score 3 max.
- Are tool calls isolated and safe (tempfile sandboxing, subprocess.run
  with error handling)?  If not: "Security Negligence".
- If Pydantic models are used correctly with proper field types and
  validation: good architecture.
- If plain dicts are used for complex nested state: "Dict Soup" — Score 3.
- You are the tie-breaker.  If Prosecutor says "1" (security flaw) and
  Defense says "5" (great effort), YOU assess the Technical Debt
  realistically.

## Output Format
For the criterion you are judging, provide:
- score: An integer 1, 3, or 5 (you prefer decisive scores, rarely 2 or 4)
- argument: Your pragmatic technical assessment with specific code references
- cited_evidence: List of evidence location strings that support your assessment

Be realistic.  Be practical.  Focus on what WORKS and what DOESN'T.
"""


# ---------------------------------------------------------------------------
# Evidence formatter — converts Evidence objects to readable text for LLM
# ---------------------------------------------------------------------------


def _format_evidence_for_prompt(evidences: Dict[str, List[Evidence]]) -> str:
    """Flatten all Evidence objects into a structured text block for the LLM."""
    lines = []
    for source_key, evidence_list in evidences.items():
        lines.append(f"\n=== Evidence Source: {source_key.upper()} ===")
        for ev in evidence_list:
            status = "✓ FOUND" if ev.found else "✗ NOT FOUND"
            lines.append(f"\n[{status}] Goal: {ev.goal}")
            lines.append(f"  Location: {ev.location}")
            lines.append(f"  Confidence: {ev.confidence}")
            lines.append(f"  Rationale: {ev.rationale}")
            if ev.content:
                # Truncate very long content to avoid context overflow
                content_preview = ev.content[:500]
                if len(ev.content) > 500:
                    content_preview += "... [truncated]"
                lines.append(f"  Content: {content_preview}")
    return "\n".join(lines)


def _build_criterion_prompt(
    criterion: Dict,
    evidence_text: str,
) -> str:
    """Build the human message for a single rubric criterion."""
    # Include target_artifact so the judge knows which evidence source matters most
    target = criterion.get('target_artifact', 'all')
    judicial_logic = criterion.get('judicial_logic', '')

    return (
        f"## Criterion: {criterion['name']} (ID: {criterion['id']})\n\n"
        f"### Target Artifact: {target}\n"
        f"Focus your analysis primarily on evidence from the **{target}** source.\n\n"
        f"### Forensic Instruction\n{criterion.get('forensic_instruction', 'N/A')}\n\n"
        f"### Judicial Logic\n{judicial_logic or 'Apply your persona guidelines.'}\n\n"
        f"### Success Pattern\n{criterion.get('success_pattern', 'N/A')}\n\n"
        f"### Failure Pattern\n{criterion.get('failure_pattern', 'N/A')}\n\n"
        f"### Collected Evidence\n{evidence_text}\n\n"
        f"Based on the evidence above and the success/failure patterns, "
        f"render your judicial opinion for this criterion. "
        f"Your score must be an integer from 1 to 5. "
        f"Your argument must cite specific evidence locations. "
        f"Your cited_evidence must be a list of location strings from the evidence above."
    )


# ---------------------------------------------------------------------------
# Generic judge runner — shared logic, only the persona prompt differs
# ---------------------------------------------------------------------------


def _run_judge(
    judge_name: str,
    system_prompt: str,
    state: AgentState,
) -> Dict[str, Any]:
    """Run a single judge across all rubric dimensions.

    Returns a partial state update: {"opinions": [JudicialOpinion, ...]}
    """
    evidences = state.get("evidences", {})
    rubric_dimensions = state.get("rubric_dimensions", [])
    evidence_text = _format_evidence_for_prompt(evidences)

    opinions: List[JudicialOpinion] = []
    
    # We use both structured and base LLM for robustness
    from src.llm import get_llm
    base_llm = get_llm(role="judge", temperature=0.1)
    # Note: .with_structured_output is the preferred LangChain way, 
    # but we add manual fallback for local models (Ollama) that occasionally fail.
    structured_llm = base_llm.with_structured_output(JudicialOpinion)

    for idx, criterion in enumerate(rubric_dimensions, 1):
        try:
            criterion_id = criterion.get("id", "unknown")
            logger.info("%s starting analysis of '%s' (%d/%d)", 
                        judge_name, criterion_id, idx, len(rubric_dimensions))
            
            criterion_prompt = _build_criterion_prompt(criterion, evidence_text)
            
            # Hyper-explicit JSON instruction for small models
            schema_hint = (
                "\n\nYOUR RESPONSE MUST BE A SINGLE VALID JSON OBJECT with this structure:\n"
                "{\n"
                '  "score": <int 1-5>,\n'
                '  "argument": "<detailed string using evidence>",\n'
                '  "cited_evidence": ["<location1>", "<location2>"]\n'
                "}\n"
                "Do NOT include any text before or after the JSON."
            )
            
            messages = [
                SystemMessage(content=system_prompt + schema_hint),
                HumanMessage(content=criterion_prompt),
            ]

            import time
            backoff = 1.0  # start with 1s backoff

            # Retry loop for structured output parsing failures
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    # 1. Try native structured output
                    result = structured_llm.invoke(messages)
                    
                    # If we got a result, wrap it
                    opinion = JudicialOpinion(
                        judge=judge_name,
                        criterion_id=criterion["id"],
                        score=result.score,
                        argument=result.argument,
                        cited_evidence=result.cited_evidence,
                    )
                    opinions.append(opinion)
                    logger.info("%s scored '%s': %d/5", judge_name, criterion["id"], opinion.score)
                    break

                except Exception as exc:
                    logger.debug("%s structured invoke failed: %s. Attempting manual extraction...", judge_name, exc)
                    
                    # 2. Manual JSON Recovery from raw text
                    try:
                        raw_response = base_llm.invoke(messages)
                        content = raw_response.content
                        
                        import json
                        import re
                        
                        # Look for { ... } anywhere in the string
                        json_match = re.search(r"(\{.*\})", content, re.DOTALL)
                        if json_match:
                            raw_json = json.loads(json_match.group(1))
                            opinion = JudicialOpinion(
                                judge=judge_name,
                                criterion_id=criterion["id"],
                                score=int(raw_json.get("score", 3)),
                                argument=raw_json.get("argument", content),
                                cited_evidence=raw_json.get("cited_evidence", [])
                            )
                            opinions.append(opinion)
                            logger.info("%s manually recovered JSON for '%s'", judge_name, criterion["id"])
                            break
                    except Exception as recovery_exc:
                        logger.warning("%s manual recovery attempt %d failed: %s", judge_name, attempt, recovery_exc)

                    if attempt == MAX_RETRIES:
                        logger.error("%s failed all %d attempts for '%s'.", judge_name, MAX_RETRIES, criterion["id"])
                        opinions.append(
                            JudicialOpinion(
                                judge=judge_name,
                                criterion_id=criterion["id"],
                                score=3,
                                argument=f"[STRUCTURAL FAILURE] {judge_name} could not produce valid JSON. Error: {exc}",
                                cited_evidence=[],
                            )
                        )
                    else:
                        # Exponential backoff before retry
                        logger.info("%s backing off for %.1fs before retry...", judge_name, backoff)
                        time.sleep(backoff)
                        backoff *= 2.0
        except Exception as fatal_exc:
            logger.error("%s encountered a fatal error while processing '%s': %s", 
                         judge_name, criterion_id, fatal_exc)
            # Add a failure opinion so the graph can continue
            opinions.append(
                JudicialOpinion(
                    judge=judge_name,
                    criterion_id=criterion_id,
                    score=1,
                    argument=f"[FATAL ERROR] Judge crashed while processing this criterion. Error: {fatal_exc}",
                    cited_evidence=[],
                )
            )

    logger.info("%s completed with %d opinions.", judge_name, len(opinions))
    return {"opinions": opinions}


# ---------------------------------------------------------------------------
# LangGraph node functions — one per persona
# ---------------------------------------------------------------------------


def prosecutor_node(state: AgentState) -> Dict[str, Any]:
    """LangGraph node: The Prosecutor (The Critical Lens).

    "Trust No One. Assume Vibe Coding."
    Scrutinises evidence for gaps, security flaws, and laziness.
    Returns harsh scores (1–3) backed by specific evidence citations.
    """
    logger.info("=== PROSECUTOR entering courtroom ===")
    return _run_judge("Prosecutor", PROSECUTOR_SYSTEM_PROMPT, state)


def defense_node(state: AgentState) -> Dict[str, Any]:
    """LangGraph node: The Defense Attorney (The Optimistic Lens).

    "Reward Effort and Intent. Look for the Spirit of the Law."
    Highlights creative workarounds, deep thought, and effort.
    Returns generous scores (3–5) backed by evidence of engineering effort.
    """
    logger.info("=== DEFENSE ATTORNEY entering courtroom ===")
    return _run_judge("Defense", DEFENSE_SYSTEM_PROMPT, state)


def tech_lead_node(state: AgentState) -> Dict[str, Any]:
    """LangGraph node: The Tech Lead (The Pragmatic Lens).

    "Does it actually work? Is it maintainable?"
    Evaluates architectural soundness and practical viability.
    Returns decisive scores (1, 3, or 5) based on technical facts.
    """
    logger.info("=== TECH LEAD entering courtroom ===")
    return _run_judge("TechLead", TECH_LEAD_SYSTEM_PROMPT, state)
