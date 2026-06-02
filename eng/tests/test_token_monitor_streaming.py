import ai.nodes as nodes_mod
from ai.services.token_monitor import BudgetExceeded, TokenBudgetTracker
from eng import runner


def _fake_generator_raises(state):
    # Simulate a mid-generation budget exceeded event
    raise BudgetExceeded("simulated budget exceeded in generator")


def test_runner_handles_budget_exceeded_and_attempts_mitigation(monkeypatch):
    # Patch the generator to raise BudgetExceeded during generation
    monkeypatch.setattr(nodes_mod, "generator", _fake_generator_raises)

    # Ensure the TokenBudgetTracker.switch_to_cheaper returns True to exercise retry branch
    monkeypatch.setattr(TokenBudgetTracker, "switch_to_cheaper", lambda self: True)

    # Run the mock pipeline; runner.run_mock returns a dict (final_state.dict())
    res = runner.run_mock("Generate CLTV per customer from ecommerce orders", ".")

    # The runner should mark policy_clearance False and surface a recommendation note
    assert res.get("policy_clearance") is False
    notes = res.get("recommendation_notes") or ""
    assert "Budget exceeded" in notes or "budget" in notes.lower()
