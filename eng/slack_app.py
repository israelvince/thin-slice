import logging
import os
from typing import Any

from ai.langgraph_runner import run_with_langgraph
from ai.models.state import HackathonAppState
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN")
DEFAULT_TARGET_REPO = os.environ.get("SLACK_TARGET_REPO", "sandbox_repo")

if not SLACK_BOT_TOKEN or not SLACK_APP_TOKEN:
    raise RuntimeError(
        "SLACK_BOT_TOKEN and SLACK_APP_TOKEN must be set in the environment before starting the Slack app."
    )

app = App(token=SLACK_BOT_TOKEN)


def extract_user_message(text: str) -> str:
    if not text:
        return ""

    parts = text.split()
    filtered = [part for part in parts if not (part.startswith("<@") and part.endswith(">"))]
    return " ".join(filtered).strip()


def format_response(result: Any) -> str:
    if isinstance(result, dict):
        data = result
    elif hasattr(result, "dict"):
        try:
            data = result.dict()
        except Exception:
            data = {}
    else:
        data = {}

    notes = data.get("recommendation_notes")
    pr_url = data.get("pull_request_url")
    generated = data.get("generated_code_blocks") or {}

    lines = []
    if pr_url:
        lines.append(f"*Pull request:* {pr_url}")
    else:
        lines.append(":warning: LangGraph did not create a pull request in this run.")

    if notes:
        lines.append(f"*Notes:* {notes}")

    if generated:
        lines.append(f"*Generated code blocks:* {len(generated)}")
        for name, content in generated.items():
            lines.append(f"\n*File:* `{name}`")
            lines.append("```\n" + str(content).strip() + "\n```")

    if not lines:
        lines.append("LangGraph completed but returned no structured output.")

    return "\n".join(lines)


@app.event("app_mention")
def handle_app_mention(body, say, logger):
    event = body.get("event", {})
    text = event.get("text", "")
    thread_ts = event.get("ts")

    user_message = extract_user_message(text)
    if not user_message:
        say(
            text="I couldn't parse your request. Please mention me and include the text of your request.",
            thread_ts=thread_ts,
        )
        return

    say(text=f"Processing your request: `{user_message}`...", thread_ts=thread_ts)

    try:
        state = HackathonAppState(user_request=user_message, target_repo=DEFAULT_TARGET_REPO)
        result = run_with_langgraph(state)
        response_text = format_response(result)
        say(text=response_text, thread_ts=thread_ts)
    except Exception as exc:
        logger.exception("Failed to run LangGraph pipeline")
        say(
            text=(
                "Sorry, I couldn't process your request through the LangGraph pipeline."
                f"\nError: {exc}"
            ),
            thread_ts=thread_ts,
        )


if __name__ == "__main__":
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
