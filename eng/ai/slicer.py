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
    affected = []
    snippets = []

    for root, dirs, files in os.walk(target_repo_path):
        # Prune dirs in-place to skip vendor/cache trees entirely
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
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    txt = fh.read()
                if any(k.lower() in txt.lower() for k in keywords):
                    rel = os.path.relpath(path, target_repo_path)
                    affected.append(rel)
                    snippets.append(f"# FILE: {rel}\n{txt[:_SNIPPET_CHARS]}")
            except Exception:
                continue

    return {
        "affected_files": affected,
        "extracted_slice_context": "\n\n".join(snippets),
    }
