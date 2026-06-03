# Code Generator — one‑page design

Goal
- Given a small code slice and a clear spec, produce code patches and matching unit tests, and prepare a PR payload.

Responsibilities
- Receive `extracted_slice_context` and `user_request`.
- Run LLM generation (or use a mocked response for Phase 1).
- Produce file diffs/patches and a summary changelog for the PR body.

Tools
- LLM SDKs (OpenAI/Anthropic) or mocked responses for Phase 1.
- Use `git` to create a branch, apply patches, and open a PR via GitHub API.
