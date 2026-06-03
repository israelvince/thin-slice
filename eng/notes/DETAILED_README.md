Thin‑Slice — Detailed README
=============================

Purpose
-------
This document explains what was built in the `eng/` workspace, file by file, why it exists, how to run the demo for the meeting, and who/what you're waiting on.

Top-level quick commands (macOS zsh)
------------------------------------
Activate the venv (if present):
```bash
source eng/.venv/bin/activate
```

Run tests (recommended before demo):
```bash
python -m pytest eng/tests -q
```

Run the mock demo (deterministic LangGraph sequence mode):
```bash
export THINSLICE_LANGGRAPH_MODE=sequence
python eng/runner.py --mock --request "Update customer onboarding profiles" --repo "."
```

If you want to run the runner with the venv python explicitly (recommended in local demos):
```bash
/path/to/eng/.venv/bin/python eng/runner.py --mock --request "..." --repo "."
```

File-by-file explanation
------------------------

eng/README.md
  - Short summary and quick commands. Useful for attendees who need a one-liner to run the demo.

eng/DETAILED_README.md
  - This file. Full details for talking points, commands, and owners.

eng/requirements.txt
  - Pinned dependencies used for deterministic behavior in demos and tests. Key pins:
    - langgraph==1.2.2
    - langgraph-sdk==1.2.2
    - langchain-core==0.0.206
    - pydantic==2.7.0
    - pytest==7.4.0

eng/.venv/
  - Local virtualenv used during development (not committed). Use `source eng/.venv/bin/activate` to enter.

eng/runner.py
  - Entrypoint runner for Phase‑1 demos.
  - Accepts `--mock` to run the mock pipeline. Uses `ai.nodes` for sequential execution.
  - Prefers LangGraph orchestration if `THINSLICE_LANGGRAPH_MODE` is set to `sequence` or `auto` and a compatible runtime is found. Falls back to pure‑Python sequential execution on errors.
  - After the generator runs and if `policy_clearance` is True, creates a PR via the safe stub (`ai.github_pr.create_pr_stub`) and returns the final state JSON.

eng/slack_handler.py
  - Small Flask endpoint to accept Slack style payloads and call the runner. Useful for later live demos.

eng/ai/__init__.py
  - Package init for `ai` code.

eng/ai/models/state.py
  - Pydantic model `HackathonAppState` — the canonical contract passed between nodes.
  - Fields: `user_request`, `target_repo`, `affected_files`, `extracted_slice_context`, `selected_model_tier`, `projected_token_cost_usd`, `policy_clearance`, `recommendation_notes`, `generated_code_blocks`, `pull_request_url`.

eng/ai/nodes.py
  - The core synchronous node implementations used by the pure‑Python runner and the LangGraph shim.
  - Functions:
    - `planner(state)` — runs `slicer` if `target_repo` is a local path; otherwise sets context heuristically.
    - `optimizer(state)` — estimates tokens and selects a simple `selected_model_tier`.
    - `estimator(state)` — computes token estimates and calls `services.cost_policy.check_budget` to set `projected_token_cost_usd` and `policy_clearance`.
    - `generator(state)` — if policy cleared, creates demo changes in `generated_code_blocks` (non-destructive). PR creation is handled by the runner.

eng/ai/slicer.py
  - Filesystem-based slicer used for demo/sandbox runs. Scans `.py`, `.md`, `.txt` for keywords and returns `affected_files` plus `extracted_slice_context` snippets.

eng/ai/tokenizer.py
  - Token estimation utility. Uses `tiktoken` if installed, otherwise falls back to a heuristic (words→tokens) estimator.

eng/ai/services/cost_policy.py
  - Deterministic pricing table and `check_budget(tokens_in, tokens_out, tier)` which returns `projected_cost_usd` and whether it clears a fixed budget.

eng/ai/github_pr.py
  - PR creation stub for safe local demos. Writes changes to a temp directory and returns a fake PR URL. Avoids network side effects during Phase 1.

eng/ai/langgraph_runner.py
  - LangGraph integration shim with deterministic-mode support.
  - Key behaviors:
    - Reads environment variable `THINSLICE_LANGGRAPH_MODE` (values: `sequence`, `toolnode`, `auto`).
    - `sequence`: deterministic RunnableSeq composition path.
    - `toolnode`: deterministic ToolNode path (agentic tool-call style).
    - `auto`: probe installed langgraph package and try `RunnableSeq` then `ToolNode`.
    - When in deterministic mode, the shim allows test-time module overrides so we can mock RunnableSeq/ToolNode in unit tests.
  - The shim was written and tested against the pinned langgraph runtime listed above.

eng/tests/
  - Unit tests include:
    - `test_cost_policy.py` — tests for the pricing/budget math.
    - `test_langgraph_runner.py` — smoke test that ensures `run_with_langgraph` returns a `HackathonAppState` when LangGraph is available.
    - `test_langgraph_adapter_sequence.py` & `test_langgraph_adapter_toolnode.py` — adapter tests that mock `RunnableSeq` and `ToolNode` to verify deterministic shim paths.

eng/conftest.py
  - Inserts `eng/` into `sys.path` during pytest runs to make `from ai...` imports resolve for local tests.

eng/pytest.ini
  - Test discovery settings and ignore patterns to avoid scanning backups and `.venv`.

Notes on decisions and current limitations
---------------------------------------
- LLM and GitHub interactions are intentionally stubbed for safety and reproducibility.
- LangGraph API surfaces vary by release; this repo pins a tested version in `eng/requirements.txt` and provides a deterministic env-mode to avoid runtime probing surprises during demos.
- The PR stub is a deliberate decision to avoid accidental network writes during demos. Replace with `github` API integration behind a feature flag when ready.

Commands to run during the call (copy-paste)
-------------------------------------------
1) Activate venv
```bash
source eng/.venv/bin/activate
```

2) Run tests quickly
```bash
python -m pytest eng/tests -q
```

3) Run the deterministic mock demo with LangGraph sequence mode
```bash
export THINSLICE_LANGGRAPH_MODE=sequence
python eng/runner.py --mock --request "Update customer onboarding profiles" --repo "."
```

4) If you need the runner to use LangGraph installed in the venv, ensure the pinned packages from `eng/requirements.txt` are installed and set `THINSLICE_LANGGRAPH_MODE=auto` or `sequence`.

Who/what you're waiting on (owners)
-----------------------------------
- LLM & pricing confirmation (Cheyenne / Prasana) — deliver exact model tiers and token pricing for `eng/ai/services/cost_policy.py`.
- Sandbox repo with representative files (Krishna / Bryan) — provide `target_repo` path for richer slicer demos.
- Slack UX and demo script (Pilar) — finalize messages, expected outputs, and owner for posting PR links.
- Real GitHub app credentials for real PR creation (security owner) — only enable after code review.

Quick messaging script for the meeting
-------------------------------------
- Start: "We'll run a mock flow showing how a Slack request gets reduced to a minimal slice, costs are estimated, and a PR is created if under budget. The flow is deterministic and uses pinned runtime versions."
- Run the demo command from the 'Commands to run during the call' section.
- Show the runner output JSON and call out these fields: `policy_clearance`, `projected_token_cost_usd`, `generated_code_blocks`, `pull_request_url`.
- Discuss next steps and owners (who will provide sandbox, pricing, and GitHub app).

Appendix: suggested immediate follow-ups (for next 24 hours)
--------------------------------------------------------
1. Replace `model.dict()` usages with `model.model_dump()` to remove Pydantic v2 deprecation warnings.
2. Add a tiny `eng/MEETING.md` with the exact one-line run commands for attendees.
3. If you want me to, I can remove the timestamped backup `eng_backup_*` now — confirm the exact backup name and I will `rm -rf` it.

-- End of Detailed README
