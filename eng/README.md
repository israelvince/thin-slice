# Thin-Slice — Sliced Regen + Cost Guardrails (Demo)

This directory (`eng/`) contains a small, self-contained hackathon demo that implements a "sliced regeneration" pipeline with cost guardrails for LLM-driven code generation. The demo focuses on a data-processing thin-slice (CLTV) to make the flow concrete and testable locally.

Goals
- Demonstrate a 5-agent pipeline: Planner → Optimizer → Estimator → Generator → Continuous Cost Monitor (Agent 5).
- Show pre-generation cost estimation and a runtime guardrail that can hard-stop or attempt a mitigation if token/spend budgets are exceeded mid-generation.
- Produce a runnable sandbox artifact (a Python processing script) and tests, then collect outputs into a local PR stub for review.

High-level accomplishments
- Implemented the multi-node pipeline in `eng/ai/nodes.py`.
- Implemented cost rules in `eng/ai/services/cost_policy.py` and token estimation in `eng/ai/tokenizer.py`.
- Added a conservative, process-local runtime monitor `TokenBudgetTracker` in `eng/ai/services/token_monitor.py` (Agent 5). Generator and runner integrate with it and the system can abort or retry with a cheaper model.
- Generator produces a sandbox data-processing script `eng/sandbox_repo/process_orders.py` and unit tests in `eng/sandbox_repo/tests/` for the CLTV demo.
- Runner (`eng/runner.py`) orchestrates the pipeline, persists generated artifacts, executes the sandbox script, collects outputs, and builds a safe local PR stub (no network PR unless enabled).
- Added a simulated streaming unit test `eng/tests/test_token_monitor_streaming.py` that forces a mid-generation BudgetExceeded and verifies the unhappy-path/mitigation handling.

What's left (short)
- Provider-level streaming integration (needs a specific LLM SDK): forward token counts from streaming callbacks to `TokenBudgetTracker.consume()` for accurate mid-stream enforcement.
- Advanced mitigations: automatic context pruning heuristics, resumable generation, and multi-worker regen flows.
- Interactive review gate (Go/No-Go) for unhappy-path handling (could be a simple CLI prompt, Slack flow, or a tiny web UI).

Project layout (important files)
- `runner.py` — top-level orchestrator used by developers to run the demo (mock and demo modes).
- `ai/` — pipeline code:
  - `nodes.py` — planner/optimizer/estimator/generator node implementations.
  - `services/cost_policy.py` — pricing table and pre-generation budget checks.
  - `services/token_monitor.py` — `TokenBudgetTracker` (Agent 5).
  - `tokenizer.py` — token estimator (tiktoken optional fallback).
  - `slicer.py` — repo slicer used by the planner to extract relevant context.
- `sandbox_repo/` — generated sandbox processing script and tests; `sandbox_repo/output/` holds produced CSV artifacts (ignored by .gitignore).
- `tests/` — unit tests, including the simulated streaming test.

Quick start (local)
1. Python: create and activate a virtualenv (project has a `.venv` placeholder but use your preferred env):

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Run the demo in mock mode (or use `runner.py --mock`):

```bash
cd eng
python runner.py --mock --request "Generate CLTV per customer from ecommerce orders" --repo .
```

This will: run the planner/slicer on the local repo, run optimizer/estimator, create a `TokenBudgetTracker`, run the generator (which writes files under `sandbox_repo/`) and attempt to run the generated `sandbox_repo/process_orders.py` if present. The runner collects CSV outputs into a local PR stub and prints a JSON result.

Run tests

```bash
cd eng
. .venv/bin/activate
pytest -q
```

Design notes and contracts
- Inputs/Outputs: `HackathonAppState` (Pydantic model) flows through nodes. Each node mutates and returns a state. The runner expects `generated_code_blocks` to be a mapping path→content and will persist those into the repo when `policy_clearance` is true.
- Token guardrail contract: generator code must call `get_current_tracker()` and invoke `tracker.consume(tokens)` at points where token usage is incurred (e.g., prior to a large LLM call or during streaming callbacks). If `BudgetExceeded` is raised, generator must abort and signal the unhappy path.

How we used the dataset
- The CLTV demo uses the included Brazilian e-commerce CSVs under `eng/data/ecommerce/` for the sandbox processing script. The generator creates a `process_orders.py` that reads the dataset, aggregates order totals per customer, and writes the top-N CLTV CSV to `sandbox_repo/output/`.
- Datasets are large and are ignored by `.gitignore` so they are not accidentally committed to the repo; if you want the raw data in version control, decide and we can remove that rule.

Tools & libraries used (and why)
- Python 3.13 — runtime for scripts & tests.
- Pydantic — used for `HackathonAppState` (typed state, validation). We updated `runner.py` to use `model_dump()` for Pydantic v2
- pytest — unit tests and the simulated streaming test harness.
- Optional: `tiktoken` (or a simple heuristic fallback) for token counting in `ai/tokenizer.py`.
- GitHub CLI (`gh`) — used to set the remote default branch and to simplify repo operations.

How far have we come (short milestone map)
- Slicer & planner: implemented and run locally against the repo.
- Optimizer & estimator: simple heuristics implemented to choose model tier and project cost.
- Generator: produces runnable artifacts (sandbox script + unit test) and reports token consumption to the runtime tracker.
- Agent 5 (TokenBudgetTracker): implemented and integrated into runner/generator; supports mid-generation hard-stop and single-step mitigation (switch to cheaper tier and retry).
- Tests: unit tests added and passing, including a test that simulates mid-generation budget exceed.

Suggested next steps (prioritized)
1. Provider streaming integration (urgent for real deployments): implement an LLM SDK wrapper that forwards streaming token counts into `TokenBudgetTracker.consume()` so mid-stream stops are accurate.
2. Context pruning & resumable regen (medium): implement heuristics to drop lower-priority context and resume generation when budget is tight.
3. Human reviewer gate (optional): add a small CLI or Slack flow that lets a reviewer approve/reject unhappy-path PRs.

Contributing and maintainer checklist
- To commit new source files, add them under `ai/` and `tests/` and run `pytest`.
- Keep datasets and large outputs out of git; keep `.gitignore` updated.
- When adding provider-specific code (OpenAI, Anthropic), keep the `ai/langgraph_runner.py` shim updated so the demo can fall back to deterministic pure-Python logic when LangGraph or network is not available.

Contact / owner
- Repository owner: israelvince
- If you want, I can open a small PR that implements provider streaming for a specific provider you name.

License
- This demo does not include a formal license file. Add one if you want to open-source the project.

-----
If you want, I will now:
- commit this README and a curated minimal set of source files (ai/, tests/, sandbox_repo/), or
- implement provider streaming for a specific provider you pick (OpenAI/Anthropic/etc.), or
- create a compact developer checklist (one-pager) for onboarding new contributors.
Thin-Slice demo (eng)
=====================

This folder contains the Phase-1 demo for the Thin-Slice hackathon prototype.

Pinned runtime (use the venv under `eng/.venv`):

- langgraph==1.2.2
- langgraph-sdk==1.2.2
- langchain-core==0.0.206
- pydantic==2.7.0
- pytest==7.4.0

Quick commands (macOS zsh):

Activate venv:
```bash
source eng/.venv/bin/activate
```

Run unit tests:
```bash
python -m pytest eng/tests -q
```

Run the demo runner in mock mode (deterministic LangGraph sequence mode):
```bash
export THINSLICE_LANGGRAPH_MODE=sequence
python eng/runner.py --mock --request "Update customer onboarding profiles" --repo "."
```

Notes:
- This workspace is intentionally local-only: PR creation uses a safe stub that writes to a temp dir.
- To run with real LangGraph, install the pinned `langgraph` packages into the venv.
# Thin‑Slice Hackathon — Brainstorming & Implementation Plan

This `eng/` folder is now our focused brainstorming workspace for the Thin‑Slice hackathon demo (Sliced Regen + Cost Guardrails). The goal here is to break the project into small, testable pieces so each teammate can own discrete tasks and we can demo reliably by the submission window.

Purpose
- Align the team on a thin, demoable pipeline that isolates a minimal code slice, predicts token costs, gates expensive LLM runs, and either auto‑generates a PR or returns scope guidance.
- Provide clear, small deliverables for each phase so we can move quickly and track progress.

High‑level user flow
- Slack message (user request) → Orchestrator (LangGraph or small runner) receives a typed `HackathonAppState` → Slice Planner isolates `affected_files` + `extracted_slice_context` → Context Optimizer compresses context & selects `selected_model_tier` → Pre‑PR Cost Estimator computes `projected_token_cost_usd` and sets `policy_clearance` → Branch:
	- Go (policy_clearance=True): Code Generator runs, produces `generated_code_blocks` and opens a PR → Slack posts PR link
	- No‑Go (policy_clearance=False): Return `recommendation_notes` with scope‑split guidance to the user
	- Continuous Cost Monitor watches streaming generation and can trigger emergency cutoffs

Core components (what each does)
- Slice Planner — repo inspection, call/dependency graph, minimal file selection.
- Context & Model Optimizer — trim boilerplate, token counting/compression, choose low‑cost vs high‑reasoning model.
- Pre‑PR Cost Estimator — deterministic pricing math (tokens × $/1M) + budget gate.
- Code Generator — LLM-driven patch generation limited to the sliced context, generate unit tests, prepare PR payload.
- Continuous Cost Monitor — streaming token observer that triggers safe abort on anomalies.

Data contracts
- Keep a single source of truth: Pydantic `HackathonAppState` (see `eng_backup_*/ai/models/state.py` for the scaffold). Key fields to reference in docs and prompts:
	- `user_request`, `target_repo`
	- `affected_files`, `extracted_slice_context`
	- `selected_model_tier`, `projected_token_cost_usd`, `policy_clearance`, `recommendation_notes`
	- `generated_code_blocks`, `pull_request_url`

Quick sample `HackathonAppState` (JSON) for prompts/tests
```json
{
	"user_request": "Update customer onboarding profiles to change 'risk_status' to an ENUM",
	"target_repo": "sandbox/tails",
	"affected_files": ["profile_schema.py", "onboarding_validator.py"],
	"extracted_slice_context": "<truncated code...>",
	"selected_model_tier": "standard",
	"projected_token_cost_usd": 0.22,
	"policy_clearance": true
}
```

Tools & integrations (how we'll implement)
- Orchestration: LangGraph (preferred) — start with a small pure‑Python runner for Phase 1 and swap to LangGraph when ready.
- Typing & validation: Pydantic for all node inputs/outputs.
- Token counting: `tiktoken` or provider SDKs; fallback to a simple word→token heuristic for early demo.
- LLM providers: mock for Phase 1; OpenAI/Anthropic for Phase 2+ (API keys via env vars).
- Repo & PR: `git` CLI + GitHub REST API (OAuth app) for creating PRs.
- Messaging: Slack app with event subscriptions or incoming webhooks.

Phased implementation (concrete small tasks)

Phase 1 — Steel Thread (Days 1–3) — Minimal runnable demo
- Goal: Slack → Orchestrator → Mock Planner → Mock Estimator → Mock Generator → PR result
- Small tasks (each task = one PR):
	1. Create folders: `eng/notes/`, `eng/prompts/`, `eng/diagrams/`.
	2. Add one‑page design docs: `Planner.md`, `Estimator.md`, `Generator.md`, `Monitor.md` in `eng/notes/`.
	3. Implement a tiny pure‑Python runner `eng/runner.py` that executes node functions and passes a `HackathonAppState` instance.
	4. Add `eng/ai/nodes.py` with planner/estimator/generator functions returning deterministic or sandbox‑derived outputs.
	5. Add `eng/slack_stub.md` with example Slack payloads and responses.
	6. Unit test: cost estimator math in `eng/tests/test_cost_estimator.py`.
- Success criteria: From a Slack payload we can run the runner locally and see a final state JSON showing `policy_clearance` and `generated_code_blocks` (mocked).

Phase 2 — Agent Intelligence (Days 4–6) — Replace mocks with basic real logic
- Small tasks:
	1. Implement `eng/slicer.py` — read `target_repo` tree, run light AST extraction, and produce `extracted_slice_context`.
	2. Add token counter util `eng/tokenizer.py` (tiktoken or heuristic fallback).
	3. Implement `eng/cost_policy.py` with pricing table and budget config.
	4. Create `eng/generator_prompt.md` templates and wire a stub that calls a mocked LLM or a low‑cost endpoint.
	5. Add streaming monitor stub and basic threshold tests.
- Success criteria: Demonstrate happy/no‑go flows deterministically using a sandbox repo.

Phase 3 — Polish & Record (Days 7–9)
- Small tasks:
	1. Replace mocked LLMs with actual provider endpoints (env‑driven keys).
	2. Improve prompts and implement patch application + PR creation via GitHub API.
	3. Add CI steps for Python tests and a demo checklist.
	4. Rehearse and record two demo runs.
- Success criteria: Two recorded demo runs with PR links posted to Slack and a checklist confirming gating behavior.

Developer quickstart (copy/paste)
```bash
# create venv
python3 -m venv .venv
source .venv/bin/activate

# install minimal dev deps
pip install flask pydantic pytest

# optional tokenizer
pip install tiktoken

# run the mocked runner (after runner is scaffolded)
python eng/runner.py --mock
```

Owners & first actions (for the next meeting)
- Israel — orchestrator + runner scaffold, LangGraph integration plan
- Bryan — Slice Planner design + slicer prototype
- Cheyenne / Prasana — confirm model endpoints + pricing table
- Pilar — Slack UX messages and demo script
- Krishna — sandbox Tails access

Meeting checklist (next sync)
- Confirm team name and daily stand time
- Pick model tiers and a temporary pricing table (owner: Cheyenne/Prasana)
- Show the Phase 1 runner demo (owner: Israel)
- Assign the first three PRs (maker & reviewer)

Where the old scaffold is
- The prior code scaffolds are preserved in a timestamped backup directory at the repo root: `eng_backup_*/` — pull pieces back as needed.

Keep PRs small: one feature, one test, and one doc change per PR. If you want I can scaffold `eng/runner.py` and the `eng/ai/nodes.py` next so we have a runnable demo for the meeting.

Contributors: Israel, Bryan, Pilar, Krishna, Cheyenne, Prasana



