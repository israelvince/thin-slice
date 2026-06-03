# Continuous Cost Monitor — one‑page design

Goal
- Watch LLM streaming generation, track token usage, and abort on anomalies (loops, spikes).

Responsibilities
- Subscribe to streaming hooks (provider SDK) or instrument the generation loop.
- Track cumulative tokens and per‑chunk size.
- Trigger an emergency cutoff and set state to aborted if thresholds exceeded.
