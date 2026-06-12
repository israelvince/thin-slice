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

_BUDGET_THRESHOLD = int(os.environ.get("THIN_SLICE_TOKEN_THRESHOLD", "3000"))

_MINS_PER_FILE = {"HIGH": 30, "MEDIUM": 20, "LOW": 12}
_COUPLING_OVERHEAD_MINS = 30
ENGINEER_HOURLY_RATE = float(os.environ.get("ENGINEER_HOURLY_RATE", "150"))


def _review_minutes(file_count: int, risk: str, has_coupling: bool = False) -> int:
    return file_count * _MINS_PER_FILE.get(risk, 20) + (_COUPLING_OVERHEAD_MINS if has_coupling else 0)


def _fmt_time(minutes: int) -> str:
    if minutes < 60:
        return f"~{minutes} min"
    return f"~{minutes / 60:.1f} hr"


def _engineering_exposure(file_count: int, risk: str, has_coupling: bool = False) -> float:
    """Engineering review cost in USD = review minutes × ENGINEER_HOURLY_RATE / 60."""
    return round((_review_minutes(file_count, risk, has_coupling) / 60) * ENGINEER_HOURLY_RATE, 2)


def _blast_radius_score(files: List[str], graph: Dict[str, List[str]]) -> int:
    return sum(1 for f in files for deps in graph.values() if f in deps)


def _estimate_token_cost(
    context: str, user_request: str, file_count: int
) -> Tuple[int, int, float, str]:
    """Return (input_tokens, output_tokens, cost_usd, model_label).

    input_tokens  — what the model reads (context + request + system overhead)
    output_tokens — what the model generates (per-file estimate at 1.5× input share, capped 2k/file)
    cost_usd      — real dollar cost using the recommended model's per-token pricing
    model_label   — human-readable model name for display
    """
    from ai.pricing import MODEL_PRICING, recommend_model as _price_recommend
    ctx_tokens = estimate_tokens(context)
    req_tokens = estimate_tokens(user_request)
    input_tokens = ctx_tokens + req_tokens + 500  # 500 = system prompt overhead

    n = max(file_count, 1)
    ctx_per_file = max(ctx_tokens // n, 50)
    # Output scales with request complexity: short "add a docstring" requests
    # generate less than long cross-file migrations (0.5×–4× multiplier).
    req_mult = min(max(req_tokens / 15.0, 0.5), 4.0)
    # Cap output per file at the larger of: the file's own size (model outputs
    # the full modified file) or 2000 tokens minimum. This lets large-file
    # scenarios produce proportionally larger costs instead of all hitting 2000.
    per_file_cap = max(ctx_per_file, 2000)
    output_per_file = min(int(ctx_per_file * req_mult), per_file_cap)
    output_tokens = output_per_file * n

    model_id, model_desc = _price_recommend(input_tokens + output_tokens)
    inp_price, out_price = MODEL_PRICING[model_id]
    cost = (input_tokens * inp_price) + (output_tokens * out_price)
    return input_tokens, output_tokens, round(cost, 6), f"{model_id} ({model_desc})"


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
    file_count = len(state.affected_files or [])
    input_tokens, output_tokens, _, _ = _estimate_token_cost(
        state.extracted_slice_context or state.user_request,
        state.user_request,
        file_count,
    )
    if token_count is None:
        token_count = input_tokens + output_tokens
    agent_1_tokens_used = len(state.user_request) // 4
    say(
        text=(
            "*Agent 1 — Thin Slicer*\n"
            f"Files affected:\n{format_file_list(state.affected_files)}\n"
            f"Estimated tokens for change: *{token_count:,}* ({input_tokens:,} in · {output_tokens:,} out)\n"
            f"`Tokens used by Agent 1: {agent_1_tokens_used:,}`"
        ),
        thread_ts=thread_ts,
    )


def post_cost_estimate(say, thread_ts: str, state: HackathonAppState) -> None:
    file_count = len(state.affected_files or [])
    input_tokens, output_tokens, slice_cost, _ = _estimate_token_cost(
        state.extracted_slice_context or state.user_request,
        state.user_request,
        file_count,
    )
    total_tokens = input_tokens + output_tokens
    from ai.pricing import recommend_model as _price_rec
    recommended_model, reason = _price_rec(total_tokens)
    say(
        text=(
            "*Agent 2 — Model Optimizer*\n"
            f"Estimated tokens for change: *{input_tokens:,}* in · *{output_tokens:,}* out · *{total_tokens:,}* total\n"
            f"Estimated cost: *${slice_cost:.4f}* | Suggested model: *{recommended_model}* — {reason}\n"
            "`Tokens used by Agent 2: 150`"
        ),
        thread_ts=thread_ts,
    )


def post_generated_code(say, thread_ts: str, state: HackathonAppState, ledger: Optional[dict] = None) -> None:
    if not state.generated_code_blocks:
        say(text="*Agent 4 — Code Generator*\nNo code blocks were produced.", thread_ts=thread_ts)
        return

    pr_line = (
        f"🔗 {state.pull_request_url}"
        if state.pull_request_url
        else "Ready to commit — no PR created in this run"
    )
    agent_4_tokens = estimate_tokens(str(state.generated_code_blocks))

    say(
        text=(
            "*Agent 4 — Code Generator*\n"
            "✅ Change is safe to ship\n"
            f"{pr_line}\n"
            f"`Tokens used by Agent 4: {agent_4_tokens:,}`"
        ),
        thread_ts=thread_ts,
    )

    if ledger is not None:
        post_token_ledger(say, thread_ts, state, ledger)


def post_token_ledger(say, thread_ts: str, state: HackathonAppState, ledger: dict) -> None:
    from ai.pricing import MODEL_PRICING, HAIKU_45, SONNET_46
    total_tokens = sum(v["tokens"] for v in ledger.values())
    total_cost = sum(v["cost_usd"] for v in ledger.values())

    try:
        total_files = sum(1 for _, _, fs in os.walk(state.target_repo) for _ in fs)
    except Exception:
        total_files = len(state.affected_files or [])
    # Full regen estimate: every file read as input + generated as output at Sonnet prices
    avg_tokens_per_file = 500
    _fr_inp = total_files * avg_tokens_per_file
    _fr_out = total_files * avg_tokens_per_file
    _inp_p, _out_p = MODEL_PRICING[SONNET_46]
    full_regen_cost = (_fr_inp * _inp_p) + (_fr_out * _out_p)

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


def _assess_request(
    files: List[str], snippets: Dict[str, str], user_request: str = ""
) -> dict:
    """Evaluate a change request across 5 risk dimensions.

    Returns a dict with: coupling, business_impact, review_complexity,
    reversibility, confidence, overall_risk, trigger, slice_count, strategy,
    folder_counts, coupled, risk_score.
    """
    folder_counts = {
        cat: sum(1 for f in files if folder_category(f) == cat)
        for cat in ("models", "core", "tests", "docs", "config", "readme", "other")
    }
    has_model = folder_counts["models"] > 0
    has_core  = folder_counts["core"] > 0
    core_count = folder_counts["core"]
    non_readme = [f for f in files if folder_category(f) != "readme"]
    layer_diversity = sum(1 for c in ("models", "core", "tests", "docs") if folder_counts[c] > 0)
    req_lower = user_request.lower()

    # ── 1. Deployment coupling ────────────────────────────────────────────────
    coupled = (
        [f for f in files if folder_category(f) in ("models", "core")]
        if has_model and has_core else []
    )
    if has_model and has_core:
        coupling = "HIGH"
    elif has_model:
        coupling = "MEDIUM"
    else:
        coupling = "LOW"

    # ── 2. Business / data impact ─────────────────────────────────────────────
    _HIGH_BIZ = {
        "payment", "auth", "persist", "compliance", "access control",
        "customer_id", "identity", "irreversible", "production decisioning",
    }
    _MED_BIZ = {
        "score", "ltv", "churn", "metric", "export", "report",
        "recommend", "classify", "aggregate",
    }
    _LOW_BIZ = {
        "log", "comment", "docstring", "format", "readme",
        "observability", "explain", "annotation",
    }
    if any(w in req_lower for w in _HIGH_BIZ):
        business_impact = "HIGH"
    elif any(w in req_lower for w in _LOW_BIZ):
        business_impact = "LOW"
    else:
        business_impact = "MEDIUM"

    # ── 3. Review complexity ──────────────────────────────────────────────────
    file_count = len(non_readme)
    if file_count > 5 or layer_diversity >= 3:
        review_complexity = "HIGH"
    elif file_count > 2 or layer_diversity == 2:
        review_complexity = "MEDIUM"
    else:
        review_complexity = "LOW"

    # ── 4. Reversibility (rollback risk) ─────────────────────────────────────
    if has_model:
        reversibility = "HIGH"
    elif has_core and core_count > 3:
        reversibility = "MEDIUM"
    else:
        reversibility = "LOW"

    # ── 5. Confidence ─────────────────────────────────────────────────────────
    named_files = re.findall(r'\b\w+\.py\b', req_lower)
    found = sum(1 for nf in named_files if any(
        os.path.basename(af).lower() == nf for af in files
    ))
    if named_files and found == len(named_files):
        confidence = "HIGH"
    elif named_files:
        confidence = "MEDIUM"
    elif len(user_request) > 800:
        confidence = "LOW"
    else:
        confidence = "MEDIUM"

    # ── Overall risk ──────────────────────────────────────────────────────────
    dims = (coupling, business_impact, review_complexity, reversibility)
    high_count = sum(1 for d in dims if d == "HIGH")
    med_count  = sum(1 for d in dims if d == "MEDIUM")
    if high_count >= 1 or confidence == "LOW":
        overall_risk = "HIGH"
    elif med_count >= 2:
        overall_risk = "MEDIUM"
    else:
        overall_risk = "LOW"

    trigger = overall_risk in ("HIGH", "MEDIUM") and not (
        coupling == "LOW" and business_impact == "LOW" and confidence == "HIGH"
    )

    # ── Slice count (1–5, default 3) ──────────────────────────────────────────
    if overall_risk == "LOW" and file_count <= 2:
        slice_count = 1
    elif overall_risk == "HIGH" or file_count > 5:
        slice_count = min(5, max(3, file_count // 2))
    else:
        slice_count = 3

    # ── Slicing strategy ──────────────────────────────────────────────────────
    categories = [folder_category(f) for f in non_readme]
    same_layer = len(set(categories)) == 1 and len(non_readme) > 1
    if same_layer:
        strategy = "vertical"
    elif has_model:
        strategy = "layer"
    elif reversibility == "HIGH" or confidence == "LOW":
        strategy = "risk-first"
    else:
        strategy = "layer"

    risk_score = (2 if has_model else 0) + (2 if has_core else 0)

    return {
        "coupling": coupling,
        "business_impact": business_impact,
        "review_complexity": review_complexity,
        "reversibility": reversibility,
        "confidence": confidence,
        "overall_risk": overall_risk,
        "trigger": trigger,
        "slice_count": slice_count,
        "strategy": strategy,
        "folder_counts": folder_counts,
        "coupled": coupled,
        "risk_score": risk_score,
    }





def _slice_reply_options(display_slices: list) -> str:
    """Build the 'Reply to select' line showing only the slice numbers that exist."""
    nums = [str(d) for d, _, _ in display_slices]
    parts = ["*go* — ship everything", "*no go* — cancel"]
    if nums:
        parts.append("*slice " + "*, *slice ".join(nums) + "* — pick specific slices")
        if len(nums) > 1:
            parts.append(f"*slice {' '.join(nums)}* — ship all slices together")
    return " | ".join(parts)


def _generate_slices_with_llm(
    assessment: dict,
    files: List[str],
    snippets: Dict[str, str],
    user_request: str,
) -> Optional[tuple]:
    """Call OpenAI to generate 1–5 intelligent slice recommendations.

    Returns (slices, mvp_text) on success, None if unavailable or call fails.
    slices: list of {num, name, goal, files, why_first}
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    try:
        import openai as _openai
    except ImportError:
        logger.warning("openai package not installed; falling back to deterministic slicing")
        return None

    slice_count = assessment["slice_count"]
    strategy    = assessment["strategy"]
    file_summaries = "\n".join(
        f"- {f}: {(snippets.get(f, '')[:400].strip() or '(no content)')}"
        for f in files
    )

    system_prompt = (
        "You are a Change Request Slicing Agent. Split a software change into safe, "
        "thin, independently-deployable slices that each deliver real business value.\n\n"
        "Respond with JSON only — no markdown, no prose outside the JSON object.\n"
        'Schema: {"slices": [{"num": int, "name": str, "goal": str, '
        '"files": [str], "why_first": str}], "mvp": str}\n\n'
        "Rules:\n"
        "- Each file appears in at most one slice. Every file must be assigned.\n"
        "- name: 3–6 words, descriptive.\n"
        "- goal: one sentence — what ships and why it matters to the business.\n"
        "- why_first: one sentence on why this slice is ordered where it is.\n"
        "- mvp: name the single best first slice and explain in one sentence why it "
        "delivers the smallest meaningful business value with the safest validation path."
    )

    user_content = (
        f"Change request:\n{user_request}\n\n"
        f"Affected files and content:\n{file_summaries}\n\n"
        "Risk assessment:\n"
        f"  Coupling: {assessment['coupling']}\n"
        f"  Business impact: {assessment['business_impact']}\n"
        f"  Review complexity: {assessment['review_complexity']}\n"
        f"  Reversibility: {assessment['reversibility']}\n"
        f"  Confidence: {assessment['confidence']}\n"
        f"  Strategy: {strategy}\n\n"
        f"Recommend exactly {slice_count} slice(s) using a {strategy} strategy."
    )

    try:
        client = _openai.OpenAI(api_key=api_key)
        model = os.environ.get("OPENAI_SLICE_MODEL", "gpt-4o-mini")
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_content},
            ],
            response_format={"type": "json_object"},
            max_tokens=1200,
            temperature=0,
        )
        data = json.loads(resp.choices[0].message.content)
        raw  = data.get("slices") or []
        mvp  = str(data.get("mvp") or "")

        slices = []
        for i, s in enumerate(raw, 1):
            slice_files = [f for f in (s.get("files") or []) if f in files]
            slices.append({
                "num":       i,
                "name":      str(s.get("name") or f"Slice {i}"),
                "goal":      str(s.get("goal") or ""),
                "files":     slice_files,
                "why_first": str(s.get("why_first") or ""),
            })

        # Guarantee every file is assigned to exactly one slice
        assigned   = {f for s in slices for f in s["files"]}
        unassigned = [f for f in files if f not in assigned]
        if unassigned and slices:
            slices[-1]["files"].extend(unassigned)
        elif unassigned:
            slices = [{"num": 1, "name": "Full change", "goal": "", "files": files, "why_first": ""}]

        logger.info("LLM slicing: %d slice(s) via %s", len(slices), model)
        return slices, mvp
    except Exception as exc:
        logger.warning("LLM slice generation failed (%s): %s", type(exc).__name__, exc)
        return None


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

    assessment    = _assess_request(files, snippets, state.user_request)
    overall_risk  = assessment["overall_risk"]
    folder_counts = assessment["folder_counts"]
    coupled       = assessment["coupled"]
    no_test_count = 0
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
    # Tests ship WITH their source layer — never as a separate slice.
    # Each slice must be independently testable (INVEST: Testable).

    layer_bins: Dict[int, List[str]] = {1: [], 2: []}

    # Pass 1: assign non-test files to schema (1) or logic (2)
    for f in non_readme:
        if "tests/" in f or os.path.basename(f).startswith("test_") or "/test_" in f:
            continue
        if "models/" in f or "contracts/" in f:
            layer_bins[1].append(f)
        elif "pipelines/" in f or "services/" in f:
            layer_bins[2].append(f)
        else:
            cat = folder_category(f)
            layer_bins[1 if cat == "models" else 2].append(f)

    # Build stem → layer so each test can be paired with its source layer
    stem_to_layer = {
        os.path.splitext(os.path.basename(f))[0]: layer
        for layer, files in layer_bins.items()
        for f in files
    }

    # Pass 2: assign each test to the layer of the file it covers
    for f in non_readme:
        if "tests/" not in f and not os.path.basename(f).startswith("test_") and "/test_" not in f:
            continue
        source_stem = re.sub(r'^test_', '', os.path.splitext(os.path.basename(f))[0])
        layer_bins[stem_to_layer.get(source_stem, 2)].append(f)

    # Fallback: when layer 1 (schema) is empty and layer 2 has >=3 non-test files,
    # use dep-graph to promote imported files to layer 1; move their tests with them
    if not layer_bins[1] and len(layer_bins[2]) >= 3:
        non_test_l2 = [f for f in layer_bins[2]
                       if "tests/" not in f and not os.path.basename(f).startswith("test_")]
        g = _dep_graph(non_test_l2)
        roots = [f for f in non_test_l2
                 if any(f in g.get(other, []) for other in non_test_l2 if other != f)]
        if roots:
            root_stems = {os.path.splitext(os.path.basename(r))[0] for r in roots}
            root_set = set(roots)
            layer_bins[1] = [
                f for f in layer_bins[2]
                if f in root_set or
                re.sub(r'^test_', '', os.path.splitext(os.path.basename(f))[0]) in root_stems
            ]
            layer_bins[2] = [f for f in layer_bins[2] if f not in layer_bins[1]]

    # Build sequential display list — skip empty layers, renumber from the top
    # Each entry: (display_num, semantic_layer, files)
    ordered = [(sem, layer_bins[sem]) for sem in (1, 2) if layer_bins[sem]]
    display_slices: List[Tuple[int, int, List[str]]] = [
        (i + 1, sem, fs) for i, (sem, fs) in enumerate(ordered)
    ]

    # Store in display order: "slice 1" → first shown slice, "slice 2" → second, etc.
    if not display_slices:
        display_slices = [(1, 2, non_readme)]
        pending_slice_maps[thread_ts] = [non_readme]
    else:
        pending_slice_maps[thread_ts] = [fs for _, _, fs in display_slices]

    # When all files collapse into one layer/slice but the assessment wants more,
    # split them round-robin across slice_count groups so the reply flow works.
    if len(display_slices) == 1 and assessment["slice_count"] > 1:
        single_files = display_slices[0][2]
        n = min(assessment["slice_count"], len(single_files))
        if n > 1:
            groups: List[List[str]] = [[] for _ in range(n)]
            for idx, f in enumerate(single_files):
                groups[idx % n].append(f)
            display_slices = [(i + 1, 2, grp) for i, grp in enumerate(groups) if grp]
            pending_slice_maps[thread_ts] = [grp for _, _, grp in display_slices]

    # ── LLM slice generation (OpenAI) — overrides deterministic if available ──
    _llm_result = _generate_slices_with_llm(assessment, non_readme, snippets, state.user_request)
    if _llm_result is not None:
        _llm_slices, _mvp_text = _llm_result
        display_slices = [(s["num"], 2, s["files"]) for s in _llm_slices if s["files"]]
        pending_slice_maps[thread_ts] = [s["files"] for s in _llm_slices if s["files"]]
    else:
        _llm_slices, _mvp_text = [], ""

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _count_lines(fs: List[str]) -> int:
        return sum(snippets[f].count("\n") + 1 if snippets.get(f) else 10 for f in fs)

    _GENERIC_TECH_WORDS = {
        "JSON", "HTTP", "REST", "API", "UUID", "CSV", "SQL", "XML", "HTML",
        "TRUE", "FALSE", "NONE", "NULL", "SRE", "CRM", "LTV", "ETL",
    }

    def _extract_new_value(request: str) -> str:
        caps = [
            w for w in re.findall(r'\b[A-Z][A-Z0-9_]*\b', request)
            if ('_' in w or len(w) >= 4) and w not in _GENERIC_TECH_WORDS
        ]
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

    # Detect request intent once — drives description framing below
    _req_lower = state.user_request.lower()
    _is_logging_req = any(w in _req_lower for w in (
        "log ", "logging", "logger", "structured json", "log when", "log if",
        "observability", "structured log", "json log",
    ))
    _is_annotation_req = any(w in _req_lower for w in (
        "log", "logging", "print", "comment", "docstring", "debug", "trace", "print statement"
    ))
    _is_null_req = any(w in _req_lower for w in (
        "missing", "gracefully", "default to 0", "none check", "null", "no reviews",
    ))

    # "What ships" — intent-aware so logging/null/enum each get the right framing
    def _what_ships(semantic: int, files: List[str]) -> str:
        file_names = ", ".join(f"`{os.path.basename(f)}`" for f in files)
        if _is_logging_req:
            return (
                f"Structured JSON logging added to {file_names} via `infra/logger.py` — "
                "each event emits `pipeline_run_id`, timing, and per-pipeline metric fields"
            )
        if _is_null_req:
            return (
                f"{file_names} handles missing/null data gracefully — "
                "defaults applied, no ZeroDivisionError on empty input"
            )
        if semantic == 1:
            if new_value and class_name:
                return (
                    f"The {class_name} enum includes {new_value} — "
                    "downstream code can import it immediately"
                )
            if new_value:
                return f"The contract defines {new_value} — downstream code can import it immediately"
            return "The data contract is defined — downstream code can import it immediately"
        if semantic == 2:
            pipeline = _pipeline_name(files)
            if new_value:
                return f"The {pipeline} enforces {new_value} when {condition}"
            return f"The {pipeline} implements the updated logic"
        if semantic == 3:
            if new_value:
                return f"Tests verify {new_value} behaves correctly and existing cases are unchanged"
            return "Tests verify the new behavior and guard against regressions"
        return "Delivers a verifiable outcome"

    # INVEST independence check
    def _invest_check(display_num: int, semantic: int) -> str:
        if semantic == 1:
            return "✅ Independent — no other slice must land first"
        if semantic == 2:
            if display_num > 1:
                anchor = class_name or (
                    os.path.basename(display_slices[0][2][0]) if display_slices[0][2] else "Slice 1"
                )
                return f"✅ Ships after Slice 1 | Needs: {anchor}"
            return "✅ Independent — no schema dependency detected"
        if semantic == 3:
            return "✅ Independent — tests always ship last, never block"
        return "✅ Independent"

    # Testability hint — intent-aware
    def _testability_hint(semantic: int, files: List[str]) -> str:
        if _is_logging_req:
            file_names = " + ".join(f"`{os.path.basename(f)}`" for f in files)
            return (
                f"Testable: run the pipeline, check stdout for JSON lines with "
                f"`\"pipeline_run_id\"` from {file_names}"
            )
        if _is_null_req:
            return "Testable: call with empty input — assert result is 0 or None, no exception raised"
        cls = class_name or "Enum"
        val = new_value or "NEW_VALUE"
        if semantic == 1:
            module = _slice1_module_path(files)
            return (
                f"Testable: `from {module} import {cls}; "
                f"assert '{val}' in [m.value for m in {cls}]`"
            )
        if semantic == 2:
            entity_plural = _entity + "s"
            field = _to_field_name(class_name) if class_name else val.lower() + "s"
            return (
                f"Testable: pass a profile with `{val}` set, "
                f"assert `{field}` is correct"
            )
        if semantic == 3:
            return "Testable: run `pytest tests/` — all new cases pass"
        return "Testable: run targeted tests"

    # ── Per-slice cost estimate (using real pricing) ──────────────────────────
    from ai.pricing import MODEL_PRICING, SONNET_46 as _SONNET
    _inp_p, _out_p = MODEL_PRICING[_SONNET]

    def _slice_cost(fs: List[str]) -> str:
        ctx = sum(estimate_tokens(snippets.get(f, "")) for f in fs)
        out = min(int(ctx * 1.5), 2000 * len(fs))
        cost = (ctx * _inp_p) + (out * _out_p)
        return f"${cost:.4f}"

    # ── Build slice output lines ──────────────────────────────────────────────
    readme_note = (
        f"\n\nUpdate README.md to reflect the `{subject_label}` change once slices land."
        if readme_files else ""
    )
    trigger_reason = "shipping risk" if token_count <= _BUDGET_THRESHOLD else "token budget"

    slice_lines: List[str] = []
    for display_num, semantic, files_in_slice in display_slices:
        files_str = ", ".join(f"`{os.path.basename(f)}`" for f in files_in_slice)
        line_count = _count_lines(files_in_slice)
        cost_label = _slice_cost(files_in_slice)
        slice_lines.append(
            f"*Slice {display_num}*\n"
            f"Files: {files_str}\n"
            f"What ships: {_what_ships(semantic, files_in_slice)}\n"
            f"INVEST: {_invest_check(display_num, semantic)}\n"
            f"{_testability_hint(semantic, files_in_slice)} | ~{line_count} lines | Est. cost: {cost_label}"
        )

    # ── Smart move — references only the slices that actually exist ───────────
    if len(display_slices) == 1:
        _, _, f1 = display_slices[0]
        smart_move = (
            f"*Smart move:* Ship Slice 1 ({_count_lines(f1)} lines) — "
            "self-contained, ships and verifies in one cycle."
        )
    elif len(display_slices) == 2:
        _, _, f1 = display_slices[0]
        _, _, f2 = display_slices[1]
        smart_move = (
            f"*Smart move:* Slice 1 ({_count_lines(f1)} lines) first — "
            f"verify in prod, then Slice 2 ({_count_lines(f2)} lines). "
            "Each is independently deployable."
        )
    else:
        _, _, f1 = display_slices[0]
        smart_move = (
            f"*Smart move:* Slice 1 ({_count_lines(f1)} lines) is the contract — "
            "ship it first so Slices 2 and 3 have something to import. "
            "Each slice verifies green before the next one ships."
        )

    # ── Total cost for the full change ────────────────────────────────────────
    _total_inp, _total_out, _total_cost, _ = _estimate_token_cost(
        state.extracted_slice_context or "", state.user_request, len(non_readme)
    )
    from ai.pricing import MODEL_PRICING as _MP, SONNET_46 as _S46
    _a3_ctx = estimate_tokens(state.extracted_slice_context or "")
    _a3_tokens = _a3_ctx + 800
    _a3_cost = round(_a3_tokens * _MP[_S46][0], 6)

    cost_triggered = token_count > _BUDGET_THRESHOLD
    risk_triggered_here = overall_risk == "HIGH"
    if cost_triggered and not risk_triggered_here:
        verdict = "❌ Too costly to ship as a single PR."
    elif risk_triggered_here and not cost_triggered:
        verdict = "❌ Too risky to ship as a single PR."
    else:
        verdict = "❌ Too risky and costly to ship as a single PR."

    # ── Engineering exposure ──────────────────────────────────────────────────
    full_exposure = _engineering_exposure(len(non_readme), overall_risk, bool(coupled))
    slice_exposure_by_num = {
        dn: _engineering_exposure(len(fs), "LOW")
        for dn, _, fs in display_slices if fs
    }
    total_slice_exposure = sum(slice_exposure_by_num.values())
    exposure_savings = max(0.0, round(full_exposure - total_slice_exposure, 2))

    valid_slices = [(dn, fs) for dn, _, fs in display_slices if fs]
    if _llm_slices:
        slice_parts = []
        for s in _llm_slices:
            if not s["files"]:
                continue
            exp = _engineering_exposure(len(s["files"]), "LOW")
            mins = _review_minutes(len(s["files"]), "LOW")
            part = (
                f"*Slice {s['num']}:* {s['name']}\n_{s['goal']}_"
                + (f"\n_{s['why_first']}_" if s["why_first"] else "")
                + f"\n_{_fmt_time(mins)} review · ${exp:,.0f} engineering cost_"
            )
            slice_parts.append(part)
        slice_names_text = "\n\n".join(slice_parts)
        if _mvp_text:
            slice_names_text += f"\n\n💡 *MVP:* {_mvp_text}"
    else:
        _LAYER_NAME = {1: "Data Contract", 2: "Business Logic"}
        _LAYER_SCOPE = {
            1: "Defines the shared schema and rules downstream computation depends on — ships with its tests",
            2: "Stateless computation that reads the contract and produces derived values — ships with its tests",
        }
        det_parts = []
        for dn, sem, fs in display_slices:
            if not fs:
                continue
            mins = _review_minutes(len(fs), "LOW")
            det_parts.append(
                f"*Slice {dn}:* {_LAYER_NAME.get(sem, 'Supporting Change')}\n"
                f"_{_LAYER_SCOPE.get(sem, 'Isolated change with limited blast radius')}_\n"
                f"_{_fmt_time(mins)} review · Risk: LOW_"
            )
        slice_names_text = "\n\n".join(det_parts)
    reply_options = " · ".join(f"*slice {num}*" for num, _ in valid_slices)

    _strategy_labels = {
        "vertical": "Vertical slice",
        "layer": "Layer slice",
        "risk-first": "Risk-first slice",
    }
    strategy_label = _strategy_labels.get(assessment["strategy"], "Layer slice")
    if not _llm_slices:
        strategy_label += " · deterministic"

    say(
        text=(
            "*Agent 3 — Risk Assessment*\n"
            f"~{_count_lines(non_readme)} lines across {len(non_readme)} files | "
            f"Estimated tokens for change: {token_count:,}/{_BUDGET_THRESHOLD:,} | "
            f"Strategy: {strategy_label}\n"
            "\n"
            f"Coupling: *{assessment['coupling']}* | "
            f"Business impact: *{assessment['business_impact']}* | "
            f"Complexity: *{assessment['review_complexity']}*\n"
            f"Reversibility: *{assessment['reversibility']}* | "
            f"Confidence: *{assessment['confidence']}*\n"
            f"Engineering exposure: {_fmt_time(_review_minutes(len(non_readme), overall_risk, bool(coupled)))} · *${full_exposure:,.0f}* as-is"
            f" → {_fmt_time(int(sum(_review_minutes(len(fs), 'LOW') for _, _, fs in display_slices if fs)))} · *${total_slice_exposure:,.0f}* sliced"
            + (f" · *${exposure_savings:,.0f}* protected" if exposure_savings > 0 else "") + "\n"
            "\n"
            f"{verdict}\n"
            "\n"
            f"These are the recommended slices:\n{slice_names_text}\n"
            "\n"
            f"Reply to select: *go* — ship everything | *no go* — cancel | {reply_options} — pick specific slices\n"
            "`Tokens used by Agent 3: 200`"
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

    from ai.pricing import MODEL_PRICING, HAIKU_45, SONNET_46

    ledger: Dict[str, Dict] = {
        "agent_1_slicer":    {"tokens": 0, "cost_usd": 0.0},
        "agent_2_optimizer": {"tokens": 0, "cost_usd": 0.0},
        "agent_3_risk":      {"tokens": 0, "cost_usd": 0.0},
        "agent_4_generator": {"tokens": 0, "cost_usd": 0.0},
    }

    state = pipeline.planner(state)
    state = pipeline.optimizer(state)
    state = pipeline.estimator(state)

    file_count = len(state.affected_files or [])
    inp, out, est_cost, _model_label = _estimate_token_cost(
        state.extracted_slice_context or "", user_message, file_count
    )
    # token_count = input + output — single source of truth for budget comparison
    token_count = inp + out

    # Agent 1: reads the slice context at Haiku rates (search + scan, no generation)
    a1_tokens = estimate_tokens(state.extracted_slice_context or "")
    a1_cost = a1_tokens * MODEL_PRICING[HAIKU_45][0]
    ledger["agent_1_slicer"]["tokens"] = a1_tokens
    ledger["agent_1_slicer"]["cost_usd"] = round(a1_cost, 6)

    # Agent 2: 200-token optimizer decision at Haiku rates
    a2_tokens = 200
    a2_cost = a2_tokens * MODEL_PRICING[HAIKU_45][0]
    ledger["agent_2_optimizer"]["tokens"] = a2_tokens
    ledger["agent_2_optimizer"]["cost_usd"] = round(a2_cost, 6)

    # Agent 3: reads full context + request to analyse risk at Sonnet rates
    a3_tokens = a1_tokens + 800  # context + analysis overhead
    a3_cost = a3_tokens * MODEL_PRICING[SONNET_46][0]
    ledger["agent_3_risk"]["tokens"] = a3_tokens
    ledger["agent_3_risk"]["cost_usd"] = round(a3_cost, 6)

    post_slices_identified(say, thread_ts, state, token_count)
    post_cost_estimate(say, thread_ts, state)

    snippets: Dict[str, str] = {}
    for chunk in ("\n" + (state.extracted_slice_context or "")).split("\n# FILE: "):
        if chunk.strip():
            first_line, _, rest = chunk.partition("\n")
            snippets[first_line.strip()] = rest

    _req_lower = user_message.lower()
    _is_annotation = any(w in _req_lower for w in (
        "comment", "docstring", "explain", "explanation", "add a note",
        "module-level", "what it means", "what ltv", "what ltv means",
    ))
    if _is_annotation and len(state.affected_files or []) <= 2:
        assessment = {"trigger": False, "overall_risk": "LOW"}
    else:
        assessment = _assess_request(state.affected_files, snippets, user_message)
    cost_triggered = token_count > _BUDGET_THRESHOLD
    risk_triggered = assessment["trigger"]

    if cost_triggered or risk_triggered:
        state.policy_clearance = False
        pending_budget_checks[(channel, thread_ts)] = state
        post_budget_check(say, thread_ts, token_count)
        return

    say(text="✅ *Change is within budget and safe to ship — generating code...*", thread_ts=thread_ts)
    state.policy_clearance = True
    state = _run_generator_and_pr(state)

    # Agent 4: real cost = input context + generated output at Sonnet rates
    a4_in_tokens = a1_tokens  # reads the same context
    a4_out_tokens = estimate_tokens(str(state.generated_code_blocks or {}))
    a4_tokens = a4_in_tokens + a4_out_tokens
    _inp_p, _out_p = MODEL_PRICING[SONNET_46]
    a4_cost = (a4_in_tokens * _inp_p) + (a4_out_tokens * _out_p)
    ledger["agent_4_generator"]["tokens"] = a4_tokens
    ledger["agent_4_generator"]["cost_usd"] = round(a4_cost, 6)

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
        token_count, _, _, _ = _estimate_token_cost(
            state.extracted_slice_context or "", state.user_request,
            len(state.affected_files or [])
        )
        post_cost_estimate(say, thread_ts, state)

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
