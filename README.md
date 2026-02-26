# 🏛️ Automaton Auditor — Digital Courtroom for Code Governance

A production-grade **Autonomous Governance Swarm** built on **LangGraph**. It performs deep forensic audits of GitHub repositories and PDF technical reports using a hierarchical multi-agent architecture modelled as a **Digital Courtroom**.

## 🏗️ Architecture

```
START
  ├──► RepoInvestigator  ──┐
  ├──► DocAnalyst         ──┤       (Detective Fan-Out)
  └──► VisionInspector    ──┤
                             │
               EvidenceAggregator    (Fan-In Sync)
                             │
                   [conditional routing]
                      ├── error ──► END
                      └── ok    ──► JudgeDispatch
                                       │
                       ┌───────────────┼───────────────┐
                       ▼               ▼               ▼
                  Prosecutor       Defense         TechLead   (Judge Fan-Out)
                       │               │               │
                       └───────────────┼───────────────┘
                                       ▼
                                JudgeSyncPoint
                                       │
                                 ChiefJustice             (Synthesis)
                                       │
                                  ReportNode
                                       │
                                      END
```

### Layer 1 — Detective Layer (Forensic Evidence Collection)

- **RepoInvestigator:** AST-based code analysis (not regex), sandboxed `git clone`, commit history extraction.
- **DocAnalyst:** PDF ingestion + RAG-lite chunked querying, cross-references file paths against repo evidence.
- **VisionInspector:** Multimodal LLM diagram classification (implementation required, execution optional).

### Layer 2 — Judicial Layer (Dialectical Bench)

Three judges analyze the **same evidence** for **each rubric criterion** independently:

- **Prosecutor** — "Trust No One. Assume Vibe Coding." Harsh scores (1–3).
- **Defense Attorney** — "Reward Effort and Intent." Generous scores (3–5).
- **Tech Lead** — "Does it actually work?" Decisive scores (1, 3, or 5).

All judges use `.with_structured_output(JudicialOpinion)` with retry + regex fallback.

### Layer 3 — Supreme Court (Synthesis Engine)

The **ChiefJustice** applies deterministic Python rules (not LLM prompts) to resolve conflicts:

- **Rule of Security:** `os.system` or shell injection → score capped at 3.
- **Rule of Evidence:** Detective facts overrule Defense opinions (fact supremacy).
- **Rule of Functionality:** Tech Lead carries highest weight for architecture criteria.
- **Dissent Requirement:** Score variance > 2 triggers mandatory dissent summary.

---

## 🛠️ Setup & Installation

### Prerequisites

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) package manager
- An LLM provider: **Ollama** (local), **Groq**, **OpenAI**, **Anthropic**, or **Google Gemini**

### Install Dependencies

```bash
git clone https://github.com/<your-username>/trp1-automation-auditor.git
cd trp1-automation-auditor
uv sync
```

### Configure Environment

```bash
cp .env.example .env
# Edit .env with your API keys and preferred LLM provider
```

---

## 🚀 Usage

### Audit Your Own Repository (Self-Audit)

```bash
uv run python main.py . --pdf reports/Week-two-interim-report.pdf
```

### Audit a Peer's Repository

```bash
uv run python main.py https://github.com/peer/repo \
  --pdf path/to/peer_report.pdf \
  --output-dir audit/report_onpeer_generated
```

### CLI Options

| Flag               | Description                              | Default                         |
| ------------------ | ---------------------------------------- | ------------------------------- |
| `repo_url`         | GitHub URL or local path to audit        | _(required)_                    |
| `--pdf`            | Path to PDF report for cross-referencing | `None`                          |
| `--output-dir`     | Directory for generated reports          | `audit/report_onself_generated` |
| `--rubric`         | Path to `rubric.json`                    | `rubric/rubric.json`            |
| `-v` / `--verbose` | Enable debug logging                     | `False`                         |

### Output

The auditor generates two files in the output directory:

- **`audit_report.md`** — Human-readable Markdown (Executive Summary → Criterion Breakdown → Remediation Plan)
- **`audit_report.json`** — Machine-readable Pydantic-validated JSON

---

## 📁 Project Structure

```
trp1-automation-auditor/
├── main.py                         # CLI entry point
├── pyproject.toml                  # uv-managed dependencies
├── .env.example                    # Required environment variables
├── rubric/
│   └── rubric.json                 # Machine-readable rubric (the "Constitution")
├── src/
│   ├── state.py                    # Pydantic/TypedDict state definitions
│   ├── graph.py                    # Complete LangGraph StateGraph wiring
│   ├── llm.py                      # Dynamic multi-provider LLM factory
│   ├── report_generator.py         # Markdown serializer for AuditReport
│   ├── nodes/
│   │   ├── detectives.py           # RepoInvestigator, DocAnalyst, VisionInspector
│   │   ├── judges.py               # Prosecutor, Defense, TechLead
│   │   └── justice.py              # ChiefJustice synthesis engine
│   └── tools/
│       ├── repo_tools.py           # Sandboxed git clone, AST parsing
│       └── doc_tools.py            # PDF ingestion, RAG-lite chunking
├── audit/
│   ├── report_onself_generated/    # Self-audit output
│   ├── report_onpeer_generated/    # Peer-audit output
│   └── report_bypeer_received/     # Report received from peer's agent
├── reports/
│   └── Week-two-interim-report.pdf # Interim PDF report
└── docs/
    └── TRP1 Challenge Week 2.md    # Task specification
```

---

## ⚙️ Configuration (.env)

| Variable               | Description                  | Example                              |
| ---------------------- | ---------------------------- | ------------------------------------ |
| `LLM_PROVIDER`         | Default LLM provider         | `ollama`, `groq`, `openai`, `google` |
| `LLM_MODEL`            | Default model                | `qwen2.5`, `llama-3.3-70b-versatile` |
| `JUDGE_LLM_PROVIDER`   | Override provider for judges | `groq`                               |
| `JUDGE_LLM_MODEL`      | Override model for judges    | `llama-3.3-70b-versatile`            |
| `GROQ_API_KEY`         | Groq Cloud API key           | `gsk_...`                            |
| `GOOGLE_API_KEY`       | Google Gemini API key        | `AIza...`                            |
| `LANGCHAIN_TRACING_V2` | Enable LangSmith tracing     | `true` / `false`                     |
| `LANGCHAIN_API_KEY`    | LangSmith API key            | `lsv2_pt_...`                        |

---

### 🐳 Docker Usage (Optional)

If you prefer to run the auditor in a container, a `Dockerfile` is provided:

1. **Build the image:**

    ```bash
    docker build -t automaton-auditor .
    ```

2. **Run the auditor:**
    ```bash
    # Map your .env file and output directories
    docker run --env-file .env \
      -v $(pwd)/audit:/app/audit \
      -v $(pwd)/reports:/app/reports \
      automaton-auditor https://github.com/user/repo --pdf reports/interim.pdf
    ```

## 📜 Synthesis Rules

The Chief Justice enforces these deterministic rules from `rubric.json`:

1. **Rule of Security** — Confirmed security flaws (e.g., raw `os.system`) cap the score at 3, overriding Defense arguments.
2. **Rule of Evidence** — Forensic facts always overrule subjective judicial opinions. Defense claims without Detective support are overruled.
3. **Rule of Functionality** — Tech Lead's assessment carries highest weight for architecture criteria.
4. **Dissent Requirement** — Any criterion with score variance > 2 includes a mandatory dissent summary explaining why one side was overruled.

---

## 🚀 Key Features

- **Dynamic Multi-LLM Factory:** Route requests to Ollama, Groq, OpenAI, Anthropic, or Gemini per-node.
- **AST-First Forensics:** Uses Python's `ast` module for objective code analysis (no brittle regex).
- **Robust State Reducers:** `operator.add` / `operator.ior` prevent parallel data races.
- **Fail-Safe JSON Enforcement:** Schema hints + regex fallback for structured output from any model.
- **Swarm Resilience:** Exponential backoff handles 429 rate limits during parallel fan-out.
- **LangSmith Observability:** Native tracing for debugging multi-agent reasoning chains.

---

_Built for the TRP1 Challenge Week 2 — Orchestrating Deep LangGraph Swarms for Autonomous Governance._
