import logging
import os
import re
from typing import Dict, Tuple

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
DEFAULT_TARGET_REPO = os.environ.get("SLACK_TARGET_REPO", "./demo_repo")

# Tokens above this threshold trigger the budget-warning / go-no-go flow.
_BUDGET_THRESHOLD = int(os.environ.get("THIN_SLICE_TOKEN_THRESHOLD", "500"))

if not SLACK_BOT_TOKEN or not SLACK_APP_TOKEN:
    raise RuntimeError(
        "SLACK_BOT_TOKEN and SLACK_APP_TOKEN must be set before starting the Slack app."
    )

app = App(token=SLACK_BOT_TOKEN)
pending_budget_checks: Dict[Tuple[str, str], HackathonAppState] = {}


def resolve_repo_path(target_repo: str) -> str:
    if os.path.isabs(target_repo):
        return target_repo
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(script_dir, target_repo))


def extract_user_message(text: str) -> str:
    if not text:
        return ""
    parts = text.split()
    filtered = [p for p in parts if not (p.startswith("<@") and p.endswith(">"))]
    return " ".join(filtered).strip()


def format_file_list(files) -> str:
    if not files:
        return "None"
    return "\n".join(f"• `{f}`" for f in files)


def format_code_blocks(generated: dict) -> str:
    blocks = []
    for name, content in generated.items():
        code = str(content).strip()
        if len(code) > 3000:
            code = code[:3000] + "\n...truncated..."
        blocks.append(f"*File:* `{name}`\n```\n{code}\n```")
    return "\n\n".join(blocks)


def recommend_model(token_count: int) -> str:
    if token_count < 500:
        return "claude-haiku-4-5 (fastest, cheapest)"
    if token_count <= 4000:
        return "claude-sonnet-4-6 (balanced)"
    return "claude-sonnet-4-6 (large context)"


def post_slices_identified(say, thread_ts: str, state: HackathonAppState):
    tokens = estimate_tokens(state.extracted_slice_context or state.user_request)
    say(
        text=(
            "🗂 *Agent 1 — Thin Slicer*\n"
            f"Files affected:\n{format_file_list(state.affected_files)}\n"
            f"Estimated tokens: *{tokens:,}*"
        ),
        thread_ts=thread_ts,
    )


def post_cost_estimate(say, thread_ts: str, state: HackathonAppState, token_count: int):
    say(
        text=(
            "💰 *Agent 2 — Model Optimizer*\n"
            f"Estimated cost: *${state.projected_token_cost_usd:.6f}*\n"
            f"Recommended model: {recommend_model(token_count)}"
        ),
        thread_ts=thread_ts,
    )


def post_budget_check(say, thread_ts: str, token_count: int):
    say(
        text=(
            "⚠️ *Agent 3 — Cost Estimator*\n"
            f"Token estimate *{token_count:,}* exceeds the *{_BUDGET_THRESHOLD:,}* token threshold.\n\n"
            "💡 *Thin-slice recommendation:* Break this into smaller requests, for example:\n"
            "• First: add the schema change only\n"
            "• Then: add the API endpoints\n"
            "• Then: add tests\n\n"
            "Reply *go* to proceed anyway, or *no go* to re-slice to a smaller scope."
        ),
        thread_ts=thread_ts,
    )


def post_generated_code(say, thread_ts: str, state: HackathonAppState):
    if not state.generated_code_blocks:
        say(
            text="✅ *Agent 4 — Code Generator*\nNo code blocks were produced.",
            thread_ts=thread_ts,
        )
        return
    pr_line = f"\n\n🔗 PR: {state.pull_request_url}" if state.pull_request_url else ""
    say(
        text=(
            "✅ *Agent 4 — Code Generator*\n"
            + format_code_blocks(state.generated_code_blocks)
            + pr_line
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

    state = mock_nodes.planner(state)
    post_slices_identified(say, thread_ts, state)

    state = mock_nodes.optimizer(state)
    state = mock_nodes.estimator(state)
    token_count = estimate_tokens(state.extracted_slice_context or state.user_request)
    post_cost_estimate(say, thread_ts, state, token_count)

    if token_count > _BUDGET_THRESHOLD or not state.policy_clearance:
        state.policy_clearance = False
        pending_budget_checks[(channel, thread_ts)] = state
        post_budget_check(say, thread_ts, token_count)
        return

    state = mock_nodes.generator(state)
    post_generated_code(say, thread_ts, state)


@app.event("app_mention")
def handle_app_mention(body, say, logger):
    event = body.get("event", {})
    channel = event.get("channel")
    thread_ts = event.get("thread_ts") or event.get("ts")
    text = event.get("text", "")

    user_message = extract_user_message(text)
    if not user_message:
        say(
            text="I couldn't parse your request. Mention me with the text of your request.",
            thread_ts=thread_ts,
        )
        return

    say(text="Processing your request… 🔍", thread_ts=thread_ts)
    try:
        run_pipeline_until_generation(say, channel, thread_ts, user_message)
    except Exception as exc:
        logger.exception("Pipeline failed")
        say(
            text=f"Sorry, something went wrong.\nError: {exc}",
            thread_ts=thread_ts,
        )


@app.message(re.compile(r"^(go|no go)$", re.IGNORECASE))
def handle_budget_reply(message, say, logger):
    if message.get("subtype") or message.get("bot_id"):
        return

    channel = message.get("channel")
    thread_ts = message.get("thread_ts") or message.get("ts")
    text = message.get("text", "").strip().lower()
    key = (channel, thread_ts)
    state = pending_budget_checks.get(key)
    if not state:
        return

    try:
        if text == "go":
            say(text="Proceeding despite the budget warning…", thread_ts=thread_ts)
            state.policy_clearance = True
            state = mock_nodes.generator(state)
            post_generated_code(say, thread_ts, state)
            pending_budget_checks.pop(key, None)
            return

        say(text="Re-running the slicer with a smaller scope…", thread_ts=thread_ts)
        state = reduce_slices(state)
        post_slices_identified(say, thread_ts, state)

        state = mock_nodes.optimizer(state)
        state = mock_nodes.estimator(state)
        token_count = estimate_tokens(state.extracted_slice_context or state.user_request)
        post_cost_estimate(say, thread_ts, state, token_count)

        if token_count > _BUDGET_THRESHOLD or not state.policy_clearance:
            state.policy_clearance = False
            pending_budget_checks[key] = state
            post_budget_check(say, thread_ts, token_count)
            return

        state = mock_nodes.generator(state)
        post_generated_code(say, thread_ts, state)
        pending_budget_checks.pop(key, None)
    except Exception as exc:
        logger.exception("Budget reply handler failed")
        say(
            text=f"Sorry, I couldn't complete the budget flow.\nError: {exc}",
            thread_ts=thread_ts,
        )


if __name__ == "__main__":
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
