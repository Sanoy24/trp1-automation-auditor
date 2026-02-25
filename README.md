# The Automaton Auditor

**FDE Challenge Week 2 — Orchestrating Deep LangGraph Swarms for Autonomous Governance**

A hierarchical multi-agent swarm that audits a GitHub repository and its accompanying PDF report with forensic precision. Detective agents collect structured evidence in parallel; the output is a typed `AgentState` ready for judicial analysis in the final submission.

---

## Prerequisites

- **Python 3.11+** — verify with `python --version`
- **git** — must be available in `PATH` for the `RepoInvestigator` to clone target repos
- **uv** — the package manager used to manage this project's dependencies

Install `uv` if you don't have it:

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Verify the installation:

```bash
uv --version
```

---

## Installation

```bash
# 1. Clone this repository
git clone https://github.com/<your-username>/automaton-auditor.git
cd automaton-auditor

# 2. Create a virtual environment
uv venv

# 3. Activate it
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\activate         # Windows

# 4. Install all dependencies
uv pip install -e .
```

All dependencies are declared in `pyproject.toml`. The `-e .` flag installs the project in editable mode so `src/` is importable directly.

---

## Configuration

Copy the example environment file and fill in your API keys:

```bash
cp .env.example .env
```

Then open `.env` and set the following:

```bash
# Required — LLM provider for detective nodes
OPENAI_API_KEY=sk-...

# Required — LangSmith observability
# Set this before your first run. Debugging parallel multi-agent
# chains without traces is extremely difficult.
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=lsv2_...
LANGCHAIN_PROJECT=automaton-auditor
```

Your `.env` file is listed in `.gitignore` and will never be committed.

---

## Running the Detective Graph

The detective graph clones a target GitHub repository, parses its PDF report, and returns structured `Evidence` objects from three parallel detectives: `RepoInvestigator`, `DocAnalyst`, and `VisionInspector`.

### Option 1 — Python script (recommended)

Create a file called `audit.py` in the project root:

```python
from dotenv import load_dotenv
load_dotenv()

from src.graph import run_detective_audit

state = run_detective_audit(
    repo_url="https://github.com/PEER_USERNAME/automaton-auditor",
    pdf_path="reports/their_interim_report.pdf",   # local path to the PDF
)

# Print all collected evidence
for detective, evidence_list in state["evidences"].items():
    print(f"\n{'='*60}")
    print(f"  {detective.upper()} — {len(evidence_list)} evidence items")
    print(f"{'='*60}")
    for ev in evidence_list:
        status = "✓ FOUND    " if ev.found else "✗ NOT FOUND"
        print(f"\n  [{status}] {ev.goal}")
        print(f"    location:   {ev.location}")
        print(f"    confidence: {ev.confidence:.0%}")
        print(f"    rationale:  {ev.rationale}")
        if ev.content:
            preview = ev.content[:120].replace("\n", " ")
            print(f"    content:    {preview}...")
```

Run it:

```bash
python audit.py
```

### Option 2 — One-liner from the command line

```bash
python -c "
from dotenv import load_dotenv; load_dotenv()
from src.graph import run_detective_audit
state = run_detective_audit(
    repo_url='https://github.com/PEER_USERNAME/automaton-auditor',
    pdf_path='reports/their_interim_report.pdf',
)
total = sum(len(v) for v in state['evidences'].values())
found = sum(e.found for v in state['evidences'].values() for e in v)
print(f'Done. {total} evidence items collected, {found} confirmed found.')
"
```

### What the graph does

```
START
  └─► ContextBuilder          loads rubric.json, initialises state
        │
        ├─► RepoInvestigator   clones repo (sandboxed), runs git log,
        │                      AST-parses graph structure and state schema
        │
        ├─► DocAnalyst         ingests PDF via RAG-lite chunking,
        │                      verifies forensic concepts, extracts file paths
        │
        └─► VisionInspector    extracts images from PDF (classification stub)
              │
        [operator.ior merges all three evidence dicts in parallel]
              │
        EvidenceAggregator     fan-in sync, validates completeness
              │
             END
```

The three detectives run **concurrently** — not sequentially. Each writes to its own key in `state["evidences"]`:

| Detective          | State key             | What it checks                                        |
| ------------------ | --------------------- | ----------------------------------------------------- |
| `RepoInvestigator` | `evidences["repo"]`   | Git history, AST structure, sandboxing, file manifest |
| `DocAnalyst`       | `evidences["doc"]`    | PDF concepts, file path cross-reference               |
| `VisionInspector`  | `evidences["vision"]` | Diagram image extraction                              |

---

## Project Structure

```
automaton-auditor/
├── src/
│   ├── state.py              # Evidence, JudicialOpinion, AgentState with reducers
│   ├── graph.py              # StateGraph: parallel fan-out → EvidenceAggregator fan-in
│   ├── nodes/
│   │   └── detectives.py     # RepoInvestigator, DocAnalyst, VisionInspector
│   └── tools/
│       ├── repo_tools.py     # Sandboxed git clone, git log, AST analysis
│       └── doc_tools.py      # PDF ingestion, RAG-lite chunking, concept querying
├── rubric.json               # Machine-readable rubric loaded by ContextBuilder
├── reports/
│   └── interim_report.pdf   # Interim architectural report
├── pyproject.toml            # uv-managed dependencies
├── .env.example              # Environment variable template (no secrets)
└── README.md
```

---

## Troubleshooting

**`ModuleNotFoundError: No module named 'src'`**
Run from the project root with `uv pip install -e .` completed. The `-e` flag makes `src/` importable.

**`git clone failed: Repository not found`**
The target repo must be public. Private repos require SSH key setup and will time out after 120 seconds.

**`PDF not found at path: ...`**
The `pdf_path` argument is a local path on your machine. Download the peer's PDF first, then pass its local path.

**LangSmith traces not appearing**
Confirm `LANGCHAIN_TRACING_V2=true` and `LANGCHAIN_API_KEY` are both set in `.env` and that `load_dotenv()` is called before importing from `src`.
