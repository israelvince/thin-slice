import os

from ai.langgraph_runner import run_with_langgraph, LANGGRAPH_AVAILABLE
from ai.models.state import HackathonAppState


def test_run_with_langgraph_returns_state():
    # Prepare a minimal state
    state = HackathonAppState(user_request="Add a comment", target_repo=".")

    if not LANGGRAPH_AVAILABLE:
        # If LangGraph isn't installed in CI, skip this assertion path.
        assert True
        return

    result = run_with_langgraph(state)

    # The result should be a HackathonAppState (or convertible to it)
    assert result is not None
    assert isinstance(result, HackathonAppState)

    # Key fields should be present/populated (policy_clearance may be bool)
    assert hasattr(result, "policy_clearance")
    assert hasattr(result, "projected_token_cost_usd")
    assert hasattr(result, "generated_code_blocks")
