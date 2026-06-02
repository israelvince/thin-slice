import logging
import os
import re
from typing import Any, Dict, Tuple

from ai import nodes as mock_nodes
from ai.models.state import HackathonAppState
from ai.slicer import slice_repo
from ai.tokenizer import estimate_tokens
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN")
DEFAULT_TARGET_REPO = os.environ.get("SLACK_TARGET_REPO", "./sandbox_repo")

if not SLACK_BOT_TOKEN or not SLACK_APP_TOKEN:
    raise RuntimeError(
        "SLACK_BOT_TOKEN and SLACK_APP_TOKEN must be set in the environment before starting the Slack app."
    )

app = App(token=SLACK_BOT_TOKEN)
pending_budget_checks: Dict[Tuple[str, str], HackathonAppState] = {}


def resolve_repo_path(target_repo: str) -> str:
    if os.path.isabs(target_repo):
        return target_repo
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(script_dir, "..", target_repo))


def extract_user_message(text: str) -> str:
    if not text:
        return ""

    parts = text.split()
    filtered = [part for part in parts if not (part.startswith("<@") and part.endswith(">"))]
    return " ".join(filtered).strip()


def format_file_list(files):
    if not files:
        return "None"
    return "\n".join(f"• `{f}`" for f in files)


def format_code_blocks(generated):
    blocks = []
    for name, content in generated.items():
        code = str(content).strip()
        if len(code) > 3000:
            code = code[:3000] + "\n...truncated..."
        blocks.append(f"*File:* `{name}`\n```\n{code}\n```")
    return "\n\n".join(blocks)


def recommend_model(token_count: int) -> str:
    if token_count < 20:
        return "claude-haiku-3-5 (fastest, cheapest)"
    if token_count <= 500:
        return "claude-sonnet-4-5 (balanced)"
    return "gemini-1.5-pro (large context, cost effective)"


def post_slices_identified(say, thread_ts: str, state: HackathonAppState):
    tokens = estimate_tokens(state.extracted_slice_context or state.user_request)
    say(
        text=(
            "🗂 *Agent 1 — Thin Slicer agent*\n"
            f"Files affected:\n{format_file_list(state.affected_files)}\n"
            f"Estimated tokens: {tokens}"
        ),
        thread_ts=thread_ts,
    )


def post_cost_estimate(say, thread_ts: str, state: HackathonAppState, token_count: int):
    say(
        text=(
            "💰 *Agent 2 — Cost Optimizator agent*\n"
            f"Estimated cost: ${state.projected_token_cost_usd:.6f}\n"
            f"Proposed model: {recommend_model(token_count)}"
        ),
        thread_ts=thread_ts,
    )


def post_budget_check(say, thread_ts: str, token_count: int):
    say(
        text=(
            "⚠️ *Agent 3 — Cost Estimator agent*\n"
            f"Token estimate: {token_count} exceeds the 20 token budget.\n"
            "💡 Thin-slice recommendation: Break this into smaller requests, for example:\n"
            "- First: Add the database schema only\n"
            "- Then: Add the API endpoints\n"
            "- Then: Add the reporting module\n"
            "Reply with *go* to proceed anyway or *no go* to cancel."
        ),
        thread_ts=thread_ts,
    )


def post_generated_code(say, thread_ts: str, state: HackathonAppState):
    if not state.generated_code_blocks:
        say(
            text="✅ *Agent 4 — Code generation complete*\nNo generated code blocks were produced.",
            thread_ts=thread_ts,
        )
        return

    say(
        text=(
            "✅ *Agent 4 — Code generation complete*\n"
            + format_code_blocks(state.generated_code_blocks)
        ),
        thread_ts=thread_ts,
    )


def reduce_slices(state: HackathonAppState) -> HackathonAppState:
    keywords = [w for w in re.findall(r"\w+", state.user_request) if len(w) > 3][:3]
    if not keywords and state.affected_files:
        keywords = state.affected_files[:2]

    repo_path = state.target_repo
    if os.path.isdir(repo_path):
        res = slice_repo(repo_path, keywords)
        state.affected_files = res.get("affected_files", [])
        state.extracted_slice_context = res.get("extracted_slice_context", "")
    else:
        state.affected_files = state.affected_files[:2]
        state.extracted_slice_context = (state.extracted_slice_context or state.user_request)[:1500]
    return state


def run_pipeline_until_generation(say, channel: str, thread_ts: str, user_message: str):
    repo_path = resolve_repo_path(DEFAULT_TARGET_REPO)
    state = HackathonAppState(user_request=user_message, target_repo=repo_path)

    try:
        state = mock_nodes.planner(state)
    except Exception as exc:
        print("planner failed:", exc, flush=True)
        import traceback; traceback.print_exc()
        raise

    try:
        post_slices_identified(say, thread_ts, state)
    except Exception as exc:
        print("post_slices_identified failed:", exc, flush=True)
        import traceback; traceback.print_exc()
        raise

    try:
        state = mock_nodes.optimizer(state)
    except Exception as exc:
        print("optimizer failed:", exc, flush=True)
        import traceback; traceback.print_exc()
        raise

    try:
        state = mock_nodes.estimator(state)
    except Exception as exc:
        print("estimator failed:", exc, flush=True)
        import traceback; traceback.print_exc()
        raise

    token_count = estimate_tokens(state.extracted_slice_context or state.user_request)
    try:
        post_cost_estimate(say, thread_ts, state, token_count)
    except Exception as exc:
        print("post_cost_estimate failed:", exc, flush=True)
        import traceback; traceback.print_exc()
        raise

    print(f"token count is {token_count}, threshold is 20")
    if token_count > 20 or not state.policy_clearance:
        state.policy_clearance = False
        pending_budget_checks[(channel, thread_ts)] = state
        try:
            post_budget_check(say, thread_ts, token_count)
        except Exception as exc:
            print("post_budget_check failed:", exc, flush=True)
            import traceback; traceback.print_exc()
            raise
        return

    try:
        state = mock_nodes.generator(state)
    except Exception as exc:
        print("generator failed:", exc, flush=True)
        import traceback; traceback.print_exc()
        raise

    try:
        post_generated_code(say, thread_ts, state)
    except Exception as exc:
        print("post_generated_code failed:", exc, flush=True)
        import traceback; traceback.print_exc()
        raise


@app.event("app_mention")
def handle_app_mention(body, say, logger):
    event = body.get("event", {})
    channel = event.get("channel")
    thread_ts = event.get("thread_ts") or event.get("ts")
    text = event.get("text", "")

    def safe_say(**kwargs):
        try:
            return say(**kwargs)
        except Exception as exc:
            print("say() failed in handle_app_mention:", exc, flush=True)
            import traceback; traceback.print_exc()
            raise

    user_message = extract_user_message(text)
    if not user_message:
        safe_say(
            text="I couldn't parse your request. Please mention me and include the text of your request.",
            thread_ts=thread_ts,
        )
        return

    safe_say(text="Processing your request... 🔍", thread_ts=thread_ts)
    try:
        run_pipeline_until_generation(safe_say, channel, thread_ts, user_message)
    except Exception as exc:
        logger.exception("Failed to run Slack pipeline")
        print("handle_app_mention failed:", exc, flush=True)
        import traceback; traceback.print_exc()
        try:
            safe_say(
                text=(
                    "Sorry, I couldn't process your request through the pipeline."
                    f"\nError: {exc}"
                ),
                thread_ts=thread_ts,
            )
        except Exception:
            pass


@app.message(re.compile(r"^(go|no go)$", re.IGNORECASE))
def handle_budget_reply(message, say, logger):
    if message.get("subtype") is not None:
        return
    if message.get("bot_id") is not None:
        return

    def safe_say(**kwargs):
        try:
            return say(**kwargs)
        except Exception as exc:
            print("say() failed in handle_budget_reply:", exc, flush=True)
            import traceback; traceback.print_exc()
            raise

    channel = message.get("channel")
    thread_ts = message.get("thread_ts") or message.get("ts")
    text = message.get("text", "").strip().lower()
    key = (channel, thread_ts)
    state = pending_budget_checks.get(key)
    if not state:
        return

    try:
        if text == "go":
            safe_say(text="Proceeding with the requested generation despite the budget warning.", thread_ts=thread_ts)
            state.policy_clearance = True
            state = mock_nodes.generator(state)
            post_generated_code(safe_say, thread_ts, state)
            pending_budget_checks.pop(key, None)
            return

        safe_say(text="Re-running the slicer with smaller slices...", thread_ts=thread_ts)
        state = reduce_slices(state)
        post_slices_identified(safe_say, thread_ts, state)

        state = mock_nodes.optimizer(state)
        state = mock_nodes.estimator(state)
        token_count = estimate_tokens(state.extracted_slice_context or state.user_request)
        post_cost_estimate(safe_say, thread_ts, state, token_count)

        print(f"token count is {token_count}, threshold is 20")
        if token_count > 20 or not state.policy_clearance:
            state.policy_clearance = False
            pending_budget_checks[key] = state
            post_budget_check(safe_say, thread_ts, token_count)
            return

        state = mock_nodes.generator(state)
        post_generated_code(safe_say, thread_ts, state)
        pending_budget_checks.pop(key, None)
    except Exception as exc:
        print("handle_budget_reply failed:", exc, flush=True)
        import traceback; traceback.print_exc()
        try:
            safe_say(
                text=(
                    "Sorry, I couldn't complete the budget flow."
                    f"\nError: {exc}"
                ),
                thread_ts=thread_ts,
            )
        except Exception:
            pass


if __name__ == "__main__":
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
