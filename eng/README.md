# Thin-Slice — Risk-Aware Code Shipping

A 5-agent Slack pipeline that turns a natural language change request into a risk-assessed, dependency-aware shipping plan — and generates the code to execute it.

When you mention the bot in Slack with a change request, it finds the affected files, measures the blast radius, applies DORA and Shape Up principles to decide whether the change is safe to ship as-is, and if not, slices it into independently verifiable vertical strips. It then generates real code changes and opens a PR.

Built for the AI/works hackathon — June 2026.

---

## Pipeline

```
Slack: @thin-slice <change request>
        │
        ├── Oversized? → scoping guidance, no generation
        │
        ▼
Agent 1 — Thin Slicer
  Keyword-scores every file in the repo, returns the bounded set of
  affected files and their content as context.

        ▼
Agent 2 — Model Optimizer
  Computes a structural token estimate (context + request + system
  overhead × output multiplier). Selects model tier. Displays
  complexity as Nx safe-ship threshold.

        ▼
Agent 3 — Risk Assessment
  Builds a dependency graph from import statements. Calculates blast
  radius (inbound dependency count). Computes risk level (HIGH/MEDIUM/LOW)
  from layer coverage and test gaps. Reads DORA, Shape Up, and Strangler
  Fig principles from knowledge files and applies them to this specific
  change. For simple annotation requests (log, print, comment, docstring)
  with zero blast radius: "safe to ship as-is". For complex changes:
  generates vertically-sliced shipping recommendations where each slice
  title is derived from the business value descriptions in the code itself.

        ├── Annotation / LOW risk → go/no-go only
        ├── HIGH risk → sliced plan → slice N or go / no go
        │
        ▼
Agent 4 — Code Generator
  Tries Anthropic API → Ollama → smart rule-based fallback.
  Fallback handles: logging, print statements, error handling,
  enum migration, null/missing data, docstrings, input validation,
  comment at top of file — all from actual file content.
  Creates a PR stub (gh CLI → local stub).

        ▼
Agent 5 — Continuous Cost Monitor
  Enforces token cap mid-generation. Switches model tier and retries
  on breach. Surfaces BudgetExceeded to the pipeline.
```

---

## Demo use cases

**Data product:** Customer Transaction Intelligence — built on the Brazilian olist e-commerce dataset (99k customers, 100k orders, payments, reviews).

| Request | What happens |
|---|---|
| `migrate risk_level to RiskCategory enum` | HIGH risk, 4-file coupled change. Agent 3 detects model+core coupling, slices into: define contract → enforce it → prove it works. Generator migrates all string returns to enum values across classifier, model, and validator. |
| `Add a log statement to the churn scorer when a customer scores above the CHURN_RISK_THRESHOLD` | Annotation fast-path. Finds `CHURN_RISK_THRESHOLD`, locates the threshold check in `flag_churn_risks()`, injects `logger.warning()` with correct variable names and indentation. |
| `The customer aggregator needs to handle missing review scores gracefully` | Null handling. Adds zero-division guards for `avg_review_score`, appends `count_profiles_missing_reviews()`. |
| `add a docstring to the risk classifier` | Annotation fast-path. Finds `risk_classifier.py`, adds docstring to any function missing one. |
| `Redesign the entire customer data pipeline to support real-time streaming...` | Scope detection fires before Agent 1. Returns scoping guidance with three concrete options to narrow the request. |

---

## Quick start

```bash
cd eng
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run tests
pytest -q

# CLI — no API key needed (smart rule-based fallback)
python runner.py \
  --request "migrate risk_level to RiskCategory enum" \
  --repo ./demo_repo

# Slack bot
cp .env.example .env          # fill in SLACK_BOT_TOKEN, SLACK_APP_TOKEN
python slack_app.py
```

To use real LLM generation, add `ANTHROPIC_API_KEY` to `.env`.

---

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `SLACK_BOT_TOKEN` | — | Slack bot token (required for Slack mode) |
| `SLACK_APP_TOKEN` | — | Slack app-level token (Socket Mode) |
| `SLACK_TARGET_REPO` | `./demo_repo` | Repo the bot slices against |
| `ANTHROPIC_API_KEY` | — | Enables real LLM generation; omit for smart fallback |
| `OLLAMA_MODEL` | — | Ollama model name (e.g. `codellama`); tried before fallback |
| `THIN_SLICE_TOKEN_THRESHOLD` | `1500` | Structural token count that triggers the go/no-go gate |
| `THIN_SLICE_TOKEN_CAP` | `20000` | Hard token cap for Agent 5 (runtime monitor) |
| `THIN_SLICE_SPEND_CAP_USD` | `2.00` | Hard spend cap for Agent 5 |

---

## Project layout

```
eng/
├── runner.py                       CLI orchestrator (pure Python + LangGraph paths)
├── slack_app.py                    Slack Socket Mode bot — all agent Slack output
├── conftest.py                     pytest path setup
│
├── ai/
│   ├── agent3.py                   Pure Agent 3 logic — Slack-free, fully testable
│   │                               (blast_radius, dep_graph, risk, slicing, token est.)
│   ├── nodes.py                    Pipeline node functions (planner → optimizer →
│   │                               estimator → generator)
│   ├── slicer.py                   Keyword-scored repo walker (score ≥ 4 threshold)
│   ├── tokenizer.py                Token estimator (tiktoken + word-count fallback)
│   ├── langgraph_runner.py         LangGraph adapter with pure-Python fallback
│   ├── github_pr.py                PR creation (gh CLI → local stub)
│   ├── models/state.py             HackathonAppState (Pydantic)
│   └── services/
│       ├── cost_policy.py          Pricing table + budget gate
│       ├── token_monitor.py        TokenBudgetTracker — Agent 5 runtime enforcer
│       ├── generator.py            LLM generation (Anthropic → Ollama → None)
│       ├── demo_generator.py       Smart rule-based fallback — intent-aware, file-aware
│       ├── slice_planner.py        Thin wrapper around slicer
│       └── orchestrator.py         Thin wrapper — runs all nodes in sequence
│
├── knowledge/
│   ├── dora_metrics.txt            DORA principles on batch size and stability
│   ├── shape_up.txt                Shape Up: fixed time, variable scope
│   └── strangler_fig.txt           Strangler Fig: independent, reversible slices
│
├── demo_repo/                      Customer Transaction Intelligence data product
│   ├── models/
│   │   ├── customer_profile.py     Core schema — risk_level tech debt lives here
│   │   ├── order.py                Order model
│   │   ├── transaction.py          Transaction model
│   │   └── ...
│   ├── pipelines/
│   │   ├── customer_aggregator.py  Reads olist CSVs → CustomerProfile objects
│   │   ├── risk_classifier.py      Assigns risk_level from spend + review history
│   │   ├── churn_scorer.py         Churn risk score with CHURN_RISK_THRESHOLD
│   │   └── ...
│   ├── validators/
│   │   └── profile_validator.py    Data quality gate before downstream consumers
│   ├── services/                   Export, PII masking, data quality reporting
│   └── tests/
│
├── sandbox_repo/                   CLTV sandbox (process_orders.py + tests)
│
├── data/ecommerce/                 Brazilian olist CSVs (in repo, geolocation excluded)
│   ├── olist_customers_dataset.csv
│   ├── olist_orders_dataset.csv
│   ├── olist_order_payments_dataset.csv
│   ├── olist_order_reviews_dataset.csv
│   ├── olist_order_items_dataset.csv
│   ├── olist_products_dataset.csv
│   └── olist_sellers_dataset.csv
│
└── tests/                          49 tests — all passing
    ├── test_agent3_features.py     42 tests against real demo repo file content
    ├── test_cost_policy.py
    ├── test_token_monitor_streaming.py
    ├── test_sandbox_processing.py
    └── ...
```

---

## What makes it interesting

**Dependency-aware slicing** — slices are built from actual import graph analysis, not folder names. A file only shares a slice with another if they genuinely import each other.

**Blast radius signal** — measures downstream impact (how many other changed files depend on each file), not just file count. A model file with 4 consumers has blast radius 4, not 1.

**Review-time framing** — cost displayed as estimated review time and complexity ratio (`2.3x safe-ship threshold`), not dollar fractions. Numbers developers can reason about.

**Knowledge-grounded advice** — DORA, Shape Up, and Strangler Fig principles are loaded from knowledge files at startup and applied to the specific change, not used as generic boilerplate.

**Smart fallback generation** — no API key needed. The demo generator reads actual file content and applies real transformations: adds logger calls at the right indent, migrates enum types end-to-end, adds null guards, wraps functions in try/except, etc.

**Scope protection** — requests that span too many concerns (real-time streaming, full redesign, cross-cutting refactors) are caught before Agent 1 runs and returned with concrete scoping guidance.

---

## Team

Israel, Bryan, Cheyenne, Prasana, Pilar, Krishna — Thoughtworks AI/works hackathon 2026
