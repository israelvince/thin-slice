import importlib
import os

from ai.models.state import HackathonAppState


def test_toolnode_mode(monkeypatch, tmp_path):
    # Fake ToolNode that accepts tools and executes them synchronously
    class FakeToolNode:
        def __init__(self, tools, name=None):
            self.tools = tools

        def invoke(self, payload, config=None):
            # payload is list of single tool_call dicts
            # Find the tool by name and run it with the provided state arg
            for call in payload:
                name = call.get("name")
                args = call.get("args", {})
                for t in self.tools:
                    if t.__name__ == name:
                        return t(args.get("state"))
            return None

    lr = importlib.import_module("ai.langgraph_runner")
    monkeypatch.setenv("THINSLICE_LANGGRAPH_MODE", "toolnode")
    monkeypatch.setattr(lr, "ToolNode", FakeToolNode, raising=False)

    s = HackathonAppState(user_request="y", target_repo=str(tmp_path))
    res = lr.run_with_langgraph(s)

    assert isinstance(res, HackathonAppState)
    assert hasattr(res, "projected_token_cost_usd")
