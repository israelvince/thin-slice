"""Pipeline orchestration service — runs all nodes in sequence."""
from ..models.state import HackathonAppState
from .. import nodes


def run_pipeline(state: HackathonAppState) -> HackathonAppState:
    """Run planner → optimizer → estimator → generator and return final state."""
    state = nodes.planner(state)
    state = nodes.optimizer(state)
    state = nodes.estimator(state)
    state = nodes.generator(state)
    return state
