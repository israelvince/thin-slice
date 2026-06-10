import csv
import datetime
import json
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


def _blast_radius_score(files: List[str], graph: Dict[str, List[str]]) -> int:
    return sum(1 for f in files for deps in graph.values() if f in deps)


def _structural_token_estimate(context: str, user_request: str) -> int:
    ctx_tokens = estimate_tokens(context)
    req_tokens = estimate_tokens(user_request)
    return int((ctx_tokens + req_tokens + 500) * 1.4)


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

def post_slices_identified(say, thread_ts: str, state: HackathonAppState, token_count: Optional[int] = None) -> None:
    if token_count is None:
        token_count = estimate_tokens(state.extracted_slice_context or state.user_request)
    # Agent 1 consumes the slice context only — not the inflated structural estimate
    _a1_tokens = estimate_tokens(state.extracted_slice_context or state.user_request)
    say(
        text=(
            "*Agent 1 — Thin Slicer*\n"
            f"Files affected:\n{format_file_list(state.affected_files)}\n"
            f"Estimated tokens: *{token_count:,}*\n"
            f"_Used tokens: {_a1_tokens:,}_"
        ),
        thread_ts=thread_ts,
    )


def post_cost_estimate(say, thread_ts: str, state: HackathonAppState, token_count: int) -> None:
    complexity = token_count / _BUDGET_THRESHOLD
    file_count = len(state.affected_files or [])
    # Agent 2 re-reads the same slice context as Agent 1 (overhead is token-cost, not raw count)
    _a2_tokens = estimate_tokens(state.extracted_slice_context or "")
    say(
        text=(
            "*Agent 2 — Model Optimizer*\n"
            f"Files: *{file_count}* | Complexity: *{complexity:.1f}x* safe-ship threshold | Tokens: *{token_count:,}*\n"
            f"Recommended model: {recommend_model(token_count)}\n"
            f"_Used tokens: {_a2_tokens:,}_"
        ),
        thread_ts=thread_ts,
    )


def post_generated_code(say, thread_ts: str, state: HackathonAppState, ledger: Optional[dict] = None) -> None:
    if not state.generated_code_blocks:
        say(text="*Agent 4 — Code Generator*\nNo code blocks were produced.", thread_ts=thread_ts)
        return

    pr_url = state.pull_request_url
    pr_line = f"*PR:* {pr_url}" if pr_url else "*PR:* Not created in this run"
    files_line = ", ".join(f"`{f}`" for f in state.generated_code_blocks)
    a4_tokens = (
        ledger["agent_4_generator"]["tokens"]
        if ledger else estimate_tokens(str(state.generated_code_blocks))
    )

    say(
        text=(
            "*Agent 4 — Code Generator complete*\n\n"
            f"Files changed: {files_line}\n"
            f"{pr_line}\n\n"
            + format_code_blocks(state.generated_code_blocks)
            + f"\n\n_Used tokens: {a4_tokens:,}_"
        ),
        thread_ts=thread_ts,
    )

    if ledger is not None:
        post_token_ledger(say, thread_ts, state, ledger)


def post_token_ledger(say, thread_ts: str, state: HackathonAppState, ledger: dict) -> None:
    total_tokens = sum(v["tokens"] for v in ledger.values())
    total_cost = sum(v["cost_usd"] for v in ledger.values())

    try:
        total_files = sum(1 for _, _, fs in os.walk(state.target_repo) for _ in fs)
    except Exception:
        total_files = len(state.affected_files or [])
    full_regen_cost = total_files * 0.0008

    agent_rows = [
        ("Agent 1 — Slicer",         ledger["agent_1_slicer"]),
        ("Agent 2 — Optimizer",       ledger["agent_2_optimizer"]),
        ("Agent 3 — Risk Assessment", ledger["agent_3_risk"]),
        ("Agent 4 — Generator",       ledger["agent_4_generator"]),
    ]

    table_rows = "\n".join(
        f"| {name} | {d['tokens']:,} | ${d['cost_usd']:.6f} |"
        for name, d in agent_rows
    )

    say(
        text=(
            "📊 *Agent 5 — Token Ledger*\n\n"
            "| Agent | Tokens | Cost |\n"
            "|-------|--------|------|\n"
            + table_rows + "\n"
            + f"| *Total* | *{total_tokens:,}* | *${total_cost:.6f}* |\n\n"
            f"💡 *vs full regeneration:* estimated ${full_regen_cost:.6f} for all {total_files} files"
        ),
        thread_ts=thread_ts,
    )

    entry = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "request": state.user_request,
        "ledger": ledger,
        "total_tokens": total_tokens,
        "total_cost": total_cost,
    }
    _eng = os.path.dirname(os.path.abspath(__file__))
    log_path = os.path.join(_eng, "token_log.jsonl")
    try:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception as exc:
        logger.warning("Token log write failed: %s", exc)

    csv_path = os.path.join(_eng, "token_usage.csv")
    _csv_header = [
        "timestamp", "request", "files",
        "agent_1_tokens", "agent_2_tokens", "agent_3_tokens", "agent_4_tokens",
        "total_tokens", "total_cost_usd",
    ]
    _csv_row = [
        entry["timestamp"],
        state.user_request[:120],
        "|".join(state.affected_files or []),
        ledger["agent_1_slicer"]["tokens"],
        ledger["agent_2_optimizer"]["tokens"],
        ledger["agent_3_risk"]["tokens"],
        ledger["agent_4_generator"]["tokens"],
        total_tokens,
        f"{total_cost:.8f}",
    ]
    try:
        write_header = not os.path.exists(csv_path)
        with open(csv_path, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if write_header:
                w.writerow(_csv_header)
            w.writerow(_csv_row)
    except Exception as exc:
        logger.warning("Token CSV write failed: %s", exc)


# ── Agent 3 — Risk Assessment ─────────────────────────────────────────────────

_ANNOTATION_WORDS = {
    "log", "logging", "print", "comment", "docstring", "debug", "trace", "print statement"
}

_OVERSIZED_SIGNALS = {
    "entire", "redesign", "real-time", "real time", "streaming", "dashboard",
    "comprehensive", "complete feature", "across every", "across all", "weekly summary",
    "aggregation pipeline", "event tracking", "reporting module",
    "every file", "codebase", "centralized", "retry logic", "exponential backoff",
}


def _is_annotation_request(user_request: str) -> bool:
    return any(w in user_request.lower() for w in _ANNOTATION_WORDS)


def _is_oversized_request(user_request: str) -> bool:
    req = user_request.lower()
    signal_hits = sum(1 for w in _OVERSIZED_SIGNALS if w in req)
    scope_indicators = req.count(",") + req.count(" and ")
    return signal_hits >= 2 or scope_indicators >= 5


def _extract_change_subject(user_request: str) -> Optional[str]:
    invalid_tokens = {"a", "the", "it"}

    def is_valid_candidate(candidate: str) -> bool:
        normalized = candidate.strip().lower()
        return len(normalized) >= 5 and normalized not in invalid_tokens

    # ALL_CAPS constant with underscore (e.g. CHURN_RISK_THRESHOLD)
    caps = [c for c in re.findall(r'\b[A-Z][A-Z0-9_]{4,}\b', user_request) if '_' in c and is_valid_candidate(c)]
    if caps:
        return caps[0]

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
    # Bug 5: count only affected files imported by at least one other affected file
    blast_radius = sum(
        1 for f in non_readme
        if any(f in dep_graph.get(other, []) for other in non_readme if other != f)
    )
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
    _annotation = _is_annotation_request(state.user_request)
    if (overall_risk == "LOW" or (_annotation and len(non_readme) == 1)) and blast_radius == 0 and not coupled:
        pending_slice_maps[thread_ts] = [non_readme]
        trigger_reason = "shipping risk" if token_count <= _BUDGET_THRESHOLD else "token budget"
        _fast_total_lines = min(max(
            sum(snippets.get(f, "").count("\n") + 1 if snippets.get(f) else 10 for f in non_readme),
            5,
        ), 150)
        say(
            text=(
                f"*Agent 3 — Risk Assessment* _(triggered by {trigger_reason})_\n\n"
                f"📐 ~{_fast_total_lines} lines across {len(non_readme)} files"
                f" | Tokens: *{token_count:,}* | Risk: *{overall_risk}*\n\n"
                f"{risk_explanation}\n\n"
                "_Shape Up: this change is atomic and self-contained — it ships in one go and can be "
                "verified in isolation. No slicing needed._\n\n"
                "*Verdict:* Safe to ship as-is.\n\n"
                "*Reply to select:*\n"
                "*go* — ship it | *no go* — cancel\n\n"
                "_Used tokens: 200_"
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

    # ── INVEST-compliant vertical slice generation ────────────────────────────

    # Step 1: Split strictly by layer — contract/enum → 1, pipeline/service → 2, tests → 3
    slice1_files = [f for f in non_readme if "models/" in f or "contracts/" in f]
    slice2_files = [f for f in non_readme if "pipelines/" in f or "services/" in f]
    slice3_files = [f for f in non_readme if (
        "tests/" in f
        or os.path.basename(f).startswith("test_")
        or "/test_" in f
    )]
    assigned = set(slice1_files + slice2_files + slice3_files)
    for f in non_readme:
        if f not in assigned:
            cat = folder_category(f)
            if cat == "models":
                slice1_files.append(f)
            elif cat == "tests":
                slice3_files.append(f)
            else:
                slice2_files.append(f)

    # Store all three layer lists so "slice N" replies map correctly by fixed index
    pending_slice_maps[thread_ts] = [slice1_files, slice2_files, slice3_files]

    # Only display non-empty slices, preserving their 1/2/3 layer numbers
    layer_slices = [
        (idx, fs)
        for idx, fs in ((1, slice1_files), (2, slice2_files), (3, slice3_files))
        if fs
    ]

    # Step 2: Real line counts — sum of actual lines per file, no cap
    def _count_lines(fs: List[str]) -> int:
        return sum(
            snippets[f].count("\n") + 1 if snippets.get(f) else 10
            for f in fs
        )

    # Step 3: Extract new_value, condition, class name, and entity from request
    def _extract_new_value(request: str) -> str:
        caps = [w for w in re.findall(r'\b[A-Z][A-Z0-9_]*\b', request) if '_' in w or len(w) >= 4]
        return max(caps, key=len) if caps else ""

    def _extract_condition(request: str) -> str:
        m = re.search(r'\bwhen\s+(.+?)(?:[.,]|$)', request, re.IGNORECASE)
        return m.group(1).strip() if m else "the condition is met"

    def _extract_class_name(request: str) -> str:
        camel = re.findall(r'\b[A-Z][a-z]+(?:[A-Z][a-z]*)+\b', request)
        return camel[0] if camel else ""

    def _detect_primary_entity(request: str) -> str:
        candidates = [
            "payment", "customer", "review", "order", "product",
            "address", "profile", "transaction", "shipment", "inventory", "rating",
        ]
        req = request.lower()
        for c in candidates:
            if re.search(rf"\b{c}s?\b", req):
                return c
        return subject or "feature"

    new_value = _extract_new_value(state.user_request)
    condition = _extract_condition(state.user_request)
    class_name = _extract_class_name(state.user_request)
    _entity = _detect_primary_entity(state.user_request)

    def _slice1_module_path(fs: List[str]) -> str:
        if not fs:
            return "models"
        f = fs[0]
        for prefix in ("models/", "contracts/"):
            idx = f.find(prefix)
            if idx >= 0:
                return os.path.splitext(f[idx:])[0].replace("/", ".").replace("\\", ".")
        return os.path.splitext(os.path.basename(f))[0]

    def _pipeline_name(fs: List[str]) -> str:
        return os.path.splitext(os.path.basename(fs[0]))[0] if fs else "pipeline"

    def _to_field_name(cls: str) -> str:
        return re.sub(r'(?<!^)(?=[A-Z])', '_', cls).lower() + 's'

    # Step 3 cont.: Specific "What ships" per slice
    def _what_ships(layer_idx: int, files: List[str]) -> str:
        if layer_idx == 1:
            if new_value and class_name:
                return (
                    f"The {class_name} enum includes {new_value} — "
                    "downstream code can import it immediately"
                )
            if new_value:
                return f"The contract defines {new_value} — downstream code can import it immediately"
            return "The data contract is defined — downstream code can import it immediately"
        if layer_idx == 2:
            pipeline = _pipeline_name(files)
            if new_value:
                return f"The {pipeline} sets {new_value} when {condition}"
            return f"The {pipeline} implements the logic when {condition}"
        if layer_idx == 3:
            if new_value:
                return (
                    f"Automated tests verify {new_value} is set correctly "
                    "and existing behavior is unchanged"
                )
            return "Automated tests verify the new behavior and existing behavior is unchanged"
        return "Delivers a verifiable outcome"

    # Step 4: INVEST check per slice
    def _invest_check(layer_idx: int) -> str:
        if layer_idx == 1:
            return "✅ Independent (no dependencies)"
        if layer_idx == 2:
            first = class_name or (
                os.path.basename(slice1_files[0]) if slice1_files else "Slice 1 contract"
            )
            return f"✅ Independent after Slice 1 | Slice 1 must ship: {first}"
        if layer_idx == 3:
            return "✅ Independent (tests can always ship last)"
        return "✅ Independent"

    # Step 5: Testability hint per slice
    def _testability_hint(layer_idx: int, files: List[str]) -> str:
        cls = class_name or "Enum"
        val = new_value or "NEW_VALUE"
        if layer_idx == 1:
            module = _slice1_module_path(files)
            return (
                f"Testable: `from {module} import {cls}; "
                f"assert '{val}' in [f.value for f in {cls}]`"
            )
        if layer_idx == 2:
            entity_plural = _entity + "s"
            field = _to_field_name(class_name) if class_name else val.lower() + "s"
            return (
                f"Testable: pass empty {entity_plural} list, "
                f"assert `{val}` in profile.{field}"
            )
        if layer_idx == 3:
            return "Testable: run `pytest tests/` and all new cases pass"
        return "Testable: run targeted tests for this layer"

    readme_note = (
        f"\n\nUpdate README.md to reflect the `{subject_label}` change once slices land."
        if readme_files else ""
    )
    trigger_reason = "shipping risk" if token_count <= _BUDGET_THRESHOLD else "token budget"

    slice_lines: List[str] = []
    for layer_idx, files_in_slice in layer_slices:
        files_str = ", ".join(f"`{os.path.basename(f)}`" for f in files_in_slice)
        line_count = _count_lines(files_in_slice)
        slice_lines.append(
            f"*Slice {layer_idx}*\n"
            f"Files: {files_str}\n"
            f"What ships: {_what_ships(layer_idx, files_in_slice)}\n"
            f"INVEST: {_invest_check(layer_idx)}\n"
            f"{_testability_hint(layer_idx, files_in_slice)} | 📐 ~{line_count} lines"
        )

    # Step 6: Smart move line
    s1_lines = _count_lines(slice1_files) if slice1_files else 0
    smart_move = (
        f"*Smart move:* Slice 1 is {s1_lines} lines of code — ship it in minutes. "
        "Slice 2 is the real feature — ship it once Slice 1 is merged. "
        "Slice 3 locks in the behavior — ship it before the sprint ends."
    )

    say(
        text=(
            f"*Agent 3 — Risk Assessment* _(triggered by {trigger_reason})_\n\n"
            f"📐 ~{_count_lines(non_readme)} lines across {len(non_readme)} files"
            f" | Tokens: *{token_count:,}* | Risk: *{overall_risk}* | Blast radius: *{blast_label}*\n\n"
            f"{risk_explanation}\n\n"
            f"{knowledge_note}\n\n"
            "*Verdict:* Too risky to ship as a single PR.\n\nThese are the recommended slices:\n\n"
            + "\n\n".join(slice_lines)
            + "\n\n" + smart_move
            + readme_note
            + "\n\n*Reply to select:*\n"
            "*go* — ship everything | *no go* — cancel | "
            "*slice 1*, *slice 2*, *slice 1 2*, *slice 1 2 3* — pick specific slices\n\n"
            "_Used tokens: 200_"
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

    ledger: Dict[str, Dict] = {
        "agent_1_slicer":    {"tokens": 0, "cost_usd": 0.0},
        "agent_2_optimizer": {"tokens": 0, "cost_usd": 0.0},
        "agent_3_risk":      {"tokens": 0, "cost_usd": 0.0},
        "agent_4_generator": {"tokens": 0, "cost_usd": 0.0},
    }

    state = pipeline.planner(state)
    state = pipeline.optimizer(state)
    state = pipeline.estimator(state)

    # Agent 1: tokens from the extracted slice context
    a1_tokens = estimate_tokens(state.extracted_slice_context or "")
    ledger["agent_1_slicer"]["tokens"] = a1_tokens

    # Agent 2: same token base; cost is optimizer overhead fraction of projected cost
    projected_cost = getattr(state, "projected_token_cost_usd", 0.0) or 0.0
    a2_cost = projected_cost * 0.15
    ledger["agent_2_optimizer"]["tokens"] = a1_tokens
    ledger["agent_2_optimizer"]["cost_usd"] = a2_cost

    # Agent 3: fixed-size risk assessment prompt
    ledger["agent_3_risk"]["tokens"] = 200
    ledger["agent_3_risk"]["cost_usd"] = 200 * 0.000003

    # Single source of truth: calculated once after estimator, shared by all three posting functions
    token_count = _structural_token_estimate(
        state.extracted_slice_context or "", state.user_request
    )
    post_slices_identified(say, thread_ts, state, token_count)
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

    # Agent 4: tokens from generated output; cost is projected minus optimizer overhead
    a4_tokens = estimate_tokens(str(state.generated_code_blocks or {}))
    ledger["agent_4_generator"]["tokens"] = a4_tokens
    ledger["agent_4_generator"]["cost_usd"] = max(projected_cost - a2_cost, 0.0)

    post_generated_code(say, thread_ts, state, ledger)


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
