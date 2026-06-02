# Thin-Slice (demo)

This repository contains a compact hackathon demo implementing "Sliced Regen + Cost Guardrails" — a small pipeline that shows how to slice a change request, estimate LLM token cost, generate code, and enforce runtime token cost guardrails.

Important: the interactive demo and runnable code live in the `eng/` folder. Open that folder for full details, examples, and the developer README.

Quick links
- `eng/README.md` — Full, in-depth project README (how to run, test, what's done, and what's left).
- `eng/runner.py` — Entrypoint used for the demo orchestration (mock mode available).
- `eng/ai/` — Pipeline source: planner, optimizer, estimator, generator, and the TokenBudgetTracker (Agent 5).
- `eng/tests/` — Unit tests (run with `pytest`).

Quick start (summary)
```bash
cd eng
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python runner.py --mock --request "Generate CLTV per customer from ecommerce orders" --repo .
```

If you'd prefer the full `eng/README.md` moved to the repository root (so the root page shows the full docs), I can do that for you.
