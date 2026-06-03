# Slice Planner — one‑page design

Goal
- Given a user's change request and a target repository, identify the minimal set of files and the smallest contiguous code context required to implement the change.

Responsibilities
- Parse the user request (NLP hinting) to identify target symbols or keywords.
- Inspect a repo tree and optionally an AST or call graph to identify dependent files.
- Return `affected_files` and `extracted_slice_context` (string) suitable for an LLM prompt.

Tools
- Start with filename heuristics + simple AST parsing (Python `ast`) for prototypes.
- For deeper analysis consider `rope`, `jedi`, or language‑specific parsers.

Success criteria
- Planner returns a slice < 50KB for targeted changes in the demo repo.
