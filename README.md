# Thin-Slice

A Slack-native AI pipeline that intercepts code change requests, assesses shipping risk using DORA and Shape Up principles, and breaks large changes into safe, independently verifiable slices before generating code.

Built for the AI/works hackathon — June 2026.

---

All runnable code, tests, and documentation live in [`eng/`](eng/README.md).

```bash
cd eng
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest -q                          # 49 tests, all passing
python runner.py --request "migrate risk_level to RiskCategory enum" --repo ./demo_repo
```

Full documentation: [eng/README.md](eng/README.md)
