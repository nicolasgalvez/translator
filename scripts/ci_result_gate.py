"""Validate that every pull-request suite has the result its policy requires."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping


class CiResultGate:  # pylint: disable=too-few-public-methods
    """Evaluate suite results independently of GitHub Actions expressions."""

    @staticmethod
    def failures(
        classification_result: str,
        requirements: Mapping[str, bool],
        results: Mapping[str, str],
    ) -> list[str]:
        """Return an explanation for every result that violates the policy."""
        if classification_result != "success":
            return [
                "Path classification did not succeed: "
                f"{classification_result}"
            ]

        failures = []
        for suite, required in requirements.items():
            actual = results[suite]
            expected = "success" if required else "skipped"
            if actual != expected:
                failures.append(
                    f"{suite}: expected {expected}, got {actual}"
                )
        return failures


SUITE_ENVIRONMENT = (
    ("python-test", "PYTHON_REQUIRED", "PYTHON_TEST_RESULT"),
    ("python-lint", "PYTHON_REQUIRED", "PYTHON_LINT_RESULT"),
    ("frontend", "FRONTEND_REQUIRED", "FRONTEND_RESULT"),
    ("docker", "DOCKER_REQUIRED", "DOCKER_RESULT"),
    ("workflow-lint", "WORKFLOW_LINT_REQUIRED", "WORKFLOW_LINT_RESULT"),
    (
        "python-requirement",
        "PYTHON_REQUIREMENT_REQUIRED",
        "PYTHON_REQUIREMENT_RESULT",
    ),
    (
        "dependency-audit",
        "DEPENDENCY_AUDIT_REQUIRED",
        "DEPENDENCY_AUDIT_RESULT",
    ),
)


def main() -> None:
    """Read GitHub job state from the environment and enforce the gate."""
    requirements = {
        suite: os.environ[required_name] == "true"
        for suite, required_name, _ in SUITE_ENVIRONMENT
    }
    results = {
        suite: os.environ[result_name]
        for suite, _, result_name in SUITE_ENVIRONMENT
    }
    failures = CiResultGate().failures(
        classification_result=os.environ["CLASSIFY_RESULT"],
        requirements=requirements,
        results=results,
    )
    if failures:
        print("\n".join(failures), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
