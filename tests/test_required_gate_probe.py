"""Intentional failure used to verify the required pull-request gate."""


def test_required_gate_blocks_a_failing_pull_request():
    assert False, "TRAN-50 intentional required-gate probe"
