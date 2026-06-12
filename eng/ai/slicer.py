import logging
import os
import re as _re
from typing import List

logger = logging.getLogger("thin_slice.slicer")

KNOWN_ENTITIES = {
    "profile builder":       "profile_builder.py",
    "export service":        "export_service.py",
    "data quality reporter": "data_quality_reporter.py",
    "customer aggregator":   "customer_aggregator.py",
    "payment aggregator":    "payment_aggregator.py",
    "review aggregator":     "review_aggregator.py",
    "ltv calculator":        "ltv_calculator.py",
    "churn scorer":          "churn_scorer.py",
    "risk classifier":       "risk_classifier.py",
    "segment classifier":    "segment_classifier.py",
    "profile validator":     "profile_validator.py",
    "payment validator":     "payment_validator.py",
    "order validator":       "order_validator.py",
    "review validator":      "review_validator.py",
    "config":                "config.py",
    "logger":                "logger.py",
    "scheduler":             "scheduler.py",
    "storage connector":     "storage_connector.py",
    "customer profile":      "customer_profile.py",
    "data quality rules":    "data_quality_rules.py",
}


def extract_mentioned_files(user_request: str, repo_path: str) -> list:
    """Find files explicitly named in the request by filename or known entity name."""
    request_lower = user_request.lower()
    py_files = set(_re.findall(r'\b([\w]+\.py)\b', request_lower))
    entity_matches = {
        filename for entity, filename in KNOWN_ENTITIES.items()
        if entity in request_lower
    }
    all_targets = py_files | entity_matches
    if not all_targets:
        return []
    found = []
    for root, dirs, files in os.walk(repo_path):
        for f in files:
            if f.lower() in all_targets:
                found.append(os.path.relpath(os.path.join(root, f), repo_path))
    return found


def extract_constant_referenced_files(user_request: str, repo_path: str) -> list:
    """Find files defining ALL_CAPS constants mentioned in the request."""
    import re as _re
    constants = set(_re.findall(r'\b[A-Z][A-Z0-9_]{3,}\b', user_request))
    if not constants:
        return []

    found = []
    for root, dirs, files in os.walk(repo_path):
        if '.git' in root or '__pycache__' in root:
            continue
        for f in files:
            if not f.endswith('.py'):
                continue
            filepath = os.path.join(root, f)
            try:
                with open(filepath, encoding='utf-8') as fh:
                    content = fh.read()
                for const in constants:
                    if _re.search(rf'^{const}\s*[:=]', content, _re.MULTILINE):
                        rel = os.path.relpath(filepath, repo_path)
                        found.append(rel)
                        break
            except Exception:
                continue
    return found


_BROAD_SCOPE_SIGNALS = {
    "entire codebase", "all files", "every model",
    "every file", "every service", "every pipeline",
}


def extract_field_referenced_files(user_request: str, repo_path: str) -> list:
    """Find files that contain any snake_case field names mentioned in the request.

    Used as a fallback when extract_mentioned_files returns nothing — handles
    broad requests like 'update all files using total_spend_brl and ltv_brl'.
    """
    import re as _re

    # Extract snake_case identifiers from the request (likely field/variable names)
    field_candidates = set(_re.findall(r'\b[a-z][a-z0-9]*(?:_[a-z0-9]+){1,}\b', user_request.lower()))

    # Filter out common English words that happen to have underscores in this context
    if not field_candidates:
        return []

    found = []
    for root, dirs, files in os.walk(repo_path):
        if '.git' in root or '__pycache__' in root:
            continue
        for f in files:
            if not f.endswith('.py'):
                continue
            filepath = os.path.join(root, f)
            try:
                with open(filepath, encoding='utf-8') as fh:
                    content = fh.read().lower()
                if any(field in content for field in field_candidates):
                    rel = os.path.relpath(filepath, repo_path)
                    found.append(rel)
            except Exception:
                continue

    if any(sig in user_request.lower() for sig in _BROAD_SCOPE_SIGNALS) and len(found) > 25:
        logger.warning(
            "Broad-scope request matched %d files via field search; capping at 25", len(found)
        )
        found = sorted(found)[:25]

    return found


_SKIP_DIRS = {
    ".venv", "venv", ".env", "__pycache__", ".git",
    "node_modules", ".pytest_cache", "dist", "build",
    "eng_backup",
}

_MAX_FILES = 30
_SNIPPET_CHARS = 3000


def slice_repo(target_repo_path: str, keywords: List[str], user_request: str = "") -> dict:
    """Walk the repo and return files that contain any keyword.

    Skips vendor/cache directories and caps results to avoid token explosion.
    Returns {"affected_files": [...], "extracted_slice_context": "..."}.
    """
    def normalize_keywords(raw_keywords: List[str]) -> List[str]:
        pythonSTOPWORDS = {
            "add", "a", "an", "the", "to", "of", "and", "in", "for", "on", "at",
            "is", "it", "its", "this", "that", "with", "from", "by", "as", "be",
            "are", "was", "were", "been", "have", "has", "had", "do", "does", "did",
            "will", "would", "could", "should", "may", "might", "shall", "can",
            "what", "which", "who", "how", "when", "where", "why",
            "all", "each", "every", "some", "any", "no", "not", "but", "or", "if",
            "then", "so", "up", "out", "about", "into", "than", "more", "also",
            "just", "file", "files", "code", "top", "new", "get", "set",
            "make", "run", "use", "using", "used",
            "explaining", "explain", "existing", "current", "update", "change",
            "changes", "create", "remove", "delete", "fix", "implement",
        }
        normalized = []
        for kw in raw_keywords:
            clean = "".join(ch for ch in kw.lower() if ch.isalnum() or ch == "_").strip()
            if not clean or clean in pythonSTOPWORDS or len(clean) < 4:
                continue
            if clean not in normalized:
                normalized.append(clean)
        return normalized

    keywords = normalize_keywords(keywords)
    logger.debug("Slicer keywords: %s", keywords)

    affected = []
    snippets = []
    scored_files = []

    # Pre-seed files from all three matchers combined (bypass keyword threshold).
    entity_paths: set = set()
    if user_request:
        mentioned = set(extract_mentioned_files(user_request, target_repo_path))
        mentioned |= set(extract_constant_referenced_files(user_request, target_repo_path))
        mentioned |= set(extract_field_referenced_files(user_request, target_repo_path))
        for rel in sorted(mentioned):
            if rel in entity_paths:
                continue
            entity_paths.add(rel)
            try:
                with open(os.path.join(target_repo_path, rel), "r", encoding="utf-8") as fh:
                    txt = fh.read()
                affected.append(rel)
                snippets.append(f"# FILE: {rel}\n{txt[:_SNIPPET_CHARS]}")
                scored_files.append((rel, 10, txt))
                logger.debug("Entity pre-seeded: %s", rel)
            except Exception:
                continue

    for root, dirs, files in os.walk(target_repo_path):
        dirs[:] = [
            d for d in dirs
            if d not in _SKIP_DIRS and not d.startswith(".")
        ]

        for f in files:
            if len(affected) >= _MAX_FILES:
                break
            if not f.endswith((".py", ".md", ".txt", ".cs")):
                continue
            path = os.path.join(root, f)
            rel = os.path.relpath(path, target_repo_path)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    txt = fh.read()
                txt_lower = txt.lower()
                path_lower = rel.lower()
                score = sum(
                    (2 if kw in path_lower else 0) + (1 if kw in txt_lower else 0)
                    for kw in keywords
                )
                scored_files.append((rel, score, txt))
                logger.debug("Scored %s: %d", rel, score)
                if score >= 4:
                    affected.append(rel)
                    snippets.append(f"# FILE: {rel}\n{txt[:_SNIPPET_CHARS]}")
            except Exception:
                continue

    if not affected and scored_files:
        rel, score, txt = max(scored_files, key=lambda x: x[1])
        affected.append(rel)
        snippets.append(f"# FILE: {rel}\n{txt[:_SNIPPET_CHARS]}")
        logger.debug("Fallback selected: %s (score=%d)", rel, score)

    return {
        "affected_files": affected,
        "extracted_slice_context": "\n\n".join(snippets),
    }
