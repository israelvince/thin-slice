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
_BUDGET_THRESHOLD = int(os.environ.get("THIN_SLICE_TOKEN_THRESHOLD", "1500"))

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
    def parse_snippets(context: str) -> Dict[str, str]:
        snippets: Dict[str, str] = {}
        for chunk in context.split("\n# FILE: "):
            if not chunk.strip():
                continue
            first_line, _, rest = chunk.partition("\n")
            filename = first_line.strip()
            snippets[filename] = rest
        return snippets

    def file_risk_level(filename: str, snippet: str) -> str:
        lower = filename.lower()
        high_keywords = ["model", "schema", "database", "core", "order", "payment"]
        med_keywords = ["api", "endpoint", "route", "service"]
        low_keywords = ["util", "helper", "test", "log", "config"]
        is_test_file = "test" in lower
        has_test_coverage = bool(re.search(r"\b(def test_|pytest|unittest|class Test)", snippet, re.I))
        if any(k in lower for k in high_keywords) or (not is_test_file and not has_test_coverage):
            return "high"
        if any(k in lower for k in med_keywords):
            return "medium"
        if any(k in lower for k in low_keywords) or is_test_file:
            return "low"
        return "medium"

    def describe_file_value(filename: str, request: str, risk: str) -> str:
        lower = filename.lower()
        if "schema" in lower or "database" in lower or "model" in lower or "payment" in lower:
            return f"Updates the underlying {filename} so the system can {request}."
        if "api" in lower or "endpoint" in lower or "route" in lower:
            return f"Adds the API surface needed to support {request}."
        if "service" in lower:
            return f"Implements backend service behavior for {request}."
        if "test" in lower:
            return f"Adds coverage so {request} stays reliable over time."
        if "util" in lower or "helper" in lower or "log" in lower or "config" in lower:
            return f"Improves the supporting infrastructure around {request}."
        if risk == "high":
            return f"Makes the core system changes required to fully deliver {request}."
        if risk == "medium":
            return f"Builds the main capability needed to deliver {request}."
        return f"Delivers the safest part of {request} in {filename}."

    def worth_it_line(user_intent: str, folder_name: str) -> str:
        return f"Delivers the '{user_intent}' change to the {folder_name} layer first — lowest risk entry point"

    state = None
    for (_, ts), stored_state in pending_budget_checks.items():
        if ts == thread_ts:
            state = stored_state
            break
    if state is None:
        # Fallback if exact state lookup fails; use the current token count only.
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
        return

    files = state.affected_files or []
    snippets = parse_snippets(state.extracted_slice_context or "")
    total_files = len(files)
    total_cost = state.projected_token_cost_usd
    pythonuser_intent = state.user_request[:60] if len(state.user_request) > 60 else state.user_request

    def folder_category(filename: str) -> str:
        lower = filename.lower()
        if "readme" in lower:
            return "readme"
        if "models/" in lower or "schema" in lower or "model" in lower:
            return "models"
        if "validators/" in lower or "validator" in lower or "services/" in lower or "pipeline" in lower:
            return "core"
        if "tests/" in lower or lower.startswith("test_") or "/test_" in lower:
            return "tests"
        if "docs/" in lower or lower.endswith(".md"):
            return "docs"
        if "config" in lower:
            return "config"
        return "other"

    def clean_label(filename: str) -> str:
        base = os.path.basename(filename)
        name, _ = os.path.splitext(base)
        return name.replace("_", " ").replace("-", " ")

    def extract_request_keywords(request: str) -> list[str]:
        lower = request.lower()
        ordered_terms = [
            "input validation",
            "error handling",
            "validation",
            "logging",
            "refactor",
            "migration",
            "endpoint",
            "schema",
            "test",
            "auth",
            "payment",
            "order",
            "profile",
            "service",
            "pipeline",
            "security",
            "performance",
            "data",
        ]
        found: list[str] = []
        for term in ordered_terms:
            if term in lower and term not in found:
                found.append(term)
        if not found:
            found = [w for w in re.findall(r"\w+", lower) if len(w) > 3][:3]
        return found

    def format_intent_phrase(intents: list[str]) -> str:
        if not intents:
            return "the requested feature"
        if len(intents) == 1:
            return intents[0]
        if len(intents) == 2:
            return f"{intents[0]} and {intents[1]}"
        return ", ".join(intents[:-1]) + f" and {intents[-1]}"

    request_keywords = extract_request_keywords(state.user_request)
    primary_keyword = request_keywords[0] if request_keywords else "feature"
    primary_phrase = format_intent_phrase(request_keywords)
    model_keyword = clean_label(next((f for f in files if folder_category(f) == "models"), files[0] if files else "feature"))
    if model_keyword.endswith("s"):
        model_keyword = model_keyword.rstrip("s")

    folder_counts = {
        "models": sum(1 for f in files if folder_category(f) == "models"),
        "core": sum(1 for f in files if folder_category(f) == "core"),
        "tests": sum(1 for f in files if folder_category(f) == "tests"),
        "docs": sum(1 for f in files if folder_category(f) == "docs"),
        "config": sum(1 for f in files if folder_category(f) == "config"),
        "readme": sum(1 for f in files if folder_category(f) == "readme"),
        "other": sum(1 for f in files if folder_category(f) == "other"),
    }

    high_folder_names = [name for name in ("models", "core") if folder_counts[name] > 0]
    high_folder_phrase = ", ".join("models" if x == "models" else "validators/pipelines" for x in high_folder_names)
    if not high_folder_phrase:
        high_folder_phrase = "feature-critical areas"

    no_test_count = sum(
        1
        for f in files
        if not bool(re.search(r"\b(def test_|pytest|unittest|class Test)", snippets.get(f, ""), re.I))
        and "test" not in f.lower()
    )

    risk_score = 0
    if folder_counts["models"] > 0:
        risk_score += 2
    if folder_counts["core"] > 0:
        risk_score += 2
    risk_score += min(no_test_count, 2)
    overall_risk = "HIGH" if risk_score >= 3 else "MEDIUM" if risk_score == 2 else "LOW"

    touched_phrase = f"{total_files} file{'s' if total_files != 1 else ''} touched across {high_folder_phrase}"
    test_phrase = f"{no_test_count} of {total_files} file{'s' if total_files != 1 else ''} have no test coverage"
    risk_explanation = (
        f"Why {overall_risk}: {touched_phrase} — a change this broad risks cascading failures. {test_phrase}."
    )

    slice_1 = [f for f in files if folder_category(f) == "models"]
    slice_2 = [f for f in files if folder_category(f) == "core"]
    slice_3 = [f for f in files if folder_category(f) in {"tests", "docs", "config", "other"}]
    readme_files = [f for f in files if folder_category(f) == "readme"]

    if not slice_1 and slice_2:
        slice_1.append(slice_2.pop(0))
    if not slice_2 and slice_3:
        slice_2.append(slice_3.pop(0))

    # Build vertical slices based on capabilities in the user request
    capabilities = extract_request_keywords(state.user_request)
    all_files = [f for f in files if folder_category(f) != "readme"]

    # Group files by capability match
    slice_groups = []
    used_files = set()

    for i, capability in enumerate(capabilities[:5], start=1):
        matched = [
            f for f in all_files
            if capability.lower() in f.lower() or capability.lower() in (snippets.get(f, "").lower())
            and f not in used_files
        ]
        if matched:
            used_files.update(matched)
            slice_groups.append((f"Slice {i}", capability.title(), matched, capability))

    # Any remaining files go into a final slice
    remaining = [f for f in all_files if f not in used_files]
    if remaining:
        slice_groups.append((f"Slice {len(slice_groups)+1}", "Supporting changes", remaining, "other"))

    if total_files == 0:
        say(
            text=(
                "⚠️ *Agent 3 — Cost × Risk Assessment*\n"
                f"💰 Total cost: *${total_cost:.6f}* | 🔢 Tokens: *{token_count:,}* | Budget: *{_BUDGET_THRESHOLD}*\n"
                f"⚠️ Risk level: *{overall_risk}*\n{risk_explanation}\n"
                "📊 Verdict: Too costly and risky to ship in one shot.\n\n"
                "No affected files were identified, so there is nothing to slice here."
            ),
            thread_ts=thread_ts,
        )
        return

    cost_per_file = total_cost / total_files if total_files else 0.0
    slice_costs = [round(cost_per_file * len(group_files), 6) for _, _, group_files, _ in slice_groups]
    remainder = round(total_cost - sum(slice_costs), 6)
    for idx in range(len(slice_costs) - 1, -1, -1):
        if slice_costs[idx] > 0 or idx == len(slice_costs) - 1:
            slice_costs[idx] = round(slice_costs[idx] + remainder, 6)
            break

    def slice_description(filename: str) -> str:
        category = folder_category(filename)
        entity = clean_label(filename)
        if category == "models":
            return f"Update the {entity} schema to support the new field constraints."
        if category == "core":
            if "validator" in filename.lower() or "validators/" in filename.lower():
                return "Add validation rules that enforce clean inputs before they reach the pipeline."
            return f"Modify the {entity} to apply the change at the processing stage."
        if category == "tests":
            return "Extend the test suite to cover the new paths and edge cases."
        if category == "readme":
            return "Update README.md when done."
        return f"Apply changes to {entity}."

    def worth_it_line(filename: str) -> str:
        category = folder_category(filename)
        if category == "models":
            return "Foundation — everything else depends on this."
        if category == "core" and ("validator" in filename.lower() or "validators/" in filename.lower()):
            return "Enforcement point — catches bad data early."
        if category == "core":
            return "Core logic — delivers the visible change."
        if category == "tests":
            return "Safety net — confirms nothing broke."
        return "Supports the feature build."

    slice_lines = []
    for slice_index, (label, title, group_files, group_label) in enumerate(slice_groups, start=1):
        if not group_files:
            continue
        group_cost = slice_costs[slice_index - 1]
        files_str = ", ".join(f"`{f}`" for f in group_files)
        bullet_lines = "\n".join(
            f"   • {os.path.basename(f).replace('_',' ').replace('.py','')} — {slice_description(f)}"
            for f in group_files
        )
        worth_line = worth_it_line(group_files[0])
        emoji = "🟢" if slice_index == 1 else "🟡" if slice_index == 2 else "🔵"
        slice_lines.append(
            f"{emoji} *{label} — {title}* | Cost: ${group_cost:.6f} | Files: {files_str}\n"
            f"{bullet_lines}\n"
            f"   _{worth_line}_"
        )

    smart_12_cost = round(slice_costs[0] + slice_costs[1], 6)
    smart_3_cost = slice_costs[2]
    if slice_1 and slice_2:
        slice_12_files = f"`{slice_1[0]}` + `{slice_2[0]}`"
    elif slice_1:
        slice_12_files = f"`{slice_1[0]}`"
    elif slice_2:
        slice_12_files = f"`{slice_2[0]}`"
    else:
        slice_12_files = "slices 1+2"
    readme_note = "\n\n📝 Don't forget to update README.md" if readme_files else ""

    say(
        text=(
            "⚠️ *Agent 3 — Cost × Risk Assessment*\n"
            f"\n💰 Total cost: *${total_cost:.6f}* | 🔢 Tokens: *{token_count:,}* | Budget: *{_BUDGET_THRESHOLD}*\n"
            f"⚠️ Risk level: *{overall_risk}*\n"
            f"_{risk_explanation}_\n"
            "📊 Verdict: Too costly and risky to ship in one shot.\n\n"
            "*Here's what you get slice by slice:*\n\n"
            + "\n\n".join(slice_lines)
            + "\n\n"
            f"*Smart move:* Start with {slice_12_files} — that covers the foundation and core logic for *${smart_12_cost:.6f}*. "
            f"Slice 3 adds test coverage for *${smart_3_cost:.6f}* — low risk, high confidence."
            + readme_note
            + "\n\n*Select slices to run:*\nReply with *go* to run all, *no go* to cancel, or combine slice numbers like:\n- *slice 1*\n- *slice 1 2*\n- *slice 1 2 3*\n- *slice 2 3*"
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
    files_changed = ", ".join(f"`{f}`" for f in state.affected_files) if state.affected_files else "unknown files"
    pr_link = state.pull_request_url if state.pull_request_url else "Ready to commit — no PR created in this run"
    say(
        text=(
            "✅ *Done — here's what was generated*\n\n"
            f"📋 Files changed: {files_changed}\n"
            f"💰 Total cost: *${state.projected_token_cost_usd:.6f}*\n"
            f"🔗 PR: {pr_link}\n\n"
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


@app.message(re.compile(r"^(go|no go|slice 1)$", re.IGNORECASE))
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

    def folder_category(filename: str) -> str:
        lower = filename.lower()
        if "readme" in lower:
            return "readme"
        if "models/" in lower or "schema" in lower or "model" in lower:
            return "models"
        if "validators/" in lower or "validator" in lower or "services/" in lower or "pipeline" in lower:
            return "core"
        if "tests/" in lower or lower.startswith("test_") or "/test_" in lower:
            return "tests"
        if "docs/" in lower or lower.endswith(".md"):
            return "docs"
        if "config" in lower:
            return "config"
        return "other"

    try:
        if text == "go":
            say(text="Proceeding despite the budget warning…", thread_ts=thread_ts)
            state.policy_clearance = True
            state = mock_nodes.generator(state)
            post_generated_code(say, thread_ts, state)
            pending_budget_checks.pop(key, None)
            return

        if text == "no go":
            say(text="Cancelled. Feel free to refine your request and try again.", thread_ts=thread_ts)
            pending_budget_checks.pop(key, None)
            return

        slice_numbers = [int(c) for c in text if c.isdigit()]
        if slice_numbers and all(1 <= n <= 3 for n in slice_numbers):
            slice_1 = [f for f in state.affected_files if folder_category(f) == "models"]
            slice_2 = [f for f in state.affected_files if folder_category(f) == "core"]
            slice_3 = [f for f in state.affected_files if folder_category(f) in {"tests", "docs", "config", "other"}]
            
            selected_files = []
            if 1 in slice_numbers:
                selected_files.extend(slice_1)
            if 2 in slice_numbers:
                selected_files.extend(slice_2)
            if 3 in slice_numbers:
                selected_files.extend(slice_3)
            
            if not selected_files:
                say(text="No files in those slices. Please try again.", thread_ts=thread_ts)
                return
            
            say(text=f"Running slices {' + '.join(map(str, sorted(slice_numbers)))}…", thread_ts=thread_ts)
            state.affected_files = selected_files
            state = mock_nodes.optimizer(state)
            state = mock_nodes.estimator(state)
            token_count = estimate_tokens(state.extracted_slice_context or state.user_request)
            
            if token_count > _BUDGET_THRESHOLD and not state.policy_clearance:
                state.policy_clearance = False
                pending_budget_checks[key] = state
                post_budget_check(say, thread_ts, token_count)
                return
            
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
