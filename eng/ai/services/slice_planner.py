"""Slice planning service — thin wrapper around the repo slicer."""
from typing import List

from ..slicer import slice_repo


def plan_slice(repo_path: str, keywords: List[str]) -> dict:
    """Return the minimal set of files affected by the given keywords.

    Returns {"affected_files": [...], "extracted_slice_context": "..."}.
    """
    return slice_repo(repo_path, keywords)
