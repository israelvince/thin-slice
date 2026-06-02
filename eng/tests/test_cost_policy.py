from ai.services.cost_policy import estimate_cost, check_budget


def test_estimate_cost_basic():
    c = estimate_cost(1000, 2000, 'standard')
    assert c > 0


def test_check_budget():
    r = check_budget(1000, 2000, 'standard')
    assert 'projected_cost_usd' in r and 'policy_clearance' in r
