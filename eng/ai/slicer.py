import os
from typing import List

def slice_repo(target_repo_path: str, keywords: List[str]) -> dict:
    """Simple slicer: walk the target repo path and return files that contain any keyword.

    Returns a dict with affected_files and extracted_slice_context (concatenated snippets).
    """
    affected = []
    snippets = []
    for root, _, files in os.walk(target_repo_path):
        for f in files:
            if not f.endswith(('.py', '.md', '.txt')):
                continue
            path = os.path.join(root, f)
            try:
                with open(path, 'r', encoding='utf-8') as fh:
                    txt = fh.read()
                if any(k in txt for k in keywords):
                    affected.append(os.path.relpath(path, target_repo_path))
                    snippets.append(f"# FILE: {f}\n" + txt[:4000])
            except Exception:
                continue
    return {"affected_files": affected, "extracted_slice_context": "\n\n".join(snippets)}
