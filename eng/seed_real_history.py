"""
Seed eng/knowledge/runs.jsonl with real historical data from building RegenAgent itself.
These are actual incidents from the development of this project, used as
high-confidence reference data for token estimation.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ai.knowledge_store import log_run

REAL_HISTORY = [
    {
        "request": "Generate Customer 360 demo repo with models, contracts, pipelines, validators, services, infra and tests",
        "files": [
            "models/customer_profile.py", "models/order.py", "models/payment.py",
            "models/review.py", "models/product.py", "contracts/customer_profile_contract.py",
            "contracts/data_quality_rules.py", "pipelines/customer_aggregator.py",
            "pipelines/payment_aggregator.py", "pipelines/review_aggregator.py",
            "pipelines/ltv_calculator.py", "pipelines/churn_scorer.py",
            "pipelines/segment_classifier.py", "pipelines/risk_classifier.py",
            "validators/profile_validator.py", "validators/order_validator.py",
            "validators/payment_validator.py", "validators/review_validator.py",
            "services/profile_builder.py", "services/export_service.py",
            "services/data_quality_reporter.py", "infra/config.py", "infra/logger.py",
            "infra/scheduler.py", "infra/storage_connector.py", "README.md",
        ],
        "input_tokens": 3500,
        "output_tokens": 28000,
        "total_cost": 0.4305,
        "model": "opus-4-8",
        "lines_generated": 2800,
        "user_decision": "go",
    },
    {
        "request": "Improve risk assessment with real import tracing, blast radius and test coverage",
        "files": ["slack_app.py"],
        "input_tokens": 1800,
        "output_tokens": 1200,
        "total_cost": 0.0234,
        "model": "sonnet-4-6",
        "lines_generated": 120,
        "user_decision": "auto",
    },
    {
        "request": "Redesign Agent 3 to vertical INVEST slices based on thin slice methodology",
        "files": ["slack_app.py"],
        "input_tokens": 2200,
        "output_tokens": 2500,
        "total_cost": 0.0441,
        "model": "sonnet-4-6",
        "lines_generated": 250,
        "user_decision": "auto",
    },
    {
        "request": "Add Agent 5 Token Ledger, INVEST slices, and per-agent token labels",
        "files": ["slack_app.py", "fetch_bot_prs.py"],
        "input_tokens": 3200,
        "output_tokens": 6000,
        "total_cost": 0.0996,
        "model": "sonnet-4-6",
        "lines_generated": 600,
        "user_decision": "auto",
    },
    {
        "request": "Fix MISSING_PAYMENT_DATA token consistency and slice naming bugs",
        "files": ["slack_app.py"],
        "input_tokens": 1500,
        "output_tokens": 1800,
        "total_cost": 0.0315,
        "model": "sonnet-4-6",
        "lines_generated": 180,
        "user_decision": "auto",
    },
    {
        "request": "Add multi-provider pricing for Anthropic, OpenAI and Google models",
        "files": ["ai/pricing.py"],
        "input_tokens": 800,
        "output_tokens": 1200,
        "total_cost": 0.0204,
        "model": "sonnet-4-6",
        "lines_generated": 120,
        "user_decision": "auto",
    },
    {
        "request": "Add knowledge store with seed data for 20 historical scenarios",
        "files": ["ai/knowledge_store.py", "seed_knowledge.py"],
        "input_tokens": 1400,
        "output_tokens": 3500,
        "total_cost": 0.0567,
        "model": "sonnet-4-6",
        "lines_generated": 350,
        "user_decision": "auto",
    },
    {
        "request": "Audit and fix token estimation formula using 100 lines = 1000 tokens standard",
        "files": ["ai/pricing.py", "ai/nodes.py", "seed_knowledge.py"],
        "input_tokens": 1200,
        "output_tokens": 2000,
        "total_cost": 0.0336,
        "model": "sonnet-4-6",
        "lines_generated": 200,
        "user_decision": "auto",
    },
    {
        "request": "Add engineering exposure cost model and clean up Agent 2 and Agent 3 display",
        "files": ["slack_app.py"],
        "input_tokens": 2000,
        "output_tokens": 2800,
        "total_cost": 0.0480,
        "model": "sonnet-4-6",
        "lines_generated": 280,
        "user_decision": "auto",
    },
    {
        "request": "Strip Agent 3 message to bare minimum, remove frameworks paragraphs and INVEST breakdown",
        "files": ["slack_app.py"],
        "input_tokens": 2500,
        "output_tokens": 4000,
        "total_cost": 0.0675,
        "model": "sonnet-4-6",
        "lines_generated": 400,
        "user_decision": "auto",
    },
    {
        "request": "Skip Agent 3 for LOW risk single file changes and add filename bonus scoring to slicer",
        "files": ["slack_app.py", "ai/slicer.py"],
        "input_tokens": 1300,
        "output_tokens": 1500,
        "total_cost": 0.0264,
        "model": "sonnet-4-6",
        "lines_generated": 150,
        "user_decision": "auto",
    },
    {
        "request": "Remove oversized request deflection and fix Agent 3 trigger condition for cost-only failures",
        "files": ["slack_app.py"],
        "input_tokens": 2800,
        "output_tokens": 800,
        "total_cost": 0.0204,
        "model": "sonnet-4-6",
        "lines_generated": 80,
        "user_decision": "auto",
    },
]


def main():
    for entry in REAL_HISTORY:
        log_run(
            user_request=entry["request"],
            affected_files=entry["files"],
            input_tokens=entry["input_tokens"],
            output_tokens=entry["output_tokens"],
            total_cost_usd=entry["total_cost"],
            model=entry["model"],
            slices=[],
            generated_code={f: "x" * (entry["lines_generated"] * 40 // len(entry["files"])) for f in entry["files"]},
            user_decision=entry["user_decision"],
            pr_url=None,
        )
        print(f"Logged: {entry['request'][:60]}... ({entry['input_tokens']+entry['output_tokens']:,} tokens)")

    print(f"\nSeeded {len(REAL_HISTORY)} real historical entries to eng/knowledge/runs.jsonl")


if __name__ == "__main__":
    main()
