"""Persistent run history for RegenAgent.

Entries are written to eng/knowledge/runs.jsonl, one JSON object per line.
Each entry records a completed generation run — real or seeded — and is used
by get_reference_summary to calibrate token estimates against actual history.

Entries WITHOUT is_seed are treated as real runs and preferred by
get_reference_summary when computing reference statistics.
"""
import datetime
import json
import os
from typing import Dict, List, Optional

_RUNS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "knowledge", "runs.jsonl")


def log_run(
    user_request: str,
    affected_files: List[str],
    input_tokens: int,
    output_tokens: int,
    total_cost_usd: float,
    model: str,
    slices: List,
    generated_code: Dict[str, str],
    user_decision: str,
    pr_url: Optional[str],
) -> None:
    """Append one run entry to runs.jsonl."""
    lines_generated = sum(v.count("\n") + 1 for v in generated_code.values()) if generated_code else 0
    entry = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "user_request": user_request,
        "affected_files": affected_files,
        "file_count": len(affected_files),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "total_cost_usd": total_cost_usd,
        "model": model,
        "slices": slices,
        "lines_generated": lines_generated,
        "user_decision": user_decision,
        "pr_url": pr_url,
    }
    os.makedirs(os.path.dirname(_RUNS_PATH), exist_ok=True)
    with open(_RUNS_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def get_reference_summary(limit: int = 50) -> List[dict]:
    """Return recent run entries, preferring real runs over seeded ones.

    Real runs (no is_seed field) are returned first, then seeded entries
    to fill up to limit. Within each group, most recent first.
    """
    if not os.path.exists(_RUNS_PATH):
        return []
    entries = []
    with open(_RUNS_PATH, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    real   = [e for e in entries if not e.get("is_seed")]
    seeded = [e for e in entries if e.get("is_seed")]
    combined = list(reversed(real)) + list(reversed(seeded))
    return combined[:limit]
