import logging
import os
from typing import List

logger = logging.getLogger("thin_slice.slicer")

_SKIP_DIRS = {
    ".venv", "venv", ".env", "__pycache__", ".git",
    "node_modules", ".pytest_cache", "dist", "build",
    "eng_backup",
}

_STOP_WORDS = {
    "add", "a", "an", "the", "to", "of", "and", "in", "for", "on", "at",
    "is", "it", "its", "this", "that", "with", "from", "by", "as", "be",
    "are", "was", "were", "been", "have", "has", "had", "do", "does", "did",
    "will", "would", "could", "should", "may", "might", "shall", "can",
    "what", "which", "who", "how", "when", "where", "why",
    "all", "each", "every", "some", "any", "no", "not", "but", "or", "if",
    "then", "so", "up", "out", "about", "into", "than", "more", "also",
    "just", "file", "files", "code", "main", "top", "new", "get", "set",
    "make", "run", "use", "using", "used", "comment", "comments",
    "explaining", "explain", "existing", "current", "update", "change",
    "changes", "create", "remove", "delete", "fix", "implement",
}

_MAX_FILES = 30
_SNIPPET_CHARS = 3000


def _normalize_keywords(raw: List[str]) -> List[str]:
    seen = []
    for kw in raw:
        clean = "".join(ch for ch in kw.lower() if ch.isalnum() or ch == "_").strip()
        if clean and clean not in _STOP_WORDS and len(clean) >= 4 and clean not in seen:
            seen.append(clean)
    return seen


def slice_repo(target_repo_path: str, keywords: List[str]) -> dict:
    """Walk the repo and return files that contain any keyword.

    Skips vendor/cache directories, caps at 30 files, uses a scoring
    system so files whose *path* matches score higher than text-only matches.
    Returns {"affected_files": [...], "extracted_slice_context": "..."}.
    """
    keywords = _normalize_keywords(keywords)
    logger.debug("Slicer keywords: %s", keywords)

    affected: List[str] = []
    snippets: List[str] = []
    scored: List[tuple] = []

    for root, dirs, files in os.walk(target_repo_path):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]

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
                scored.append((rel, score, txt))
                logger.debug("Scored %s: %d", rel, score)
                if score >= 2:
                    affected.append(rel)
                    snippets.append(f"# FILE: {rel}\n{txt[:_SNIPPET_CHARS]}")
            except Exception:
                continue

    # Fallback: include top-2 highest-scored files even if score < 2
    if not affected and scored:
        for rel, score, txt in sorted(scored, key=lambda x: -x[1])[:2]:
            affected.append(rel)
            snippets.append(f"# FILE: {rel}\n{txt[:_SNIPPET_CHARS]}")
            logger.debug("Fallback selected: %s (score=%d)", rel, score)

    return {
        "affected_files": affected,
        "extracted_slice_context": "\n\n".join(snippets),
    }
