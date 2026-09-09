"""Intentional failure used to verify the required pull-request gate."""


def test_required_gate_blocks_a_failing_pull_request():
    assert True, "TRAN-50 repaired required-gate probe"
