import os
from typing import List

from .models.state import HackathonAppState
from .services.cost_policy import check_budget
from .services.token_monitor import get_current_tracker, BudgetExceeded
from .slicer import slice_repo
from .tokenizer import estimate_tokens
from .github_pr import create_pr_stub


def _extract_keywords(text: str) -> List[str]:
    # naive keyword extraction: split and filter short words
    parts = [p.strip(".,()\"'`)" ) for p in text.split()]
    keywords = [p for p in parts if len(p) > 3]
    return keywords[:6]


def planner(state: HackathonAppState) -> HackathonAppState:
    # If target_repo is a local path, run the slicer; otherwise use keywords heuristic
    repo_path = state.target_repo
    keywords = _extract_keywords(state.user_request)
    if os.path.isdir(repo_path):
        res = slice_repo(repo_path, keywords)
        state.affected_files = res.get("affected_files", [])
        state.extracted_slice_context = res.get("extracted_slice_context", "")
    else:
        # fallback deterministic behavior: include request as context
        state.affected_files = ["<unspecified>"]
        state.extracted_slice_context = state.user_request
    return state


def optimizer(state: HackathonAppState) -> HackathonAppState:
    # Estimate tokens for extracted context and select a model tier
    est_tokens = estimate_tokens(state.extracted_slice_context or state.user_request)
    # simple rule: >50k tokens -> high_reasoning
    state.selected_model_tier = "standard" if est_tokens < 50000 else "high_reasoning"
    return state


def estimator(state: HackathonAppState) -> HackathonAppState:
    tokens_in = estimate_tokens(state.extracted_slice_context or state.user_request)
    tokens_out = 2000
    result = check_budget(tokens_in, tokens_out, state.selected_model_tier)
    state.projected_token_cost_usd = result.get("projected_cost_usd", 0.0)
    state.policy_clearance = result.get("policy_clearance", False)
    if not state.policy_clearance:
        state.recommendation_notes = "Projected cost exceeds budget. Suggest splitting into smaller changes."
    return state


def generator(state: HackathonAppState) -> HackathonAppState:
    # If not cleared, do nothing
    if not state.policy_clearance:
        return state
    changes = {}
    repo_path = state.target_repo

    # If the request mentions data/customer or CLTV, prefer sandbox_repo for the demo
    text = (state.user_request or "").lower()
    data_keywords = ["customer", "cltv", "order", "orders", "ecommerce", "metric"]
    is_data_request = any(k in text for k in data_keywords)

    if is_data_request:
        # Create a patch for sandbox_repo/process_orders.py
        patch_path = "sandbox_repo/process_orders.py"
        try:
            with open(os.path.join("sandbox_repo", "process_orders.py"), 'r', encoding='utf-8') as fh:
                content = fh.read()
        except Exception:
            # If the file isn't present in the repo, generate a minimal implementation
            content = """# Generated process_orders.py\nimport csv\nfrom collections import defaultdict\n\n# Minimal generated implementation\ndef compute_customer_total(input_csv, output_csv):\n    totals = defaultdict(float)\n    with open(input_csv, newline='', encoding='utf-8') as fh:\n        reader = csv.DictReader(fh)\n        for r in reader:\n            cid = r.get('customer_id')\n            try:\n                val = float(r.get('price') or 0)\n            except Exception:\n                val = 0.0\n            if cid:\n                totals[cid] += val\n    with open(output_csv, 'w', newline='', encoding='utf-8') as outfh:\n        writer = csv.writer(outfh)\n        writer.writerow(['customer_id', 'total_spent'])\n        for cid, total in list(totals.items())[:10]:\n            writer.writerow([cid, f"{total:.2f}"])\n"""
        changes[patch_path] = content
        # Also generate a simple unit test that exercises compute_customer_total
        test_path = "sandbox_repo/tests/test_process_orders_generated.py"
        test_content = '''# Auto-generated unit test for process_orders.py
import os
import csv
from sandbox_repo import process_orders


def test_compute_customer_total_tmp(tmp_path):
    # create a tiny input CSV
    input_csv = tmp_path / "orders_small.csv"
    output_csv = tmp_path / "out.csv"
    rows = [
        {"customer_id": "C1", "price": "10"},
        {"customer_id": "C2", "price": "5"},
        {"customer_id": "C1", "price": "3"},
    ]
    with open(input_csv, 'w', newline='', encoding='utf-8') as fh:
        writer = csv.DictWriter(fh, fieldnames=["customer_id", "price"])
        writer.writeheader()
        for r in rows:
            writer.writerow(r)

    process_orders.compute_customer_total(str(input_csv), str(output_csv))

    assert os.path.exists(output_csv)
    with open(output_csv, newline='', encoding='utf-8') as fh:
        reader = csv.DictReader(fh)
        data = {r['customer_id']: float(r['total_spent']) for r in reader}
    assert data.get('C1') == 13.0
    assert data.get('C2') == 5.0
'''
        changes[test_path] = test_content
    # Simulate generation work that reports token consumption to the runtime tracker (Agent 5)
    tracker = get_current_tracker()
    try:
        if tracker:
            # consume input tokens for the slice context
            tokens_for_context =  max(1, len((state.extracted_slice_context or state.user_request).split()) * 2)
            tracker.consume(tokens_for_context)
            # simulate generation tokens (output)
            tracker.consume(2000)
    except BudgetExceeded as e:
        # If the budget is exceeded mid-generation, stop and mark policy clearance false
        state.policy_clearance = False
        state.recommendation_notes = f"Generation aborted: {e}"
        # Do not persist generated changes
        state.generated_code_blocks = {}
        return state
    else:
        # Fallback: behave like previous generator (edit affected files)
        for f in state.affected_files:
            # if repo path exists, read the file content; otherwise create a placeholder
            if os.path.isdir(repo_path):
                candidate = os.path.join(repo_path, f)
                try:
                    with open(candidate, 'r', encoding='utf-8') as fh:
                        content = fh.read()
                    # append a small comment to indicate change (non-destructive for demo)
                    new_content = content + "\n# Updated by Thin-Slice demo\n"
                except Exception:
                    new_content = "# new file generated by Thin-Slice demo\n"
            else:
                new_content = "# new file generated by Thin-Slice demo\n"
            changes[f] = new_content

    # Populate generated_code_blocks; PR creation will be handled by the runner
    state.generated_code_blocks = changes
    state.pull_request_url = None
    return state
