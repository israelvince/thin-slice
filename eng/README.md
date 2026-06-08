# Thin-Slice — Sliced Regen + Cost Guardrails

A 5-agent pipeline that scopes AI code generation to the minimal affected slice of a codebase, estimates cost before running, and enforces a runtime token budget so LLM spend never spirals.

Built for the AI/works hackathon (June 2026). Submission window: June 8–12.

---

## How it works

```
Slack message
     │
     ▼
Agent 1 — Slice Planner       finds the impacted files (bounded context)
     │
     ▼
Agent 2 — Context + Model Optimizer    picks model tier, refines token estimate
     │
     ▼
Agent 3 — Pre-PR Cost Estimator        projects cost → pass / no-go decision
     │
     ├── No-go → Slack: "budget exceeded, reply go / no go"
     │              └── "no go" → re-slice smaller → loop
     │              └── "go"    → proceed anyway
     ▼
Agent 4 — Code Generator       produces the code change (LLM or mock fallback)
     │
     ▼
Agent 5 — Continuous Cost Monitor      enforces token cap mid-generation, switches
                                        model tier and retries on breach
     │
     ▼
PR stub / real GitHub PR + Slack summary
```

---

## Demo use case

**Data product:** Customer Transaction Intelligence (built on the Brazilian olist e-commerce dataset)

**Change request:**
> *"Replace `risk_level` string field with a standardized `RiskCategory` enum in the Customer Transaction Profile schema"*

The slicer finds 6 affected files across `demo_repo/` (models, pipelines, validators, tests), projects a cost of ~$0.004, clears the budget gate, and generates the enum migration.

---

## Quick start

```bash
cd eng
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# CLI demo (mock mode — no API key needed)
python runner.py --mock \
  --request "Replace risk_level string field with a standardized RiskCategory enum" \
  --repo ./demo_repo

# Run tests
pytest -q
```

---

## Slack bot

```bash
# Copy and fill in credentials
cp .env.example .env
# edit .env — add SLACK_BOT_TOKEN, SLACK_APP_TOKEN, ANTHROPIC_API_KEY

source .venv/bin/activate
python slack_app.py
```

Then mention the bot in any channel:
> `@thin-slice Replace risk_level with a RiskCategory enum in Customer Transaction Profiles`

---

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `SLACK_BOT_TOKEN` | — | Slack bot token (required for Slack) |
| `SLACK_APP_TOKEN` | — | Slack app-level token (Socket Mode) |
| `SLACK_TARGET_REPO` | `./demo_repo` | Repo the bot slices against |
| `ANTHROPIC_API_KEY` | — | Enables real LLM generation; omit for mock fallback |
| `THIN_SLICE_TOKEN_THRESHOLD` | `500` | Token count that triggers the go/no-go gate |
| `THIN_SLICE_TOKEN_CAP` | `20000` | Hard token cap for Agent 5 (runtime monitor) |
| `THIN_SLICE_SPEND_CAP_USD` | `2.00` | Hard spend cap for Agent 5 |

---

## Project layout

```
eng/
├── runner.py                   CLI orchestrator (mock + LangGraph modes)
├── slack_app.py                Slack Socket Mode bot
├── demo_repo/                  Demo data product (Customer Transaction Intelligence)
│   ├── models/customer_profile.py      schema with risk_level tech debt
│   ├── pipelines/risk_classifier.py    classification logic
│   ├── pipelines/customer_aggregator.py reads olist CSVs
│   ├── validators/profile_validator.py  schema validation
│   └── tests/
├── ai/
│   ├── nodes.py                pipeline node functions (planner/optimizer/estimator/generator)
│   ├── slicer.py               keyword-based repo slicer
│   ├── tokenizer.py            token estimator (tiktoken + fallback)
│   ├── langgraph_runner.py     LangGraph shim with pure-Python fallback
│   ├── github_pr.py            PR creation (gh CLI + local stub)
│   ├── models/state.py         HackathonAppState (Pydantic)
│   └── services/
│       ├── cost_policy.py      pricing table + budget gate
│       ├── token_monitor.py    TokenBudgetTracker (Agent 5)
│       ├── generator.py        Anthropic API code generation
│       ├── slice_planner.py    thin wrapper around slicer
│       └── orchestrator.py     thin wrapper — runs all nodes in sequence
├── sandbox_repo/               CLTV sandbox (original ecommerce demo)
├── data/ecommerce/             Brazilian olist e-commerce CSVs (gitignored)
└── tests/                      unit tests (8 passing)
```

---

## Team

Israel, Bryan, Cheyenne, Prasana, Pilar, Krishna — Thoughtworks AI/works hackathon 2026
