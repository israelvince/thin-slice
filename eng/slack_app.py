import logging
import os
import re
import subprocess
import uuid
from typing import Dict, List, Optional, Tuple

from ai import nodes as pipeline
from ai.github_pr import create_pr_stub
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

_BUDGET_THRESHOLD = int(os.environ.get("THIN_SLICE_TOKEN_THRESHOLD", "1500"))

if not SLACK_BOT_TOKEN or not SLACK_APP_TOKEN:
    raise RuntimeError(
        "SLACK_BOT_TOKEN and SLACK_APP_TOKEN must be set before starting the Slack app."
    )

app = App(token=SLACK_BOT_TOKEN)
pending_budget_checks: Dict[Tuple[str, str], HackathonAppState] = {}

# ── Knowledge context (loaded once at startup) ────────────────────────────────

def _load_knowledge() -> Dict[str, str]:
    base = os.path.join(os.path.dirname(__file__), "knowledge")
    files = {
        "dora": "dora_metrics.txt",
        "shape_up": "shape_up.txt",
        "strangler": "strangler_fig.txt",
    }
    out = {}
    for key, fname in files.items():
        try:
            with open(os.path.join(base, fname), encoding="utf-8") as fh:
                out[key] = fh.read().strip()
        except Exception:
            out[key] = ""
    return out

_KNOWLEDGE = _load_knowledge()

# ── Shared helpers ────────────────────────────────────────────────────────────

def folder_category(filename: str) -> str:
    lower = filename.lower()
    if "readme" in lower:
        return "readme"
    if "models/" in lower or "schema" in lower or "/model" in lower:
        return "models"
    if (
        "validators/" in lower or "validator" in lower
        or "services/" in lower or "pipeline" in lower
    ):
        return "core"
    if "tests/" in lower or lower.startswith("test_") or "/test_" in lower:
        return "tests"
    if "docs/" in lower or lower.endswith(".md"):
        return "docs"
    if "config" in lower:
        return "config"
    return "other"


def resolve_repo_path(target_repo: str) -> str:
    if os.path.isabs(target_repo):
        return target_repo
    return os.path.abspath(os.path.join(os.path.dirname(__file__), target_repo))


def extract_user_message(text: str) -> str:
    parts = (text or "").split()
    return " ".join(p for p in parts if not (p.startswith("<@") and p.endswith(">"))).strip()


def recommend_model(token_count: int) -> str:
    if token_count < 500:
        return "claude-haiku-4-5 (fastest, cheapest)"
    if token_count <= 4000:
        return "claude-sonnet-4-6 (balanced)"
    return "claude-sonnet-4-6 (large context)"


def format_file_list(files: List[str]) -> str:
    return "\n".join(f"• `{f}`" for f in files) if files else "None"


def format_code_blocks(generated: dict) -> str:
    blocks = []
    for name, content in generated.items():
        code = str(content).strip()
        if len(code) > 2500:
            code = code[:2500] + "\n...truncated..."
        blocks.append(f"*`{name}`*\n```\n{code}\n```")
    return "\n\n".join(blocks)


# ── PR creation ───────────────────────────────────────────────────────────────

def _make_pr(state: HackathonAppState) -> Optional[str]:
    """
    Create a PR for the generated changes.
    Finds the git repo root, prefixes change paths correctly,
    then delegates to create_pr_stub (gh CLI → local stub).
    """
    if not state.generated_code_blocks:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", state.target_repo, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        repo_root = result.stdout.strip() if result.returncode == 0 else state.target_repo
        rel_prefix = os.path.relpath(
            os.path.abspath(state.target_repo),
            os.path.abspath(repo_root),
        )
        prefixed = (
            state.generated_code_blocks
            if rel_prefix == "."
            else {os.path.join(rel_prefix, k): v for k, v in state.generated_code_blocks.items()}
        )
        branch = "thin-slice/" + re.sub(r"[^a-z0-9-]", "-", state.user_request[:40].lower()).strip("-")
        return create_pr_stub(repo_root, branch, prefixed)
    except Exception as exc:
        logger.warning("PR creation failed: %s", exc)
        return f"local://thin-slice/pull/{uuid.uuid4().hex[:8]}"


# ── Slack post helpers ────────────────────────────────────────────────────────

def post_slices_identified(say, thread_ts: str, state: HackathonAppState) -> None:
    tokens = estimate_tokens(state.extracted_slice_context or state.user_request)
    say(
        text=(
            "🗂 *Agent 1 — Thin Slicer*\n"
            f"Files affected:\n{format_file_list(state.affected_files)}\n"
            f"Estimated tokens: *{tokens:,}*"
        ),
        thread_ts=thread_ts,
    )


def post_cost_estimate(say, thread_ts: str, state: HackathonAppState, token_count: int) -> None:
    say(
        text=(
            "💰 *Agent 2 — Model Optimizer*\n"
            f"Estimated cost: *${state.projected_token_cost_usd:.6f}*\n"
            f"Recommended model: {recommend_model(token_count)}"
        ),
        thread_ts=thread_ts,
    )


def post_generated_code(say, thread_ts: str, state: HackathonAppState) -> None:
    if not state.generated_code_blocks:
        say(text="✅ *Agent 4 — Code Generator*\nNo code blocks were produced.", thread_ts=thread_ts)
        return

    pr_url = state.pull_request_url
    pr_line = f"🔗 *PR:* {pr_url}" if pr_url else "🔗 *PR:* Not created in this run"
    files_line = ", ".join(f"`{f}`" for f in state.generated_code_blocks)

    say(
        text=(
            "✅ *Agent 4 — Code Generator complete*\n\n"
            f"📋 Files changed: {files_line}\n"
            f"💰 Cost: *${state.projected_token_cost_usd:.6f}*\n"
            f"{pr_line}\n\n"
            + format_code_blocks(state.generated_code_blocks)
        ),
        thread_ts=thread_ts,
    )


# ── Agent 3 — Cost × Risk Assessment ─────────────────────────────────────────

def post_budget_check(say, thread_ts: str, token_count: int) -> None:
    state: Optional[HackathonAppState] = next(
        (s for (_, ts), s in pending_budget_checks.items() if ts == thread_ts),
        None,
    )

    if state is None:
        say(
            text=(
                "⚠️ *Agent 3 — Cost × Risk Assessment*\n"
                f"Token estimate *{token_count:,}* exceeds the *{_BUDGET_THRESHOLD:,}* threshold.\n\n"
                "Reply *go* to proceed, or *no go* to cancel."
            ),
            thread_ts=thread_ts,
        )
        return

    files = state.affected_files or []
    total_cost = state.projected_token_cost_usd
    user_intent = state.user_request[:60] if len(state.user_request) > 60 else state.user_request

    # ── Parse snippets ────────────────────────────────────────────────────────
    snippets: Dict[str, str] = {}
    for chunk in (state.extracted_slice_context or "").split("\n# FILE: "):
        if not chunk.strip():
            continue
        first_line, _, rest = chunk.partition("\n")
        snippets[first_line.strip()] = rest

    # ── Risk scoring ──────────────────────────────────────────────────────────
    folder_counts = {cat: sum(1 for f in files if folder_category(f) == cat)
                     for cat in ("models", "core", "tests", "docs", "config", "readme", "other")}

    no_test_count = sum(
        1 for f in files
        if "test" not in f.lower()
        and not re.search(r"\b(def test_|pytest|unittest|class Test)", snippets.get(f, ""), re.I)
    )

    risk_score = (
        (2 if folder_counts["models"] > 0 else 0)
        + (2 if folder_counts["core"] > 0 else 0)
        + min(no_test_count, 2)
    )
    overall_risk = "HIGH" if risk_score >= 3 else "MEDIUM" if risk_score == 2 else "LOW"

    high_folders = ", ".join(
        label for label, cat in (("models", "models"), ("validators/pipelines", "core"))
        if folder_counts[cat] > 0
    ) or "feature-critical areas"

    risk_explanation = (
        f"{len(files)} file{'s' if len(files) != 1 else ''} touched across {high_folders}. "
        f"{no_test_count} of {len(files)} have no test coverage."
    )

    # ── Knowledge-backed advice ───────────────────────────────────────────────
    if overall_risk == "HIGH":
        knowledge_note = (
            f"_DORA: {_KNOWLEDGE['dora'].split('.')[0]}._\n"
            f"_Shape Up: {_KNOWLEDGE['shape_up'].split('.')[0]}._"
        ) if _KNOWLEDGE.get("dora") else ""
    else:
        knowledge_note = (
            f"_Strangler Fig: {_KNOWLEDGE['strangler'].split('.')[0]}._"
        ) if _KNOWLEDGE.get("strangler") else ""

    # ── Build vertical slices ─────────────────────────────────────────────────
    def _clean_label(filename: str) -> str:
        base = os.path.basename(filename)
        return os.path.splitext(base)[0].replace("_", " ").replace("-", " ")

    def _slice_description(filename: str) -> str:
        cat = folder_category(filename)
        label = _clean_label(filename)
        if cat == "models":
            return f"Update the `{label}` schema — everything downstream depends on this."
        if cat == "core":
            if "validator" in filename.lower():
                return "Add validation rules that enforce clean inputs at the boundary."
            return f"Apply the change to `{label}` at the processing stage."
        if cat == "tests":
            return "Extend the test suite to cover the new paths and edge cases."
        if cat == "readme":
            return "Update README to reflect the change."
        return f"Apply supporting changes to `{label}`."

    def _worth_it(filename: str) -> str:
        cat = folder_category(filename)
        if cat == "models":       return "Foundation — everything else depends on this."
        if cat == "core":
            if "validator" in filename.lower():
                return "Enforcement point — catches bad data before it propagates."
            return "Core logic — delivers the visible change."
        if cat == "tests":        return "Safety net — confirms nothing broke."
        return "Supports the feature."

    non_readme = [f for f in files if folder_category(f) != "readme"]
    readme_files = [f for f in files if folder_category(f) == "readme"]

    slice_1 = [f for f in non_readme if folder_category(f) == "models"]
    slice_2 = [f for f in non_readme if folder_category(f) == "core"]
    slice_3 = [f for f in non_readme if folder_category(f) in ("tests", "docs", "config", "other")]

    # Ensure no empty slices if files don't fit the model
    if not slice_1 and slice_2:
        slice_1.append(slice_2.pop(0))
    if not slice_2 and slice_3:
        slice_2.append(slice_3.pop(0))
    if not slice_2 and slice_1:
        slice_2 = slice_1[1:]
        slice_1 = slice_1[:1]

    slices = [(s, i + 1) for i, s in enumerate([slice_1, slice_2, slice_3]) if s]
    total_non_readme = max(len(non_readme), 1)
    cost_per_file = total_cost / total_non_readme

    def _slice_cost(files_in_slice: List[str]) -> float:
        return round(cost_per_file * len(files_in_slice), 6)

    emoji = {1: "🟢", 2: "🟡", 3: "🔵"}
    slice_lines = []
    for files_in_slice, idx in slices:
        sc = _slice_cost(files_in_slice)
        files_str = ", ".join(f"`{f}`" for f in files_in_slice)
        bullets = "\n".join(
            f"   • {_slice_description(f)}" for f in files_in_slice
        )
        worth = _worth_it(files_in_slice[0])
        title = {1: "Define the foundation", 2: "Build the core feature", 3: "Confirm it works"}[idx]
        slice_lines.append(
            f"{emoji[idx]} *Slice {idx} — {title}* | Cost: ${sc:.6f} | Files: {files_str}\n"
            f"{bullets}\n"
            f"   _{worth}_"
        )

    # ── Smart move summary ────────────────────────────────────────────────────
    s1_cost = _slice_cost(slice_1)
    s2_cost = _slice_cost(slice_2)
    s3_cost = _slice_cost(slice_3) if slice_3 else 0.0
    smart_cost = round(s1_cost + s2_cost, 6)
    smart_files = " + ".join(
        f"`{f}`" for f in (slice_1[:1] + slice_2[:1])
    ) or "slices 1+2"

    readme_note = "\n\n📝 Don't forget to update README.md" if readme_files else ""

    say(
        text=(
            "⚠️ *Agent 3 — Cost × Risk Assessment*\n\n"
            f"💰 Total cost: *${total_cost:.6f}* | 🔢 Tokens: *{token_count:,}* | Budget: *{_BUDGET_THRESHOLD:,}*\n"
            f"⚠️ Risk level: *{overall_risk}*\n"
            f"_{risk_explanation}_\n"
            + (f"\n{knowledge_note}\n" if knowledge_note else "")
            + "\n📊 *Verdict:* Too broad to ship safely in one shot.\n\n"
            "*Here's what you get slice by slice:*\n\n"
            + "\n\n".join(slice_lines)
            + f"\n\n*Smart move:* Start with {smart_files} — foundation + core logic for *${smart_cost:.6f}*."
            + (f" Add Slice 3 for test coverage at *${s3_cost:.6f}* — low risk, high confidence." if slice_3 else "")
            + readme_note
            + "\n\n*Reply to select:*\n"
            "*go* — run everything | *no go* — cancel | *slice 1*, *slice 2*, *slice 1 2*, *slice 1 2 3* — pick slices"
        ),
        thread_ts=thread_ts,
    )


# ── Main pipeline ─────────────────────────────────────────────────────────────

def _run_generator_and_pr(state: HackathonAppState) -> HackathonAppState:
    """Run the generator node then create a PR. Returns updated state."""
    state = pipeline.generator(state)
    if state.policy_clearance and state.generated_code_blocks:
        state.pull_request_url = _make_pr(state)
    return state


def run_pipeline(say, channel: str, thread_ts: str, user_message: str) -> None:
    repo_path = resolve_repo_path(DEFAULT_TARGET_REPO)
    state = HackathonAppState(user_request=user_message, target_repo=repo_path)

    state = pipeline.planner(state)
    post_slices_identified(say, thread_ts, state)

    state = pipeline.optimizer(state)
    state = pipeline.estimator(state)
    token_count = estimate_tokens(state.extracted_slice_context or state.user_request)
    post_cost_estimate(say, thread_ts, state, token_count)

    if token_count > _BUDGET_THRESHOLD or not state.policy_clearance:
        state.policy_clearance = False
        pending_budget_checks[(channel, thread_ts)] = state
        post_budget_check(say, thread_ts, token_count)
        return

    state = _run_generator_and_pr(state)
    post_generated_code(say, thread_ts, state)


# ── Slack event handlers ──────────────────────────────────────────────────────

@app.event("app_mention")
def handle_app_mention(body, say, logger):
    event = body.get("event", {})
    channel = event.get("channel")
    thread_ts = event.get("thread_ts") or event.get("ts")
    user_message = extract_user_message(event.get("text", ""))

    if not user_message:
        say(text="Mention me with your change request and I'll get to work.", thread_ts=thread_ts)
        return

    say(text="Processing your request… 🔍", thread_ts=thread_ts)
    try:
        run_pipeline(say, channel, thread_ts, user_message)
    except Exception as exc:
        logger.exception("Pipeline failed")
        say(text=f"Something went wrong.\nError: {exc}", thread_ts=thread_ts)


@app.message(re.compile(r"^(go|no go|slice[\s\d]+)$", re.IGNORECASE))
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
        # ── go: run everything ────────────────────────────────────────────────
        if text == "go":
            say(text="Running full generation…", thread_ts=thread_ts)
            state.policy_clearance = True
            state = _run_generator_and_pr(state)
            post_generated_code(say, thread_ts, state)
            pending_budget_checks.pop(key, None)
            return

        # ── no go: cancel ─────────────────────────────────────────────────────
        if text == "no go":
            say(text="Cancelled. Refine your request and try again.", thread_ts=thread_ts)
            pending_budget_checks.pop(key, None)
            return

        # ── slice N [N ...]: selective execution ──────────────────────────────
        slice_numbers = sorted({int(c) for c in text if c.isdigit() and 1 <= int(c) <= 3})
        if slice_numbers:
            non_readme = [f for f in state.affected_files if folder_category(f) != "readme"]
            s1 = [f for f in non_readme if folder_category(f) == "models"]
            s2 = [f for f in non_readme if folder_category(f) == "core"]
            s3 = [f for f in non_readme if folder_category(f) in ("tests", "docs", "config", "other")]

            selected: List[str] = []
            if 1 in slice_numbers: selected.extend(s1)
            if 2 in slice_numbers: selected.extend(s2)
            if 3 in slice_numbers: selected.extend(s3)

            if not selected:
                say(text="No files in those slices. Try different slice numbers.", thread_ts=thread_ts)
                return

            label = " + ".join(f"Slice {n}" for n in slice_numbers)
            say(text=f"Running {label}…", thread_ts=thread_ts)

            state.affected_files = selected
            state.policy_clearance = True
            state = _run_generator_and_pr(state)
            post_generated_code(say, thread_ts, state)
            pending_budget_checks.pop(key, None)
            return

        # ── fallback: re-slice smaller ────────────────────────────────────────
        say(text="Re-slicing to a smaller scope…", thread_ts=thread_ts)
        keywords = [w for w in re.findall(r"\w+", state.user_request) if len(w) > 3][:3]
        if os.path.isdir(state.target_repo):
            res = slice_repo(state.target_repo, keywords)
            state.affected_files = res.get("affected_files", [])
            state.extracted_slice_context = res.get("extracted_slice_context", "")
        else:
            state.affected_files = state.affected_files[:2]
            state.extracted_slice_context = (state.extracted_slice_context or state.user_request)[:1500]

        post_slices_identified(say, thread_ts, state)
        state = pipeline.optimizer(state)
        state = pipeline.estimator(state)
        token_count = estimate_tokens(state.extracted_slice_context or state.user_request)
        post_cost_estimate(say, thread_ts, state, token_count)

        if token_count > _BUDGET_THRESHOLD or not state.policy_clearance:
            state.policy_clearance = False
            pending_budget_checks[key] = state
            post_budget_check(say, thread_ts, token_count)
            return

        state = _run_generator_and_pr(state)
        post_generated_code(say, thread_ts, state)
        pending_budget_checks.pop(key, None)

    except Exception as exc:
        logger.exception("Budget reply failed")
        say(text=f"Something went wrong.\nError: {exc}", thread_ts=thread_ts)


if __name__ == "__main__":
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
