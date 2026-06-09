"""Pure logic for Agent 3 — Risk Assessment.

All functions here are Slack-free and fully testable in isolation.
slack_app.py imports from this module; tests import from here directly.
"""

import os
import re
from typing import Dict, List, Optional, Tuple

from ai.tokenizer import estimate_tokens

_MINS_PER_FILE = {"HIGH": 30, "MEDIUM": 20, "LOW": 12}
_COUPLING_OVERHEAD_MINS = 30


# ── Time helpers ──────────────────────────────────────────────────────────────

def review_minutes(file_count: int, risk: str, has_coupling: bool = False) -> int:
    return file_count * _MINS_PER_FILE.get(risk, 20) + (_COUPLING_OVERHEAD_MINS if has_coupling else 0)


def fmt_time(minutes: int) -> str:
    if minutes < 60:
        return f"~{minutes} min"
    return f"~{minutes / 60:.1f} hr"


# ── File classification ───────────────────────────────────────────────────────

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


# ── Blast radius ──────────────────────────────────────────────────────────────

def blast_radius_score(files: List[str], graph: Dict[str, List[str]]) -> int:
    """Count total inbound dependencies — how many changed files are depended on by others."""
    return sum(1 for f in files for deps in graph.values() if f in deps)


def build_dep_graph(files: List[str], snippets: Dict[str, str]) -> Dict[str, List[str]]:
    """Parse import statements to build an adjacency list between changed files."""
    stem_to_path = {os.path.splitext(os.path.basename(f))[0]: f for f in files}
    graph: Dict[str, List[str]] = {f: [] for f in files}
    for f in files:
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


# ── Token estimation ──────────────────────────────────────────────────────────

def structural_token_estimate(context: str, user_request: str) -> int:
    """Tokens the model will actually consume: context + request + system overhead + output."""
    ctx_tokens = estimate_tokens(context)
    req_tokens = estimate_tokens(user_request)
    return int((ctx_tokens + req_tokens + 500) * 1.4)


# ── Subject extraction ────────────────────────────────────────────────────────

def extract_change_subject(user_request: str) -> Optional[str]:
    invalid_tokens = {"a", "the", "it"}

    def is_valid(candidate: str) -> bool:
        return len(candidate.strip().lower()) >= 5 and candidate.strip().lower() not in invalid_tokens

    camel = [c for c in re.findall(r'\b[A-Z][a-z]+[A-Z][a-zA-Z]*\b', user_request) if is_valid(c)]
    if camel:
        return camel[0]

    snake = [s for s in re.findall(r'\b[a-z]+_[a-z_]+\b', user_request) if is_valid(s)]
    if snake:
        return snake[0]

    for verb in ("replace", "add", "migrate", "refactor", "update", "introduce"):
        m = re.search(rf'{verb}\s+(\w+)', user_request, re.I)
        if m and is_valid(m.group(1)):
            return m.group(1)

    return None


# ── Risk computation ──────────────────────────────────────────────────────────

def compute_risk(files: List[str], snippets: Dict[str, str]) -> Tuple[str, int, Dict, int, List[str]]:
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


# ── Value label from code ─────────────────────────────────────────────────────

def file_value_label(filename: str, snippet: str) -> str:
    """Extract the first meaningful business description from a file's content."""
    for line in snippet.splitlines()[:30]:
        s = line.strip()
        if (s.startswith('"""') or s.startswith("'''")) and len(s) > 6:
            desc = s.strip('"\'').strip()
            if len(desc) > 10:
                return desc
        if s.startswith('#') and len(s) > 15 and not s.startswith('#!'):
            return s.lstrip('# ').strip()
    return os.path.splitext(os.path.basename(filename))[0].replace('_', ' ').replace('-', ' ')


# ── Snippet parsing ───────────────────────────────────────────────────────────

def parse_snippets(extracted_slice_context: str) -> Dict[str, str]:
    """Parse extracted_slice_context into {filepath: content} dict."""
    snippets: Dict[str, str] = {}
    for chunk in ("\n" + extracted_slice_context).split("\n# FILE: "):
        if not chunk.strip():
            continue
        first_line, _, rest = chunk.partition("\n")
        snippets[first_line.strip()] = rest
    return snippets


# ── Vertical slice grouping ───────────────────────────────────────────────────

def group_vertical_slices(
    titles: List[str],
    all_files: List[str],
    graph: Dict[str, List[str]],
) -> List[Tuple[str, List[str]]]:
    """Assign files to value-based vertical slices using dep graph for cohesion."""
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
