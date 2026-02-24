import ast
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def clone_repo_sandboxed(repo_url: str) -> Tuple[str, Optional[str]]:

    tmp_dir = tempfile.mkdtemp(prefix="auditor_clone_")
    try:
        result = subprocess.run(
            ["git", "clone", "--depth=1", repo_url, tmp_dir],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            error = result.stderr.strip() or "Unknown git error"
            logger.error("Clone failed for %s: %s", repo_url, error)
            return None, f"git clone failed: {error}"

        logger.info("Cloned %s → %s", repo_url, tmp_dir)
        return tmp_dir, None

    except subprocess.TimeoutExpired:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None, "git clone timed out after 120s — repo may require authentication"
    except FileNotFoundError:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None, "git not found in PATH — cannot clone repository"
    except Exception as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None, f"Unexpected clone error: {exc}"


def cleanup_repo(tmp_dir: str) -> None:
    """Remove the sandboxed clone directory after analysis is complete."""
    shutil.rmtree(tmp_dir, ignore_errors=True)
    logger.debug("Cleaned up sandbox: %s", tmp_dir)


def extract_git_history(repo_path: str) -> Dict[str, Any]:

    result = subprocess.run(
        ["git", "log", "--oneline", "--reverse", "--format=%H|||%s|||%ci"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=30,
    )

    commits = []
    for line in result.stdout.strip().splitlines():
        if "|||" not in line:
            continue
        parts = line.split("|||")
        if len(parts) == 3:
            commits.append(
                {
                    "hash": parts[0].strip(),
                    "message": parts[1].strip(),
                    "timestamp": parts[2].strip(),
                }
            )

    total = len(commits)

    # Progression keywords mapped to expected phases
    phase_keywords = {
        "setup": ["init", "setup", "environment", "scaffold", "structure", "config"],
        "tooling": ["tool", "ast", "parse", "git", "clone", "pdf", "doc", "ingest"],
        "orchestration": [
            "graph",
            "node",
            "langgraph",
            "state",
            "detective",
            "agent",
            "swarm",
        ],
    }

    phases_found = {phase: False for phase in phase_keywords}
    for commit in commits:
        msg_lower = commit["message"].lower()
        for phase, keywords in phase_keywords.items():
            if any(kw in msg_lower for kw in keywords):
                phases_found[phase] = True

    progression_detected = sum(phases_found.values()) >= 2

    # Bulk upload detection: all commits within 5 minutes of each other
    bulk_upload_flag = False
    if total > 1:
        try:
            from datetime import datetime

            timestamps = []
            for c in commits:
                # Parse ISO 8601 with timezone offset
                ts_str = c["timestamp"][:19]  # "2024-01-15 10:30:00"
                timestamps.append(datetime.fromisoformat(ts_str))
            if timestamps:
                span_seconds = (max(timestamps) - min(timestamps)).total_seconds()
                bulk_upload_flag = span_seconds < 300 and total > 3
        except Exception:
            pass

    # Build narrative summary
    if total == 0:
        narrative = "No commits found — repository may be empty."
    elif total == 1:
        narrative = f"Single commit detected: '{commits[0]['message']}'. No development progression visible."
    elif bulk_upload_flag:
        narrative = f"{total} commits detected but all within 5 minutes — bulk upload pattern, not iterative development."
    elif progression_detected:
        narrative = f"{total} commits with clear progression narrative detected across setup, tooling, and orchestration phases."
    else:
        narrative = f"{total} commits detected but progression narrative is weak — phases not clearly delineated in commit messages."

    return {
        "commits": commits,
        "total_commits": total,
        "phases_found": phases_found,
        "progression_detected": progression_detected,
        "bulk_upload_flag": bulk_upload_flag,
        "narrative_summary": narrative,
    }


def list_repo_files(repo_path: str) -> List[str]:
    """
    Return all Python file paths in the repo relative to the root.
    Used by the RepoInvestigator to build a manifest before targeted scanning.
    """
    root = Path(repo_path)
    return [
        str(p.relative_to(root)) for p in root.rglob("*.py") if ".git" not in p.parts
    ]


def file_exists(repo_path: str, relative_path: str) -> bool:
    """Check whether a specific file exists in the cloned repo."""
    return (Path(repo_path) / relative_path).exists()


def read_file(repo_path: str, relative_path: str) -> Optional[str]:
    """
    Read a file from the cloned repo as text.
    Returns None if the file does not exist or cannot be decoded.
    Never executes, imports, or evals the content.
    """
    target = Path(repo_path) / relative_path
    if not target.exists():
        return None
    try:
        return target.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        logger.warning("Could not read %s: %s", relative_path, exc)
        return None


def analyze_graph_structure(file_path: str) -> Dict[str, Any]:
    """
    Parse a Python file with Python's ast module and extract structural facts
    about the LangGraph implementation.

    This is NOT regex — it operates on the syntax tree, not the raw text.
    A comment that says 'use StateGraph' will not produce a positive result.
    Only actual syntax-level usage is captured.

    Returns:
        has_state_graph:     bool — StateGraph() instantiated
        pydantic_models:     List[str] — class names inheriting BaseModel
        has_typed_dict:      bool — any class inherits TypedDict
        has_reducers:        bool — Annotated[..., operator.ior/add] found
        add_edge_calls:      List[(str, str)] — (from_node, to_node) pairs
        fan_out_detected:    bool — same source appears in multiple add_edge calls
        conditional_edges:   bool — add_conditional_edges() call found
        parse_error:         Optional[str] — set if ast.parse() fails
    """
    result: Dict[str, Any] = {
        "has_state_graph": False,
        "pydantic_models": [],
        "has_typed_dict": False,
        "has_reducers": False,
        "add_edge_calls": [],
        "fan_out_detected": False,
        "conditional_edges": False,
        "parse_error": None,
    }

    source = Path(file_path).read_text(encoding="utf-8", errors="replace")

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        result["parse_error"] = f"SyntaxError at line {exc.lineno}: {exc.msg}"
        return result

    for node in ast.walk(tree):

        # --- StateGraph instantiation ---
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "StateGraph":
                result["has_state_graph"] = True
            if isinstance(func, ast.Attribute):
                if func.attr == "add_edge" and len(node.args) == 2:
                    try:
                        from_node = ast.unparse(node.args[0])
                        to_node = ast.unparse(node.args[1])
                        result["add_edge_calls"].append((from_node, to_node))
                    except Exception:
                        pass
                if func.attr == "add_conditional_edges":
                    result["conditional_edges"] = True

        # --- Class inheritance ---
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                if isinstance(base, ast.Name):
                    if base.id == "BaseModel":
                        result["pydantic_models"].append(node.name)
                    if base.id == "TypedDict":
                        result["has_typed_dict"] = True
                # Handle: class Foo(pydantic.BaseModel)
                if isinstance(base, ast.Attribute) and base.attr == "BaseModel":
                    result["pydantic_models"].append(node.name)

        # Looks for: Annotated[..., operator.ior] or Annotated[..., operator.add]
        if isinstance(node, ast.Subscript):
            try:
                unparsed = ast.unparse(node)
                if "operator.ior" in unparsed or "operator.add" in unparsed:
                    result["has_reducers"] = True
            except Exception:
                pass

    # Fan-out: same source node in multiple add_edge calls
    sources = [edge[0] for edge in result["add_edge_calls"]]
    if len(sources) != len(set(sources)):
        result["fan_out_detected"] = True

    return result


def analyze_state_schema(file_path: str) -> Dict[str, Any]:
    """
    Specifically analyse a state definition file (e.g. src/state.py).
    Looks for Evidence, JudicialOpinion, AgentState definitions.

    Returns:
        models_found:       List[str] — all BaseModel subclass names
        agent_state_found:  bool
        evidence_found:     bool
        judicial_opinion_found: bool
        has_reducers:       bool
        code_snippet:       str — the AgentState class definition if found
    """
    result = {
        "models_found": [],
        "agent_state_found": False,
        "evidence_found": False,
        "judicial_opinion_found": False,
        "has_reducers": False,
        "code_snippet": None,
        "parse_error": None,
    }

    source = Path(file_path).read_text(encoding="utf-8", errors="replace")

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        result["parse_error"] = f"SyntaxError: {exc.msg} at line {exc.lineno}"
        return result

    lines = source.splitlines()

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            name = node.name
            base_names = []
            for base in node.bases:
                if isinstance(base, ast.Name):
                    base_names.append(base.id)
                elif isinstance(base, ast.Attribute):
                    base_names.append(base.attr)

            if "BaseModel" in base_names:
                result["models_found"].append(name)
                if name == "Evidence":
                    result["evidence_found"] = True
                if name == "JudicialOpinion":
                    result["judicial_opinion_found"] = True

            if "TypedDict" in base_names and name == "AgentState":
                result["agent_state_found"] = True
                # Extract the class source lines
                start = node.lineno - 1
                end = node.end_lineno
                result["code_snippet"] = "\n".join(lines[start:end])

        if isinstance(node, ast.Subscript):
            try:
                unparsed = ast.unparse(node)
                if "operator.ior" in unparsed or "operator.add" in unparsed:
                    result["has_reducers"] = True
            except Exception:
                pass

    return result


def scan_for_security_violations(repo_path: str) -> List[Dict[str, str]]:

    violations = []
    root = Path(repo_path)

    for py_file in root.rglob("*.py"):
        if ".git" in py_file.parts:
            continue
        source = py_file.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue

        rel_path = str(py_file.relative_to(root))

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "system"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "os"
                ):
                    violations.append(
                        {
                            "file": rel_path,
                            "line": str(node.lineno),
                            "violation": "os.system() call — shell injection vector. Use subprocess.run() with a list argument.",
                        }
                    )

    return violations
