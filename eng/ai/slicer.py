import os
from typing import List

_SKIP_DIRS = {
    ".venv", "venv", ".env", "__pycache__", ".git",
    "node_modules", ".pytest_cache", "dist", "build",
    "eng_backup",
}

_MAX_FILES = 30
_SNIPPET_CHARS = 3000


def slice_repo(target_repo_path: str, keywords: List[str]) -> dict:
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
            "just", "file", "files", "code", "main", "top", "new", "get", "set",
            "make", "run", "use", "using", "used", "comment", "comments",
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
    print(f"Slicer extracted keywords: {keywords}")

    affected = []
    snippets = []
    scored_files = []

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
                score = 0
                
                if keywords:
                    for kw in keywords:
                        if kw in path_lower:
                            score += 2
                        if kw in txt_lower:
                            score += 1
                
                scored_files.append((rel, score, txt, txt_lower))
                print(f"Slicer scoring {rel}: {score}")
                
                if score >= 2:
                    affected.append(rel)
                    snippets.append(f"# FILE: {rel}\n{txt[:_SNIPPET_CHARS]}")
            except Exception:
                continue

    if not affected and scored_files:
        sorted_files = sorted(scored_files, key=lambda x: x[1], reverse=True)
        for rel, score, txt, txt_lower in sorted_files[:2]:
            affected.append(rel)
            snippets.append(f"# FILE: {rel}\n{txt[:_SNIPPET_CHARS]}")
            print(f"Slicer fallback selected: {rel} (score={score})")

    return {
        "affected_files": affected,
        "extracted_slice_context": "\n\n".join(snippets),
    }
