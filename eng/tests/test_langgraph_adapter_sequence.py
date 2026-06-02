import importlib
import os

from ai.models.state import HackathonAppState


def test_sequence_mode(monkeypatch, tmp_path):
    # Arrange: create a fake RunnableSeq that calls the functions in order
    class FakeRunnableSeq:
        def __init__(self, *steps, name=None):
            self.steps = steps

        def invoke(self, initial_state):
            state = initial_state
            for s in self.steps:
                state = s(state)
            return state

    # Patch the internal import used by langgraph_runner
    lr = importlib.import_module("ai.langgraph_runner")
    monkeypatch.setenv("THINSLICE_LANGGRAPH_MODE", "sequence")
    monkeypatch.setattr(lr, "RunnableSeq", FakeRunnableSeq, raising=False)

    # Act
    s = HackathonAppState(user_request="x", target_repo=str(tmp_path))
    res = lr.run_with_langgraph(s)

    # Assert
    assert isinstance(res, HackathonAppState)
    assert hasattr(res, "generated_code_blocks")
