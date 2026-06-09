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
pending_slice_maps: Dict[str, List[List[str]]] = {}  # thread_ts -> vertical slice file lists


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


# ── Agent 3 — Risk Assessment ─────────────────────────────────────────────────

def _extract_change_subject(user_request: str) -> Optional[str]:
    invalid_tokens = {"a", "the", "it"}

    def is_valid_candidate(candidate: str) -> bool:
        normalized = candidate.strip().lower()
        return len(normalized) >= 5 and normalized not in invalid_tokens

    camel = [c for c in re.findall(r'\b[A-Z][a-z]+[A-Z][a-zA-Z]*\b', user_request) if is_valid_candidate(c)]
    if camel:
        return camel[0]

    snake = [s for s in re.findall(r'\b[a-z]+_[a-z_]+\b', user_request) if is_valid_candidate(s)]
    if snake:
        return snake[0]

    for verb in ("replace", "add", "migrate", "refactor", "update", "introduce"):
        m = re.search(rf'{verb}\s+(\w+)', user_request, re.I)
        if m:
            candidate = m.group(1)
            if is_valid_candidate(candidate):
                return candidate

    return None


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
    for chunk in ("\n" + (state.extracted_slice_context or "")).split("\n# FILE: "):
        if not chunk.strip():
            continue
        first_line, _, rest = chunk.partition("\n")
        snippets[first_line.strip()] = rest

    # ── Dependency graph (used for blast radius + vertical grouping) ──────────
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

    non_readme = [f for f in files if folder_category(f) != "readme"]
    readme_files = [f for f in files if folder_category(f) == "readme"]

    dep_graph = _dep_graph(non_readme)
    blast_radius = _blast_radius_score(non_readme, dep_graph)
    blast_label = "HIGH" if blast_radius >= 4 else "MEDIUM" if blast_radius >= 2 else "LOW"

    overall_risk, _, folder_counts, no_test_count, coupled = _compute_risk(files, snippets)
    subject = _extract_change_subject(state.user_request)
    subject_label = subject or "the change"

    # ── Risk explanation ──────────────────────────────────────────────────────
    layers_hit = [
        label for label, cat in (("schema layer", "models"), ("logic/validation layer", "core"))
        if folder_counts[cat] > 0
    ]
    layers_phrase = " and ".join(layers_hit) or "feature-critical files"

    if coupled and subject:
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

    blast_note = (
        f"Blast radius: *{blast_label}* — {blast_radius} file(s) are downstream dependencies of this change."
        if blast_radius > 0 else ""
    )

    risk_explanation = (
        f"This touches {len(files)} files across the {layers_phrase}. "
        + (f"{coverage_note} " if coverage_note else "")
        + (f"\n{blast_note}" if blast_note else "")
        + (f"\n{coupling_warning}" if coupling_warning else "")
    )

    # ── Simple change fast-path: no slicing needed ───────────────────────────
    if overall_risk == "LOW" and blast_radius == 0 and not coupled:
        pending_slice_maps[thread_ts] = [non_readme]
        trigger_reason = "shipping risk" if token_count <= _BUDGET_THRESHOLD else "token budget"
        say(
            text=(
                f"*Agent 3 — Risk Assessment* _(triggered by {trigger_reason})_\n\n"
                f"Est. review: *{_fmt_time(_review_minutes(len(non_readme), overall_risk, False))}* "
                f"| Tokens: *{token_count:,}* | Risk: *{overall_risk}*\n\n"
                f"{risk_explanation}\n\n"
                "_Shape Up: this change is atomic and self-contained — it ships in one go and can be "
                "verified in isolation. No slicing needed._\n\n"
                "*Verdict:* Safe to ship as-is.\n\n"
                "*Reply to select:*\n"
                "*go* — ship it | *no go* — cancel"
            ),
            thread_ts=thread_ts,
        )
        return

    # ── Knowledge note ────────────────────────────────────────────────────────
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

    # ── Value label from code (reads docstrings/comments Pilar added) ────────
    def _file_value_label(filename: str) -> str:
        snippet = snippets.get(filename, "")
        for line in snippet.splitlines()[:30]:
            s = line.strip()
            if (s.startswith('"""') or s.startswith("'''")) and len(s) > 6:
                desc = s.strip('"\'').strip()
                if len(desc) > 10:
                    return desc
            if s.startswith('#') and len(s) > 15 and not s.startswith('#!'):
                return s.lstrip('# ').strip()
        return os.path.splitext(os.path.basename(filename))[0].replace('_', ' ').replace('-', ' ')

    # ── Semantic slice title detection ────────────────────────────────────────
    def _detect_fields(request: str) -> List[str]:
        candidates = [
            "customer", "order", "payment", "product", "address", "profile",
            "transaction", "shipment", "review", "inventory", "rating",
        ]
        return [term for term in candidates if re.search(rf"\b{term}s?\b", request)]

    def _detect_stages(request: str) -> List[str]:
        stage_map = [
            ("extract", "read"), ("ingest", "read"), ("load", "export"),
            ("transform", "process"), ("process", "process"), ("validate", "validate"),
            ("export", "export"), ("score", "process"), ("enrich", "process"),
        ]
        stages: List[str] = []
        for term, label in stage_map:
            if term in request and label not in stages:
                stages.append(label)
        return stages

    def _detect_personas(request: str) -> List[str]:
        candidates = ["customer", "admin", "merchant", "manager", "analyst", "support", "operator"]
        return [term for term in candidates if re.search(rf"\b{term}s?\b", request)]

    def _slice_titles(request: str) -> List[str]:
        req = request.lower()
        if any(w in req for w in ["error handling", "edge case", "missing data", "missing values", "invalid", "exception", "gracefully", "default"]):
            return [
                f"Happy path: {subject_label} works for complete data",
                "Edge case: incomplete or missing data handled gracefully",
                "Sad path: invalid input fails safely before downstream processing",
            ]
        fields = _detect_fields(req)
        if len(fields) > 1:
            return [f"Data type: {field} handled end-to-end" for field in fields[:3]]
        stages = _detect_stages(req)
        if len(stages) > 1:
            return [f"Workflow step: {stage} stage delivers a complete handoff" for stage in stages[:3]]
        personas = _detect_personas(req)
        if len(personas) > 1:
            return [f"Persona: {persona.capitalize()} experience completes with feedback" for persona in personas[:3]]

        # Fallback: derive titles from what the changed files actually describe
        non_test_files = [f for f in non_readme if folder_category(f) not in ("tests", "readme")]
        value_labels = list(dict.fromkeys(_file_value_label(f) for f in non_test_files))
        if len(value_labels) >= 2:
            return [f"{label} complete and verifiable" for label in value_labels[:3]]
        if len(value_labels) == 1:
            return [
                f"{value_labels[0]} works for the happy path",
                f"{value_labels[0]} handles edge cases safely",
            ]
        return ["Core change ships atomically and can be verified end-to-end"]

    def _group_vertical_slices(
        titles: List[str], all_files: List[str], graph: Dict[str, List[str]]
    ) -> List[Tuple[str, List[str]]]:
        category_bins: Dict[str, List[str]] = {
            cat: [f for f in all_files if folder_category(f) == cat]
            for cat in ("models", "core", "tests", "other")
        }
        slices: List[Tuple[str, List[str]]] = []

        for title in titles:
            grouped: List[str] = []
            for cat in ("models", "core", "tests"):
                if category_bins[cat]:
                    grouped.append(category_bins[cat].pop(0))
            if not grouped and category_bins["other"]:
                grouped.append(category_bins["other"].pop(0))

            # Pull in required dependencies that haven't been assigned yet
            to_add: List[str] = []
            for f in grouped:
                for dep in graph.get(f, []):
                    if dep not in grouped and dep not in to_add:
                        for cat in category_bins:
                            if dep in category_bins[cat]:
                                category_bins[cat].remove(dep)
                                to_add.append(dep)
            grouped.extend(to_add)
            slices.append((title, grouped))

        leftovers = [f for cat_files in category_bins.values() for f in cat_files]
        while leftovers:
            chunk: List[str] = []
            while leftovers and len(chunk) < 3:
                chunk.append(leftovers.pop(0))
            slices.append(("Supporting slice: additional related outcome", chunk))

        return [s for s in slices if s[1]]

    def _estimate_lines(fs: List[str]) -> int:
        count = 0
        for f in fs:
            snippet = snippets.get(f, "")
            count += snippet.count("\n") + 1 if snippet else 10
        return min(max(count, 5), 150)

    def _slice_outcome(title: str) -> str:
        if title.startswith("Happy path"):
            return f"A complete {subject_label} flow works for full records — verifiable with one happy-path scenario."
        if title.startswith("Edge case"):
            return "Incomplete or missing values are handled without crashing and the outcome is observable."
        if title.startswith("Sad path"):
            return "Invalid inputs are rejected before reaching downstream processing."
        if title.startswith("Data type"):
            return "One key data category is processed end-to-end and can be validated by focused tests."
        if title.startswith("Workflow step"):
            return "This stage ships a complete handoff — testable in isolation against upstream and downstream expectations."
        if title.startswith("Persona"):
            return "One user persona sees a completed experience and can validate through their own workflow."
        return "Delivers a specific business-facing outcome that can be validated with a focused test."

    def _slice_invest(fs: List[str], graph: Dict[str, List[str]], other_files: set) -> str:
        depends_on_other = any(dep in other_files for f in fs for dep in graph.get(f, []))
        if not depends_on_other and len(fs) <= 3 and _estimate_lines(fs) <= 150:
            return "Independent | Valuable | Testable in isolation"
        return "Depends on Slice 1 shipping first — not fully independent"

    def _slice_review(fs: List[str]) -> int:
        return _review_minutes(len(fs), overall_risk, has_coupling=any(f in coupled for f in fs))

    slice_titles = _slice_titles(state.user_request)
    slices = _group_vertical_slices(slice_titles, non_readme, dep_graph)

    # Store for handle_budget_reply so slice N maps to the correct files
    pending_slice_maps[thread_ts] = [files_in_slice for _, files_in_slice in slices]

    total_review = _fmt_time(_review_minutes(len(non_readme), overall_risk, bool(coupled)))
    readme_note = f"\n\nUpdate README.md to reflect the `{subject_label}` change once slices land." if readme_files else ""
    trigger_reason = "shipping risk" if token_count <= _BUDGET_THRESHOLD else "token budget"

    slice_lines: List[str] = []
    for idx, (title, files_in_slice) in enumerate(slices, start=1):
        files_str = ", ".join(f"`{os.path.basename(f)}`" for f in files_in_slice)
        other_files = {f for j, (_, sf) in enumerate(slices) if j != idx - 1 for f in sf}
        invest_text = _slice_invest(files_in_slice, dep_graph, other_files)
        change_size = _estimate_lines(files_in_slice)
        review_t = _fmt_time(_slice_review(files_in_slice))
        slice_lines.append(
            f"*Slice {idx} — {title}*\n"
            f"Files: {files_str}\n"
            f"What ships: {_slice_outcome(title)}\n"
            f"{invest_text} | Est. review: {review_t} | ~{change_size} lines changed"
        )

    say(
        text=(
            f"*Agent 3 — Risk Assessment* _(triggered by {trigger_reason})_\n\n"
            f"Est. review: *{total_review}* | Tokens: *{token_count:,}* | Risk: *{overall_risk}* | Blast radius: *{blast_label}*\n\n"
            f"{risk_explanation}\n\n"
            f"{knowledge_note}\n\n"
            "*Verdict:* Too risky to ship as a single PR.\n\nThese are the recommended slices:\n\n"
            + "\n\n".join(slice_lines)
            + "\n\n*Smart move:* Ship Slice 1 first — get feedback before deciding if the rest is still needed."
            + readme_note
            + "\n\n*Reply to select:*\n"
            "*go* — ship everything | *no go* — cancel | "
            "*slice 1*, *slice 2*, *slice 1 2*, *slice 1 2 3* — pick specific slices"
        ),
        thread_ts=thread_ts,
    )


# ── Main pipeline ─────────────────────────────────────────────────────────────

def _run_generator_and_pr(state: HackathonAppState) -> HackathonAppState:
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

    # Structural estimate: actual context + request + system overhead + output
    token_count = _structural_token_estimate(
        state.extracted_slice_context or "", state.user_request
    )
    post_cost_estimate(say, thread_ts, state, token_count)

    snippets: Dict[str, str] = {}
    for chunk in ("\n" + (state.extracted_slice_context or "")).split("\n# FILE: "):
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

    say(text="Processing your request…", thread_ts=thread_ts)
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
            pending_slice_maps.pop(thread_ts, None)
            return

        # ── no go: cancel ─────────────────────────────────────────────────────
        if text == "no go":
            say(text="Cancelled. Refine your request and try again.", thread_ts=thread_ts)
            pending_budget_checks.pop(key, None)
            pending_slice_maps.pop(thread_ts, None)
            return

        # ── slice N [N ...]: use the vertical slices computed in Agent 3 ──────
        slice_numbers = sorted({int(c) for c in text if c.isdigit() and 1 <= int(c) <= 9})
        if slice_numbers:
            slice_map = pending_slice_maps.get(thread_ts, [])
            selected: List[str] = []
            for n in slice_numbers:
                if 1 <= n <= len(slice_map):
                    selected.extend(slice_map[n - 1])

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
            pending_slice_maps.pop(thread_ts, None)
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
        token_count = _structural_token_estimate(
            state.extracted_slice_context or "", state.user_request
        )
        post_cost_estimate(say, thread_ts, state, token_count)

        snippets: Dict[str, str] = {}
        for chunk in ("\n" + (state.extracted_slice_context or "")).split("\n# FILE: "):
            if chunk.strip():
                first_line, _, rest = chunk.partition("\n")
                snippets[first_line.strip()] = rest

        if token_count > _BUDGET_THRESHOLD or not state.policy_clearance:
            state.policy_clearance = False
            pending_budget_checks[key] = state
            post_budget_check(say, thread_ts, token_count)
            return

        state = _run_generator_and_pr(state)
        post_generated_code(say, thread_ts, state)
        pending_budget_checks.pop(key, None)
        pending_slice_maps.pop(thread_ts, None)

    except Exception as exc:
        logger.exception("Budget reply failed")
        say(text=f"Something went wrong.\nError: {exc}", thread_ts=thread_ts)


if __name__ == "__main__":
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
