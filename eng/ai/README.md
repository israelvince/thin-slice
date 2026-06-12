# ai/ — Pipeline modules

The five-agent pipeline for Thin-Slice sliced regen with cost control.

| Module | Agent | What it does |
|---|---|---|
| `nodes.py` | 1–4 | `planner`, `optimizer`, `estimator`, `generator` node functions |
| `slicer.py` | 1 | Keyword-based repo walker; skips `.venv`/`__pycache__`; caps at 30 files |
| `tokenizer.py` | 2 | Token estimation via tiktoken (or word-count fallback) |
| `langgraph_runner.py` | — | LangGraph shim; falls back to pure-Python if LangGraph not installed |
| `github_pr.py` | — | Creates a real PR via `gh` CLI when repo has a remote; local stub otherwise |
| `models/state.py` | — | `HackathonAppState` — Pydantic model flowing through all nodes |
| `services/cost_policy.py` | 3 | Pricing table + `check_budget()` gate |
| `services/token_monitor.py` | 5 | `TokenBudgetTracker` — runtime token/spend cap, `switch_to_cheaper()` mitigation |
| `services/generator.py` | 4 | Anthropic API code generation (`haiku-4-5` / `sonnet-4-6`) |
| `services/slice_planner.py` | 1 | Thin wrapper around `slicer.py` |
| `services/orchestrator.py` | — | Runs all four nodes in sequence |
