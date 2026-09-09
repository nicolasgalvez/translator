"""Behavior tests for the stable pull-request result gate."""

from scripts.ci_result_gate import CiResultGate


def test_gate_accepts_successful_required_and_skipped_optional_suites():
    gate = CiResultGate()

    assert not gate.failures(
        classification_result="success",
        requirements={"python": True, "docker": False},
        results={"python": "success", "docker": "skipped"},
    )


def test_gate_rejects_a_failed_required_suite():
    gate = CiResultGate()

    assert gate.failures(
        classification_result="success",
        requirements={"python": True},
        results={"python": "failure"},
    ) == ["python: expected success, got failure"]


def test_gate_rejects_an_inapplicable_suite_that_ran():
    gate = CiResultGate()

    assert gate.failures(
        classification_result="success",
        requirements={"frontend": False},
        results={"frontend": "success"},
    ) == ["frontend: expected skipped, got success"]


def test_gate_rejects_failed_classification_even_when_suites_skipped():
    gate = CiResultGate()

    assert gate.failures(
        classification_result="failure",
        requirements={"python": False},
        results={"python": "skipped"},
    ) == ["Path classification did not succeed: failure"]
