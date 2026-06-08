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

_MINS_PER_FILE = {"HIGH": 30, "MEDIUM": 20, "LOW": 12}
_COUPLING_OVERHEAD_MINS = 30


def _review_minutes(file_count: int, risk: str, has_coupling: bool = False) -> int:
    return file_count * _MINS_PER_FILE.get(risk, 20) + (_COUPLING_OVERHEAD_MINS if has_coupling else 0)


def _fmt_time(minutes: int) -> str:
    if minutes < 60:
        return f"~{minutes} min"
    return f"~{minutes / 60:.1f} hr"

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
            "*Agent 1 — Thin Slicer*\n"
            f"Files affected:\n{format_file_list(state.affected_files)}\n"
            f"Estimated tokens: *{tokens:,}*"
        ),
        thread_ts=thread_ts,
    )


def post_cost_estimate(say, thread_ts: str, state: HackathonAppState, token_count: int) -> None:
    complexity = token_count / _BUDGET_THRESHOLD
    file_count = len(state.affected_files or [])
    say(
        text=(
            "*Agent 2 — Model Optimizer*\n"
            f"Files: *{file_count}* | Complexity: *{complexity:.1f}x* safe-ship threshold | Tokens: *{token_count:,}*\n"
            f"Recommended model: {recommend_model(token_count)}"
        ),
        thread_ts=thread_ts,
    )


def post_generated_code(say, thread_ts: str, state: HackathonAppState) -> None:
    if not state.generated_code_blocks:
        say(text="*Agent 4 — Code Generator*\nNo code blocks were produced.", thread_ts=thread_ts)
        return

    pr_url = state.pull_request_url
    pr_line = f"*PR:* {pr_url}" if pr_url else "*PR:* Not created in this run"
    files_line = ", ".join(f"`{f}`" for f in state.generated_code_blocks)

    say(
        text=(
            "*Agent 4 — Code Generator complete*\n\n"
            f"Files changed: {files_line}\n"
            f"{pr_line}\n\n"
            + format_code_blocks(state.generated_code_blocks)
        ),
        thread_ts=thread_ts,
    )


# ── Agent 3 — Cost × Risk Assessment ─────────────────────────────────────────

def _extract_change_subject(user_request: str) -> str:
    """Pull the key entity being changed out of the request string."""
    # CamelCase identifier (e.g. RiskCategory)
    camel = re.findall(r'\b[A-Z][a-z]+[A-Z][a-zA-Z]*\b', user_request)
    if camel:
        return camel[0]
    # snake_case field (e.g. risk_level)
    snake = re.findall(r'\b[a-z]+_[a-z_]+\b', user_request)
    if snake:
        return snake[0]
    # Fallback: first substantial noun after a verb
    for verb in ("replace", "add", "migrate", "refactor", "update", "introduce"):
        m = re.search(rf'{verb}\s+(\w+)', user_request, re.I)
        if m:
            return m.group(1)
    return "the change"


def _compute_risk(files: List[str], snippets: Dict[str, str]) -> tuple:
    """Return (overall_risk, risk_score, folder_counts, no_test_count, coupled_files)."""
    folder_counts = {
        cat: sum(1 for f in files if folder_category(f) == cat)
        for cat in ("models", "core", "tests", "docs", "config", "readme", "other")
    }
    no_test_count = sum(
        1 for f in files
        if "test" not in f.lower()
        and not re.search(r"\b(def test_|pytest|unittest|class Test)", snippets.get(f, ""), re.I)
    )
    # Coupling: model + core files that reference the same field are coupled
    coupled = (
        [f for f in files if folder_category(f) in ("models", "core")]
        if folder_counts["models"] > 0 and folder_counts["core"] > 0
        else []
    )
    risk_score = (
        (2 if folder_counts["models"] > 0 else 0)
        + (2 if folder_counts["core"] > 0 else 0)
        + min(no_test_count, 2)
    )
    overall_risk = "HIGH" if risk_score >= 3 else "MEDIUM" if risk_score == 2 else "LOW"
    return overall_risk, risk_score, folder_counts, no_test_count, coupled


def _shipping_risk_triggered(files: List[str], snippets: Dict[str, str]) -> bool:
    """Return True when shipping risk alone justifies the go/no-go gate."""
    overall_risk, _, _, _, _ = _compute_risk(files, snippets)
    return overall_risk == "HIGH"


def post_budget_check(say, thread_ts: str, token_count: int) -> None:
    state: Optional[HackathonAppState] = next(
        (s for (_, ts), s in pending_budget_checks.items() if ts == thread_ts),
        None,
    )

    if state is None:
        say(
            text=(
                "*Agent 3 — Risk Assessment*\n"
                f"Token estimate *{token_count:,}* exceeds the *{_BUDGET_THRESHOLD:,}* threshold.\n\n"
                "Reply *go* to proceed, or *no go* to cancel."
            ),
            thread_ts=thread_ts,
        )
        return

    files = state.affected_files or []
    # ── Parse snippets ────────────────────────────────────────────────────────
    snippets: Dict[str, str] = {}
    for chunk in (state.extracted_slice_context or "").split("\n# FILE: "):
        if not chunk.strip():
            continue
        first_line, _, rest = chunk.partition("\n")
        snippets[first_line.strip()] = rest

    overall_risk, _, folder_counts, no_test_count, coupled = _compute_risk(files, snippets)
    subject = _extract_change_subject(state.user_request)

    # ── Risk explanation — specific, not templated ────────────────────────────
    layers_hit = [
        label for label, cat in (("schema layer", "models"), ("logic/validation layer", "core"))
        if folder_counts[cat] > 0
    ]
    layers_phrase = " and ".join(layers_hit) or "feature-critical files"

    if coupled:
        coupling_warning = (
            f"*Coupling detected:* `{os.path.basename(coupled[0])}` defines `{subject}` "
            f"and {len(coupled) - 1} file(s) consume it — they cannot deploy independently. "
            f"Strangler Fig: a slice that only works when another unfinished slice is deployed "
            f"isn't a slice — it's a dependency chain."
        )
    else:
        coupling_warning = ""

    coverage_note = (
        f"{no_test_count} of {len(files)} files have no test coverage — "
        "changes here are harder to verify and roll back safely."
        if no_test_count > 0 else ""
    )

    risk_explanation = (
        f"This touches {len(files)} files across the {layers_phrase}. "
        + (f"{coverage_note} " if coverage_note else "")
        + (f"\n{coupling_warning}" if coupling_warning else "")
    )

    # ── Knowledge applied to THIS change ─────────────────────────────────────
    if overall_risk == "HIGH" and coupled:
        knowledge_note = (
            f"_DORA says batch size is the strongest predictor of stability — "
            f"shipping {len(files)} coupled files at once is a large batch._\n"
            f"_Shape Up: can each slice ship in 1–2 days and stay green on its own? "
            f"If `{os.path.basename(coupled[0])}` lands without its consumers updated, "
            f"the answer is no._"
        )
    elif overall_risk == "HIGH":
        knowledge_note = (
            f"_DORA: deploy smaller batches more frequently — "
            f"{len(files)} files across {layers_phrase} is a large batch for one shot._"
        )
    else:
        knowledge_note = (
            f"_Strangler Fig: each slice must work independently and be reversible — "
            f"verify that before shipping._"
        )

    # ── Build slices ──────────────────────────────────────────────────────────
    def _clean_label(filename: str) -> str:
        return os.path.splitext(os.path.basename(filename))[0].replace("_", " ").replace("-", " ")

    def _slice_description(filename: str) -> str:
        cat = folder_category(filename)
        label = _clean_label(filename)
        is_coupled = filename in coupled
        base = os.path.basename(filename)

        if cat == "models":
            return (
                f"Introduce `{subject}` in `{base}` — the root contract. "
                f"This is what {len([f for f in files if folder_category(f) == 'core'])} "
                f"downstream file(s) will import from instead of maintaining their own copy."
            )
        if cat == "core":
            if "validator" in filename.lower():
                coupling_flag = " *Must ship with Slice 1* — validation breaks without the model." if is_coupled else ""
                return (
                    f"`{base}` currently checks `{subject}` as a raw string. "
                    f"Replace the inline set literal with a type-level check against `{subject}`.{coupling_flag}"
                )
            return (
                f"`{base}` references `{subject}` — update it to use the new contract "
                f"from the model layer instead of its own copy."
                + (" *Coupled to Slice 1.*" if is_coupled else "")
            )
        if cat == "tests":
            return (
                f"Update `{base}` to exercise `{subject}` — confirms the enum "
                f"validates correctly and the classifier returns the right category."
            )
        if cat == "readme":
            return f"Document the `{subject}` change in README — update the domain model table."
        return f"Apply supporting changes to `{label}`."

    def _worth_it(filename: str) -> str:
        cat = folder_category(filename)
        is_coupled = filename in coupled
        if cat == "models":
            return f"Ships the root contract — without this, nothing else can move."
        if cat == "core":
            if "validator" in filename.lower():
                return (
                    "Enforcement boundary — once this lands, bad strings can't reach the pipeline."
                    + (" DORA: ship with Slice 1 (coupled)." if is_coupled else "")
                )
            return "Core logic update — makes the change visible at the processing stage."
        if cat == "tests":
            return "Shape Up: tests are the 1-day follow-on — ship in the next cycle if time is tight."
        return "Low-risk supporting change."

    non_readme = [f for f in files if folder_category(f) != "readme"]
    readme_files = [f for f in files if folder_category(f) == "readme"]

    # ── Dependency graph → connected components ───────────────────────────────
    def _dep_graph(fs: List[str]) -> Dict[str, List[str]]:
        stem_to_path = {os.path.splitext(os.path.basename(f))[0]: f for f in fs}
        graph: Dict[str, List[str]] = {f: [] for f in fs}
        for f in fs:
            for line in snippets.get(f, "").splitlines():
                m = re.search(r'(?:from|import)\s+([\w.]+)', line)
                if not m:
                    continue
                for part in m.group(1).split('.'):
                    if part in stem_to_path and stem_to_path[part] != f:
                        nb = stem_to_path[part]
                        if nb not in graph[f]:
                            graph[f].append(nb)
        return graph

    def _find_components(fs: List[str], graph: Dict[str, List[str]]) -> List[List[str]]:
        parent = {f: f for f in fs}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for f, deps in graph.items():
            for dep in deps:
                px, py = find(f), find(dep)
                if px != py:
                    parent[px] = py

        groups: Dict[str, List[str]] = {}
        for f in fs:
            groups.setdefault(find(f), []).append(f)
        return list(groups.values())

    def _component_priority(comp: List[str]) -> int:
        order = {"models": 0, "core": 1, "tests": 2, "docs": 2, "config": 3, "other": 3}
        return min(order.get(folder_category(f), 3) for f in comp)

    def _component_title(comp: List[str]) -> str:
        cats = {folder_category(f) for f in comp}
        if "models" in cats:
            return "Define the contract"
        if "core" in cats:
            return "Enforce it"
        return "Prove it works"

    dep_graph = _dep_graph(non_readme)
    components = sorted(_find_components(non_readme, dep_graph), key=_component_priority)
    slices = [(comp, i + 1) for i, comp in enumerate(components)]

    def _slice_review(fs: List[str]) -> int:
        return _review_minutes(len(fs), overall_risk, has_coupling=any(f in coupled for f in fs))

    slice_lines = []
    for files_in_slice, idx in slices:
        review_t = _fmt_time(_slice_review(files_in_slice))
        files_str = ", ".join(f"`{os.path.basename(f)}`" for f in files_in_slice)
        bullets = "\n".join(f"   • {_slice_description(f)}" for f in files_in_slice)
        worth = _worth_it(files_in_slice[0])
        slice_lines.append(
            f"*Slice {idx} — {_component_title(files_in_slice)}* | Est. review: {review_t} | Files: {files_str}\n"
            f"{bullets}\n"
            f"   _{worth}_"
        )

    # ── Smart move ────────────────────────────────────────────────────────────
    s_mins = [_slice_review(comp) for comp, _ in slices]
    first_slice = slices[0][0] if slices else []

    if len(first_slice) > 1:
        files_label = " + ".join(f"`{os.path.basename(f)}`" for f in first_slice)
        smart_move = (
            f"*Ship Slice 1 as a unit* ({files_label}) — "
            f"these files import each other and cannot deploy independently. "
            f"Est. review: {_fmt_time(s_mins[0])}."
            + (f"\nSlices 2+ are independent — ship in order and verify in prod before the next." if len(slices) > 1 else "")
        )
    elif first_slice:
        smart_move = (
            f"*Start with Slice 1* (`{os.path.basename(first_slice[0])}`) — "
            f"{_fmt_time(s_mins[0])} of review (DORA: smallest safe batch). "
            f"Each slice is independent — ship in order and verify before the next."
        )
    else:
        smart_move = "No slices to ship."

    total_review = _fmt_time(_review_minutes(len(non_readme), overall_risk, bool(coupled)))
    readme_note = f"\n\nUpdate README.md to reflect the `{subject}` change once slices land." if readme_files else ""

    trigger_reason = "shipping risk" if token_count <= _BUDGET_THRESHOLD else "token budget"

    say(
        text=(
            f"*Agent 3 — Risk Assessment* _(triggered by {trigger_reason})_\n\n"
            f"Est. review: *{total_review}* | Tokens: *{token_count:,}* | Risk: *{overall_risk}*\n\n"
            f"{risk_explanation}\n\n"
            f"{knowledge_note}\n\n"
            "*Verdict:* Too risky to ship as a single PR.\n\nThese are the recommended slices:\n\n"
            + "\n\n".join(slice_lines)
            + f"\n\n*Smart move:* {smart_move}"
            + readme_note
            + "\n\n*Reply to select:*\n"
            "*go* — ship everything | *no go* — cancel | "
            "*slice 1*, *slice 2*, *slice 1 2*, *slice 1 2 3* — pick specific slices"
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

    # Trigger go/no-go on financial threshold OR shipping risk (HIGH = coupled layers)
    snippets: Dict[str, str] = {}
    for chunk in (state.extracted_slice_context or "").split("\n# FILE: "):
        if chunk.strip():
            first_line, _, rest = chunk.partition("\n")
            snippets[first_line.strip()] = rest

    cost_triggered = token_count > _BUDGET_THRESHOLD or not state.policy_clearance
    risk_triggered = _shipping_risk_triggered(state.affected_files, snippets)

    if cost_triggered or risk_triggered:
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
