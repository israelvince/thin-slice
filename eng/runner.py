import argparse
import json
import os
from ai.models.state import HackathonAppState
from ai import nodes as mock_nodes
from ai.slicer import slice_repo
from ai.github_pr import create_pr_stub
from ai.langgraph_runner import run_with_langgraph, LANGGRAPH_AVAILABLE
import logging
import subprocess
import sys
from ai.services.token_monitor import TokenBudgetTracker, set_current_tracker, clear_current_tracker, BudgetExceeded

logging.basicConfig()
logger = logging.getLogger("thin_slice.runner")
logger.setLevel(logging.INFO)


def run_mock(user_request: str, target_repo: str):
    state = HackathonAppState(user_request=user_request, target_repo=target_repo)

    # If target_repo is a local directory, run the slicer to populate affected files and context.
    if os.path.isdir(target_repo):
        # naive keyword extraction from request
        keywords = [w for w in user_request.split() if len(w) > 3][:6]
        res = slice_repo(target_repo, keywords)
        state.affected_files = res.get("affected_files", [])
        state.extracted_slice_context = res.get("extracted_slice_context", "")
    else:
        # fall back to planner behavior (planner may call slicer for remote cases)
        state = mock_nodes.planner(state)

    # Set up the runtime token tracker (Agent 5) before orchestration
    # Token caps can be tuned; we use conservative defaults and the estimated projected cost
    tracker = TokenBudgetTracker(token_cap=200_000, spend_cap_usd=None, initial_model_tier="standard")
    set_current_tracker(tracker)

    # continue through optimizer, estimator, generator
    # If LangGraph is available, prefer running through it for orchestration.
    if LANGGRAPH_AVAILABLE:
        try:
            logger.info("Attempting orchestration via LangGraph...")
            final = run_with_langgraph(state)
            # graph may return a state object or dict
            if hasattr(final, "dict"):
                final_state = final
            elif isinstance(final, dict):
                # convert dict to HackathonAppState for consistency
                try:
                    final_state = HackathonAppState(**final)
                except Exception:
                    # If conversion fails, keep the original state and log
                    logger.warning("LangGraph returned a dict that couldn't be converted to HackathonAppState; using original state.")
                    final_state = state
            else:
                # Unexpected return type from LangGraph; log and fallback
                logger.warning("LangGraph runner returned unexpected type; falling back to sequential run.")
                final_state = state
        except Exception as e:
            # If LangGraph fails, fallback to sequential run with debug logging
            logger.exception("LangGraph runner failed; falling back to pure-Python sequential execution.")
            final_state = state
            try:
                final_state = mock_nodes.optimizer(final_state)
                final_state = mock_nodes.estimator(final_state)
                final_state = mock_nodes.generator(final_state)
            except BudgetExceeded as be:
                # Attempt mitigation: try switching model to cheaper tier once and retry
                logger.warning("Budget exceeded during generation: %s", be)
                cur = set()
                # Try to switch to cheaper model via tracker and retry generation once.
                switched = tracker.switch_to_cheaper()
                if switched:
                    logger.info("Retrying optimizer/estimator/generator after switching model tier to conserve budget")
                    try:
                        final_state = mock_nodes.optimizer(state)
                        final_state = mock_nodes.estimator(final_state)
                        final_state = mock_nodes.generator(final_state)
                    except BudgetExceeded as be2:
                        logger.error("Retry failed: budget still exceeded: %s", be2)
                        final_state = state
                        final_state.policy_clearance = False
                        final_state.recommendation_notes = f"Budget exceeded during generation: {be2}"
                else:
                    final_state = state
                    final_state.policy_clearance = False
                    final_state.recommendation_notes = f"Budget exceeded during generation: {be}"
    else:
        final_state = state
        try:
            final_state = mock_nodes.optimizer(final_state)
            final_state = mock_nodes.estimator(final_state)
            final_state = mock_nodes.generator(final_state)
        except BudgetExceeded as be:
            logger.warning("Budget exceeded during generation: %s", be)
            switched = tracker.switch_to_cheaper()
            if switched:
                # Retry once after switching
                try:
                    final_state = mock_nodes.optimizer(state)
                    final_state = mock_nodes.estimator(final_state)
                    final_state = mock_nodes.generator(final_state)
                except BudgetExceeded as be2:
                    logger.error("Retry after model switch still failed: %s", be2)
                    final_state = state
                    final_state.policy_clearance = False
                    final_state.recommendation_notes = f"Budget exceeded during generation: {be2}"
            else:
                final_state = state
                final_state.policy_clearance = False
                final_state.recommendation_notes = f"Budget exceeded during generation: {be}"

    # If generator produced changes and policy cleared, create a PR stub and attach URL.
    if final_state.policy_clearance and final_state.generated_code_blocks:
        # Persist generated files to disk (write into repo path if it's a directory, otherwise write locally)
        persisted_changes = {}
        repo_dir = final_state.target_repo if os.path.isdir(final_state.target_repo) else os.getcwd()
        for rel_path, content in final_state.generated_code_blocks.items():
            abs_path = os.path.join(repo_dir, rel_path)
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, 'w', encoding='utf-8') as fh:
                fh.write(content)
            persisted_changes[rel_path] = content

    # If a sandbox processing script was generated, try running it to produce outputs
        output_files = {}
        try:
            # prefer venv python if available in eng/.venv
            venv_python = None
            candidate = os.path.join(os.getcwd(), 'eng', '.venv', 'bin', 'python')
            if os.path.exists(candidate):
                venv_python = candidate
            runner_python = venv_python or sys.executable

            # Look for the sandbox_repo/process_orders.py we wrote
            proc_script = os.path.join(repo_dir, 'sandbox_repo', 'process_orders.py')
            if os.path.exists(proc_script):
                logger.info(f"Running generated processing script: {proc_script}")
                # Ensure output dir exists
                outdir = os.path.join(repo_dir, 'sandbox_repo', 'output')
                os.makedirs(outdir, exist_ok=True)
                # Run script with args: --input pointing at eng/data/ecommerce/olist_orders_dataset.csv if present
                default_input = os.path.join(os.getcwd(), 'eng', 'data', 'ecommerce', 'olist_orders_dataset.csv')
                default_output = os.path.join(outdir, 'cltv_generated.csv')
                cmd = [runner_python, proc_script, '--input', default_input, '--output', default_output]
                subprocess.run(cmd, check=False)
                # collect any csv files in output dir (prefer full outputs over generated samples)
                for fn in os.listdir(outdir):
                    if fn.endswith('.csv'):
                        p = os.path.join(outdir, fn)
                        try:
                            with open(p, 'r', encoding='utf-8') as fh:
                                output_files[os.path.join('sandbox_repo', 'output', fn)] = fh.read()
                        except Exception:
                            output_files[os.path.join('sandbox_repo', 'output', fn)] = '<could not read file>'
        except Exception:
            logger.exception('Failed to run generated processing script or collect outputs')

        # Merge outputs into the PR changes so the stub can include artifacts.
        # If both a generated sample (cltv_generated.csv) and a real full output (cltv_full.csv)
        # exist, prefer the real full output. Also avoid shipping any generator-only sample files
        # that were written into persisted_changes.
        pr_changes = dict(persisted_changes)
        # Remove any generated sample keys we don't want to publish if present
        for sample_key in list(pr_changes.keys()):
            if sample_key.endswith('sandbox_repo/process_orders.py'):
                # keep the script, but do not include any sample CSVs generated as placeholders
                continue
            if sample_key.endswith('sandbox_repo/output/cltv_generated.csv') or sample_key.endswith('cltv_sample.csv'):
                pr_changes.pop(sample_key, None)

        # Merge collected outputs, preferring cltv_full.csv when present
        # (this will overwrite placeholder entries)
        for k, v in output_files.items():
            pr_changes[k] = v

        pr_url = create_pr_stub(final_state.target_repo, branch_name="thin-slice-demo", changes=pr_changes)
        final_state.pull_request_url = pr_url

    # Clear tracker for cleanliness
    try:
        clear_current_tracker()
    except Exception:
        pass

    # Return a pydantic-serializable mapping; prefer model_dump() when available
    try:
        return final_state.model_dump() if hasattr(final_state, "model_dump") else final_state.dict()
    except Exception:
        return final_state if isinstance(final_state, dict) else getattr(final_state, "dict", lambda: {})()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mock", action="store_true", help="run mocked pipeline")
    p.add_argument("--request", default="Update risk_status to ENUM", help="user request text")
    p.add_argument("--repo", default="sandbox/tails", help="target repo id")
    args = p.parse_args()

    if args.mock:
        result = run_mock(args.request, args.repo)
        # Use Pydantic v2 compatible serialization
        try:
            serialized = result.model_dump() if hasattr(result, "model_dump") else result.dict()
        except Exception:
            # Fallback in case run_mock already returned a dict
            serialized = result if isinstance(result, dict) else getattr(result, "dict", lambda: {})()
        print(json.dumps(serialized, indent=2))
    else:
        print("No runner mode selected. Use --mock for Phase 1 demo.")


if __name__ == "__main__":
    main()
