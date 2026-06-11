"""Rule-based code generation for demo fallback.

When no LLM is available, this module produces realistic, file-aware changes
based on the request type and the actual content of the affected files.
Handles the core demo scenarios: logging, enum migration, null handling.
"""
import os
import re
from typing import Dict, List, Optional


# ── Intent detection ──────────────────────────────────────────────────────────

def detect_intent(user_request: str) -> str:
    req = user_request.lower()
    if any(w in req for w in ["log statement", "log when", "log ", "logging", "logger", "log if"]):
        return "logging"
    if any(w in req for w in ["print statement", "print(", "print when", "print at the start", "hello world"]):
        return "print_statement"
    if any(w in req for w in ["error handling", "try/except", "try except", "exception", "handle error", "wrap in try"]):
        return "error_handling"
    if any(w in req for w in ["comment to the top", "comment at the top", "comment explaining", "add a comment"]):
        return "comment_top"
    if any(w in req for w in ["riskcategory", "risk_category", "enum", "migrate risk_level", "replace risk_level"]):
        return "enum_migration"
    if any(w in req for w in ["missing", "gracefully", "default to 0", "no reviews", "no order history", "null", "none check"]):
        return "null_handling"
    if any(w in req for w in ["docstring", "documentation", "describe formula", "explaining the"]):
        return "docstring"
    if any(w in req for w in ["input validation", "validate input", "validate all", "validation to all"]):
        return "input_validation"
    return "generic"


def resolve_target_function(user_request: str) -> Optional[str]:
    """Extract the target function name from the request, e.g. 'main function' → 'main'."""
    req = user_request.lower()
    # Named function: "the X function" or "function X" or "def X"
    patterns = [
        r'the\s+(\w+)\s+function',
        r'function\s+(\w+)',
        r'method\s+(\w+)',
        r'(\w+)\s+function',
        r'to\s+the\s+(\w+)\s+function',
    ]
    for pattern in patterns:
        m = re.search(pattern, req)
        if m:
            name = m.group(1)
            if name not in ("the", "a", "an", "this", "that", "each", "all", "every"):
                return name
    return None


def load_file(filepath: str, snippets: Dict[str, str], repo_path: str) -> str:
    content = snippets.get(filepath, "")
    if not content:
        try:
            with open(os.path.join(repo_path, filepath), encoding="utf-8") as f:
                content = f.read()
        except Exception:
            pass
    return content


# ── Main entry point ──────────────────────────────────────────────────────────

def generate(
    user_request: str,
    affected_files: List[str],
    snippets: Dict[str, str],
    repo_path: str,
) -> Dict[str, str]:
    intent = detect_intent(user_request)
    files = [f for f in affected_files if f != "<unspecified>"]
    if not files:
        return {}

    if intent == "logging":
        return _apply_logging(files, snippets, repo_path, user_request)
    if intent == "print_statement":
        return _apply_print_statement(files[0], snippets, repo_path, user_request)
    if intent == "error_handling":
        return _apply_error_handling(files[0], snippets, repo_path, user_request)
    if intent == "comment_top":
        return _apply_comment_top(files[0], snippets, repo_path, user_request)
    if intent == "enum_migration":
        return _apply_enum_migration(files, snippets, repo_path)
    if intent == "null_handling":
        return _apply_null_handling(files[0], snippets, repo_path, user_request)
    if intent == "docstring":
        return _apply_docstring(files[0], snippets, repo_path, user_request)
    if intent == "input_validation":
        return _apply_input_validation(files[0], snippets, repo_path, user_request)
    return _apply_generic(files, snippets, repo_path)


# ── Logging transformation ────────────────────────────────────────────────────

def _apply_logging(
    files: List[str],
    snippets: Dict[str, str],
    repo_path: str,
    request: str,
) -> Dict[str, str]:
    """Apply structured JSON logging to every affected file."""
    changes: Dict[str, str] = {}
    for filepath in files:
        result = _apply_logging_to_file(filepath, snippets, repo_path, request)
        if result:
            changes.update(result)
    return changes


def _apply_logging_to_file(
    filepath: str,
    snippets: Dict[str, str],
    repo_path: str,
    request: str,
) -> Dict[str, str]:
    content = load_file(filepath, snippets, repo_path)
    if not content:
        return {}

    base = os.path.basename(filepath)
    lines = content.splitlines()

    # ── Step 1: upgrade imports ───────────────────────────────────────────────
    # Remove old-style logging imports; inject the structured logger + uuid/time
    result: List[str] = []
    logger_injected = False
    last_import_idx = -1
    for i, ln in enumerate(lines):
        if ln.startswith("import ") or ln.startswith("from "):
            last_import_idx = i

    for i, ln in enumerate(lines):
        # Drop bare `import logging` and plain getLogger logger lines — we replace them
        if ln.strip() == "import logging":
            continue
        if re.match(r'^logger\s*=\s*logging\.getLogger', ln.strip()):
            continue
        result.append(ln)
        if i == last_import_idx and not logger_injected:
            result.append("import time")
            result.append("import uuid")
            result.append("from demo_repo.infra.logger import get_logger")
            result.append("")
            result.append("logger = get_logger(__name__)")
            logger_injected = True

    if not logger_injected:
        # No imports found — prepend everything
        header = [
            "import time",
            "import uuid",
            "from demo_repo.infra.logger import get_logger",
            "",
            "logger = get_logger(__name__)",
            "",
        ]
        result = header + result

    lines = result

    # ── Step 2: file-specific instrumentation ─────────────────────────────────
    if "churn_scorer" in base:
        lines = _instrument_churn_scorer(lines)
    elif "customer_aggregator" in base:
        lines = _instrument_aggregator(lines)
    elif "risk_classifier" in base:
        lines = _instrument_risk_classifier(lines)
    elif "profile_validator" in base or "validator" in base:
        lines = _instrument_validator(lines)
    else:
        lines = _instrument_generic(lines, base)

    return {filepath: "\n".join(lines) + "\n"}


def _instrument_churn_scorer(lines: List[str]) -> List[str]:
    """Log when a customer's churn score exceeds CHURN_RISK_THRESHOLD."""
    result: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        result.append(line)
        # After `if risk > CHURN_RISK_THRESHOLD:` inject the structured log
        if (
            re.search(r'if\s+\w+\s*[>]=?\s*CHURN_RISK_THRESHOLD', line)
            and line.strip().startswith("if ")
        ):
            indent = len(line) - len(line.lstrip())
            body = " " * (indent + 4)
            id_var = _find_loop_var(lines, i)
            var_match = re.search(r'if\s+(\w+)\s*[>]=?', line)
            score_var = var_match.group(1) if var_match else "risk"
            result.append(
                f'{body}logger.warning("churn_risk_threshold_exceeded", extra={{'
            )
            result.append(f'{body}    "pipeline_run_id": str(uuid.uuid4()),')
            result.append(f'{body}    "customer_id": {id_var},')
            result.append(f'{body}    "churn_score": round({score_var}, 4),')
            result.append(f'{body}    "threshold": CHURN_RISK_THRESHOLD,')
            result.append(f'{body}}})')
        i += 1
    return result


def _instrument_aggregator(lines: List[str]) -> List[str]:
    """Wrap aggregate_profiles with start/end timing and customer count logging."""
    result: List[str] = []
    in_fn = False
    fn_indent = 0
    run_id_injected = False

    for line in lines:
        fn_match = re.match(r'^(\s*)def aggregate_profiles\(', line)
        if fn_match:
            in_fn = True
            fn_indent = len(fn_match.group(1))
            run_id_injected = False
            result.append(line)
            continue

        if in_fn and not run_id_injected:
            # Inject pipeline_run_id + start-time log right after the def line
            # (skip blank lines and docstring first)
            stripped = line.strip()
            if stripped and not stripped.startswith('"""') and not stripped.startswith("'''"):
                body = " " * (fn_indent + 4)
                result.append(f'{body}_run_id = str(uuid.uuid4())')
                result.append(f'{body}_t0 = time.time()')
                result.append(
                    f'{body}logger.info("aggregate_profiles_start", extra={{'
                    f'"pipeline_run_id": _run_id, "profiles": len(profiles)}})'
                )
                run_id_injected = True
                result.append(line)
                continue
            result.append(line)

            # Detect end of function: return or last statement at same indent level
            if stripped.startswith("return") or (
                stripped
                and len(line) - len(line.lstrip()) == fn_indent + 4
                and not stripped.startswith("#")
                and result and result[-1].strip() == ""
            ):
                pass
            continue

        result.append(line)

    # Append end-of-run log before the final blank line
    _inject_aggregator_end(result, fn_indent)
    return result


def _inject_aggregator_end(lines: List[str], fn_indent: int) -> None:
    """Append the completion log after the last line of aggregate_profiles."""
    body = " " * (fn_indent + 4)
    insert_after = -1
    # Scan backwards for the last non-blank, non-comment line at ANY depth
    # inside the function (indent >= fn_indent + 4)
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if stripped and not stripped.startswith("#"):
            indent = len(lines[i]) - len(lines[i].lstrip())
            if indent >= fn_indent + 4:
                insert_after = i
                break
    if insert_after >= 0:
        completion_log = [
            "",
            f'{body}logger.info("aggregate_profiles_complete", extra={{',
            f'{body}    "pipeline_run_id": _run_id,',
            f'{body}    "customers_processed": len(profiles),',
            f'{body}    "duration_ms": int((time.time() - _t0) * 1000),',
            f'{body}}})',
        ]
        for j, ln in enumerate(completion_log):
            lines.insert(insert_after + 1 + j, ln)


def _instrument_risk_classifier(lines: List[str]) -> List[str]:
    """Log the risk distribution summary after apply_risk_classification."""
    result: List[str] = []
    in_fn = False
    fn_indent = 0

    for line in lines:
        fn_match = re.match(r'^(\s*)def apply_risk_classification\(', line)
        if fn_match:
            in_fn = True
            fn_indent = len(fn_match.group(1))
        result.append(line)

        if in_fn and "profile.risk_level = level" in line:
            body = " " * (fn_indent + 4)
            result.append("")
            result.append(f'{body}logger.info("risk_classified", extra={{')
            result.append(f'{body}    "pipeline_run_id": str(uuid.uuid4()),')
            result.append(f'{body}    "customer_id": profile.customer_id,')
            result.append(f'{body}    "risk_level": level,')
            result.append(f'{body}}})')
            in_fn = False

    return result


def _instrument_validator(lines: List[str]) -> List[str]:
    """Log invalid profiles at validation time."""
    result: List[str] = []
    in_fn = False
    fn_indent = 0

    for line in lines:
        fn_match = re.match(r'^(\s*)def validate_profile\(', line)
        if fn_match:
            in_fn = True
            fn_indent = len(fn_match.group(1))
        result.append(line)

        if in_fn and "return ValidationResult" in line and "is_valid" in line:
            body = " " * (fn_indent + 4)
            result.append(f'{body}if errors:')
            result.append(f'{body}    logger.warning("profile_validation_failed", extra={{')
            result.append(f'{body}        "pipeline_run_id": str(uuid.uuid4()),')
            result.append(f'{body}        "customer_id": profile.customer_id,')
            result.append(f'{body}        "errors": errors,')
            result.append(f'{body}    }})')
            in_fn = False

    return result


def _instrument_generic(lines: List[str], base: str) -> List[str]:
    """For files without a specific handler: log entry/exit of the first public function."""
    result: List[str] = []
    injected = False

    for line in lines:
        result.append(line)
        if not injected:
            fn_match = re.match(r'^(\s*)def ([a-z]\w+)\(', line)
            if fn_match:
                indent = fn_match.group(1)
                fn_name = fn_match.group(2)
                body = indent + "    "
                result.append(f'{body}logger.info("{fn_name}_start", extra={{')
                result.append(f'{body}    "pipeline_run_id": str(uuid.uuid4()),')
                result.append(f'{body}}})')
                injected = True

    return result


def _find_loop_var(lines: List[str], from_idx: int) -> str:
    for line in reversed(lines[max(0, from_idx - 6): from_idx]):
        m = re.search(r'for\s+(\w+)\s*(?:,\s*\w+)?\s+in\s+', line)
        if m:
            return m.group(1)
    return "record_id"


# ── Enum migration ────────────────────────────────────────────────────────────

def _apply_enum_migration(
    files: List[str],
    snippets: Dict[str, str],
    repo_path: str,
) -> Dict[str, str]:
    changes: Dict[str, str] = {}
    for filepath in files:
        content = load_file(filepath, snippets, repo_path)
        if not content:
            continue
        if "models/" in filepath and "customer_profile" in filepath:
            changes[filepath] = _migrate_model(content)
        elif "risk_classifier" in filepath:
            changes[filepath] = _migrate_classifier(content)
        elif "validator" in filepath:
            changes[filepath] = _migrate_validator(content)
        else:
            changes[filepath] = content
    return changes


def _migrate_model(content: str) -> str:
    lines = content.splitlines()
    result: List[str] = []

    # Add Enum import after existing imports
    enum_import = "from enum import Enum"
    has_enum = any("from enum import" in ln for ln in lines)

    enum_block = (
        "\n\nclass RiskCategory(str, Enum):\n"
        '    LOW = "low"\n'
        '    MEDIUM = "medium"\n'
        '    HIGH = "high"\n'
        '    UNKNOWN = "unknown"\n'
    )
    enum_inserted = False

    for i, line in enumerate(lines):
        # Add Enum import alongside existing imports
        if not has_enum and line.startswith("from dataclasses"):
            result.append(enum_import)
        result.append(line)
        # Insert enum class after imports, before first class definition
        if not enum_inserted and line.strip() == "" and i > 3:
            next_non_blank = next(
                (lines[j] for j in range(i + 1, len(lines)) if lines[j].strip()), ""
            )
            if next_non_blank.startswith("@dataclass") or next_non_blank.startswith("class "):
                result.append(enum_block)
                enum_inserted = True

    # Replace risk_level field
    final = "\n".join(result)
    final = re.sub(
        r'risk_level:\s*str\s*=\s*["\']unknown["\']',
        'risk_level: RiskCategory = RiskCategory.UNKNOWN',
        final,
    )
    # Update comment
    final = final.replace(
        "# Risk classification — STRING today, should become RiskCategory enum.",
        "# Risk classification — now using RiskCategory enum.",
    )
    final = final.replace(
        '# Allowed values: "low", "medium", "high", "unknown"\n'
        "    # Do NOT add new values here without updating profile_validator.py and\n"
        "    # risk_classifier.py — they each maintain their own copy of this list.",
        "    # Type-safe: RiskCategory enum enforces allowed values at the contract level.",
    )
    return final


def _migrate_classifier(content: str) -> str:
    lines = content.splitlines()
    result: List[str] = []

    for line in lines:
        # Add RiskCategory import alongside existing CustomerProfile import
        if "from demo_repo.models.customer_profile import CustomerProfile" in line:
            result.append(
                "from demo_repo.models.customer_profile import CustomerProfile, RiskCategory"
            )
            continue
        # Remove the duplicated VALID_RISK_LEVELS constant
        if "_VALID_RISK_LEVELS" in line and "=" in line:
            result.append(
                "# Risk levels now enforced by RiskCategory enum — no duplication needed."
            )
            continue
        # Update return type hint
        line = line.replace("def classify_risk(profile: CustomerProfile) -> str:",
                            "def classify_risk(profile: CustomerProfile) -> RiskCategory:")
        # Replace string return values with enum
        line = re.sub(r'return "unknown"', 'return RiskCategory.UNKNOWN', line)
        line = re.sub(r'return "high"',    'return RiskCategory.HIGH',    line)
        line = re.sub(r'return "medium"',  'return RiskCategory.MEDIUM',  line)
        line = re.sub(r'return "low"',     'return RiskCategory.LOW',     line)
        # Replace the defensive guard that used the old constant
        if "_VALID_RISK_LEVELS" in line and "not in" in line:
            result.append(
                "        # No guard needed — RiskCategory enum rejects invalid values at construction."
            )
            continue
        result.append(line)

    return "\n".join(result) + "\n"


def _migrate_validator(content: str) -> str:
    lines = content.splitlines()
    result: List[str] = []

    for line in lines:
        if "from demo_repo.models.customer_profile import CustomerProfile" in line:
            result.append(
                "from demo_repo.models.customer_profile import CustomerProfile, RiskCategory"
            )
            continue
        if "_ALLOWED_RISK_LEVELS" in line and "=" in line and "{" in line:
            result.append(
                "_ALLOWED_RISK_LEVELS = {r.value for r in RiskCategory}"
                "  # Single source of truth — no more duplication"
            )
            continue
        result.append(line)

    # Remove the duplicate comment block
    final = "\n".join(result)
    final = final.replace(
        "# Duplicated from risk_classifier.py — the exact tech debt driving the enum migration.",
        "# Now derived from RiskCategory enum — migration complete.",
    )
    return final + "\n"


# ── Null / missing data handling ──────────────────────────────────────────────

def _apply_null_handling(
    filepath: str,
    snippets: Dict[str, str],
    repo_path: str,
    request: str,
) -> Dict[str, str]:
    content = load_file(filepath, snippets, repo_path)
    if not content:
        return {}

    req = request.lower()
    lines = content.splitlines()
    result: List[str] = []

    for line in lines:
        stripped = line.strip()
        indent = " " * (len(line) - len(line.lstrip()))

        # Handle missing review scores: avg_review_score averaging without null guard
        if "avg_review_score" in line and "+=" in line and "review_score" in line:
            result.append(line)
            continue

        # Add null guard before review score processing
        if (
            "avg_review_score" in line
            and "=" in line
            and "total_reviews" in line
            and "/" in line
        ):
            result.append(
                f"{indent}# Default to 0 when no review data — avoids ZeroDivisionError"
            )
            result.append(
                line.replace(
                    "/ total_reviews",
                    "/ total_reviews if total_reviews > 0 else 0.0",
                )
            )
            continue

        # Add missing review count default
        if "total_reviews" in line and "= 0" in line and "count" in line.lower():
            result.append(line)
            continue

        # Guard: customer with no order history — default aggregated fields to 0
        if "profile.avg_review_score" in line and "=" in line and "None" not in line:
            result.append(f"{indent}# Gracefully handle missing review data")
            result.append(line.replace(
                "profile.avg_review_score",
                "profile.avg_review_score or 0.0",
            ))
            continue

        result.append(line)

    # Also add a summary count of profiles with missing reviews if requested
    if "summary" in req or "count" in req:
        missing_reviews_fn = (
            "\n\ndef count_profiles_missing_reviews(profiles: dict) -> int:\n"
            '    """Return the number of profiles where avg_review_score is None or 0."""\n'
            "    return sum(\n"
            "        1 for p in profiles.values()\n"
            "        if not p.avg_review_score\n"
            "    )\n"
        )
        result.append(missing_reviews_fn)

    return {filepath: "\n".join(result) + "\n"}


# ── Docstring addition ────────────────────────────────────────────────────────

def _apply_docstring(
    filepath: str,
    snippets: Dict[str, str],
    repo_path: str,
    request: str,
) -> Dict[str, str]:
    content = load_file(filepath, snippets, repo_path)
    if not content:
        return {}

    lines = content.splitlines()
    result: List[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        result.append(line)

        # Find function defs without a docstring
        if line.strip().startswith("def ") and line.strip().endswith(":"):
            # Check if next non-empty line is a docstring
            next_idx = i + 1
            while next_idx < len(lines) and not lines[next_idx].strip():
                next_idx += 1
            next_line = lines[next_idx].strip() if next_idx < len(lines) else ""
            if not next_line.startswith('"""') and not next_line.startswith("'''"):
                indent = " " * (len(lines[i + 1]) - len(lines[i + 1].lstrip()) if i + 1 < len(lines) else 4)
                fn_name = re.search(r'def\s+(\w+)', line)
                fn_label = fn_name.group(1).replace("_", " ") if fn_name else "function"
                result.append(f'{indent}"""Execute {fn_label} and return the result."""')
        i += 1

    return {filepath: "\n".join(result) + "\n"}


# ── Print statement ───────────────────────────────────────────────────────────

def _apply_print_statement(
    filepath: str,
    snippets: Dict[str, str],
    repo_path: str,
    request: str,
) -> Dict[str, str]:
    content = load_file(filepath, snippets, repo_path)
    if not content:
        return {}

    target_fn = resolve_target_function(request) or "main"
    lines = content.splitlines()
    result: List[str] = []
    inserted = False
    i = 0

    while i < len(lines):
        line = lines[i]
        result.append(line)

        if not inserted and re.search(rf'\bdef\s+{re.escape(target_fn)}\s*\(', line):
            i += 1
            # Collect any blank lines between def and body
            while i < len(lines) and not lines[i].strip():
                result.append(lines[i])
                i += 1
            if i >= len(lines):
                break
            indent = " " * (len(lines[i]) - len(lines[i].lstrip()))
            # Skip over an existing docstring
            if lines[i].strip().startswith(('"""', "'''")):
                result.append(lines[i])
                closing = lines[i].strip()[:3]
                i += 1
                while i < len(lines):
                    result.append(lines[i])
                    if lines[i].strip().endswith(closing) and i > len(result) - 3:
                        i += 1
                        break
                    i += 1
                if i < len(lines):
                    indent = " " * (len(lines[i]) - len(lines[i].lstrip()))
            result.append(f'{indent}print("Application started — {target_fn} initialised")')
            inserted = True
            continue
        i += 1

    if not inserted:
        # Append at end of file if function not found
        result.append(f'\n# print added at module level (function "{target_fn}" not found)')
        result.append(f'print("Application started")')

    return {filepath: "\n".join(result) + "\n"}


# ── Error handling wrapper ────────────────────────────────────────────────────

def _apply_error_handling(
    filepath: str,
    snippets: Dict[str, str],
    repo_path: str,
    request: str,
) -> Dict[str, str]:
    content = load_file(filepath, snippets, repo_path)
    if not content:
        return {}

    target_fn = resolve_target_function(request)
    lines = content.splitlines()
    result: List[str] = []

    # Ensure logging is imported
    has_logging = any("import logging" in ln for ln in lines)
    has_logger = any("getLogger" in ln for ln in lines)

    i = 0
    if not has_logging:
        # Insert before first import
        while i < len(lines) and not lines[i].strip():
            result.append(lines[i])
            i += 1
        result.append("import logging")
        if not has_logger:
            result.append("logger = logging.getLogger(__name__)")
            result.append("")

    in_target = False
    fn_indent = 0
    body_lines: List[str] = []
    collecting = False

    while i < len(lines):
        line = lines[i]

        if not collecting:
            fn_match = re.match(r'^(\s*)def\s+(\w+)\s*\(', line)
            if fn_match:
                current_fn = fn_match.group(2)
                fn_indent = len(fn_match.group(1))
                in_target = target_fn is None or current_fn == target_fn
                if in_target:
                    result.append(line)
                    i += 1
                    # Collect function header lines (multi-line args, colon)
                    while i < len(lines) and not lines[i-1].strip().endswith(":"):
                        result.append(lines[i])
                        i += 1
                    # Skip docstring
                    if i < len(lines) and lines[i].strip().startswith(('"""', "'''")):
                        result.append(lines[i])
                        closing = lines[i].strip()[:3]
                        i += 1
                        while i < len(lines):
                            result.append(lines[i])
                            stripped = lines[i].strip()
                            i += 1
                            if stripped.endswith(closing) and stripped != closing:
                                break
                            if stripped == closing:
                                break
                    body_lines = []
                    collecting = True
                    continue
            result.append(line)
            i += 1
            continue

        # Collecting body lines until the function ends
        if line.strip() == "" or len(line) - len(line.lstrip()) > fn_indent or not line.strip():
            body_lines.append(line)
            i += 1
            # Check if next non-blank line is at same or lower indent (end of function)
            peek = i
            while peek < len(lines) and not lines[peek].strip():
                peek += 1
            if peek >= len(lines) or (lines[peek].strip() and len(lines[peek]) - len(lines[peek].lstrip()) <= fn_indent and not lines[peek].startswith(" " * (fn_indent + 1))):
                # Flush wrapped body
                _flush_error_wrapped(result, body_lines, fn_indent + 4)
                body_lines = []
                collecting = False
        else:
            body_lines.append(line)
            i += 1

    if body_lines:
        _flush_error_wrapped(result, body_lines, fn_indent + 4)

    return {filepath: "\n".join(result) + "\n"}


def _flush_error_wrapped(result: List[str], body: List[str], base_indent: int) -> None:
    if not any(ln.strip() for ln in body):
        result.extend(body)
        return
    indent = " " * base_indent
    result.append(f"{indent}try:")
    for ln in body:
        result.append("    " + ln if ln.strip() else ln)
    result.append(f"{indent}except Exception as exc:")
    result.append(f"{indent}    logger.error(\"Unexpected error: %s\", exc, exc_info=True)")
    result.append(f"{indent}    raise")


# ── Comment / module docstring at top ────────────────────────────────────────

def _apply_comment_top(
    filepath: str,
    snippets: Dict[str, str],
    repo_path: str,
    request: str,
) -> Dict[str, str]:
    content = load_file(filepath, snippets, repo_path)
    if not content:
        return {}

    lines = content.splitlines()

    # If first non-blank, non-shebang line is already a docstring, replace it
    first_real = next((i for i, ln in enumerate(lines) if ln.strip() and not ln.startswith("#!")), 0)
    module_name = os.path.splitext(os.path.basename(filepath))[0].replace("_", " ")

    # Build a meaningful description from filename and any existing docstring hint
    existing = lines[first_real].strip() if first_real < len(lines) else ""
    if existing.startswith('"""') or existing.startswith("'''"):
        # Already has a module docstring — leave it, just ensure it's descriptive
        return {filepath: content}

    description = (
        f'"""{module_name.capitalize()} — '
        f'{_infer_module_purpose(filepath, content)}\n"""\n'
    )
    result = lines[:first_real] + [description] + lines[first_real:]
    return {filepath: "\n".join(result) + "\n"}


def _infer_module_purpose(filepath: str, content: str) -> str:
    lower = filepath.lower()
    if "aggregator" in lower:
        return "reads source CSVs and builds CustomerProfile objects from raw ecommerce data"
    if "classifier" in lower:
        return "assigns risk levels to customer profiles based on spend and review history"
    if "validator" in lower:
        return "enforces data quality rules before profiles reach downstream consumers"
    if "churn" in lower:
        return "scores customer churn risk and flags accounts above the intervention threshold"
    if "ltv" in lower or "lifetime" in lower:
        return "calculates predicted customer lifetime value from historical spend data"
    if "runner" in lower:
        return "CLI entry point — runs the thin-slice pipeline against a target repository"
    # Extract first comment or significant line as a hint
    for line in content.splitlines()[:10]:
        stripped = line.strip()
        if stripped.startswith("#") and len(stripped) > 15:
            return stripped.lstrip("# ").rstrip(".!?")
    return "part of the customer data product pipeline"


# ── Input validation ──────────────────────────────────────────────────────────

def _apply_input_validation(
    filepath: str,
    snippets: Dict[str, str],
    repo_path: str,
    request: str,
) -> Dict[str, str]:
    content = load_file(filepath, snippets, repo_path)
    if not content:
        return {}

    lines = content.splitlines()
    result: List[str] = []

    for i, line in enumerate(lines):
        result.append(line)
        # Find function definitions that take parameters (API endpoints or processors)
        fn_match = re.match(r'^(\s*)def\s+(\w+)\s*\(([^)]+)\)\s*:', line)
        if not fn_match:
            continue
        indent_str = fn_match.group(1)
        params_raw = fn_match.group(3)
        body_indent = indent_str + "    "

        # Extract parameter names (skip self, cls, *args, **kwargs, type-annotated)
        params = []
        for p in params_raw.split(","):
            p = p.strip().split(":")[0].split("=")[0].strip()
            if p and p not in ("self", "cls") and not p.startswith("*"):
                params.append(p)

        if not params:
            continue

        # Check if next line is already a docstring or validation
        next_line = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if next_line.startswith(('"""', "'''")):
            continue  # docstring present, skip
        if "if not" in next_line or "raise" in next_line or "assert" in next_line:
            continue  # already has guards

        # Add a None guard for the first meaningful param
        p = params[0]
        result.append(f'{body_indent}if {p} is None:')
        result.append(f'{body_indent}    raise ValueError(f"{{repr({p})}} must not be None")')

    return {filepath: "\n".join(result) + "\n"}


# ── Generic fallback ──────────────────────────────────────────────────────────

def _apply_generic(
    files: List[str],
    snippets: Dict[str, str],
    repo_path: str,
) -> Dict[str, str]:
    changes: Dict[str, str] = {}
    for filepath in files:
        content = load_file(filepath, snippets, repo_path)
        if content:
            changes[filepath] = content
        else:
            try:
                with open(os.path.join(repo_path, filepath), encoding="utf-8") as f:
                    changes[filepath] = f.read()
            except Exception:
                changes[filepath] = f"# Generated stub for {filepath}\n"
    return changes
