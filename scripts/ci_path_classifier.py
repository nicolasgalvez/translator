"""Classify changed paths into the CI suites required for a pull request."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable


class CiPathClassifier:  # pylint: disable=too-few-public-methods
    """Own the repository path policy for the required pull-request gate."""

    SUITES = (
        "python",
        "frontend",
        "docker",
        "workflow_lint",
        "python_requirement",
        "dependency_audit",
    )
    PYTHON_CONTRACT_INPUTS = frozenset(
        {
            ".dockerignore",
            ".pylintrc",
            "Dockerfile",
            "README.md",
            "docker-compose.yml",
            "docs/uv-evaluation.md",
            "pyproject.toml",
            "run.sh",
            "scripts/smoke-docker-runtime.sh",
            "tests/docker-context.Dockerfile",
            "uv.lock",
        }
    )
    PYTHON_CONTRACT_PREFIXES = (
        ".github/workflows/",
        "config/",
        "dependency-audit/",
        "templates/",
        "tests/fixtures/",
    )

    def classify(self, paths: Iterable[str]) -> dict[str, bool]:
        """Return the suites required by the union of the supplied paths."""
        result = {suite: False for suite in self.SUITES}
        for path in paths:
            for suite in self._suites_for(path):
                result[suite] = True
        return result

    @classmethod
    def _suites_for(cls, path: str) -> set[str]:
        suites = set()

        if (
            path.endswith(".py")
            or path in cls.PYTHON_CONTRACT_INPUTS
            or path.startswith(cls.PYTHON_CONTRACT_PREFIXES)
        ):
            suites.add("python")

        if (
            path.startswith("frontend/")
            or path
            in {
                "Dockerfile",
                "README.md",
                "run.sh",
                "templates/captions.html",
            }
            or path == ".github/workflows/frontend.yml"
        ):
            suites.add("frontend")

        if (
            path
            in {
                ".dockerignore",
                ".gitignore",
                "Dockerfile",
                "docker-compose.yml",
                "pyproject.toml",
                "run.sh",
                "uv.lock",
            }
            or path.endswith(".py") and "/" not in path
            or path.startswith(
                (
                    "frontend/",
                    "plugins/",
                    "templates/",
                    "transcription/",
                )
            )
            or path
            in {
                "scripts/check-docker-context.sh",
                "scripts/smoke-docker-runtime.sh",
                "tests/docker-context.Dockerfile",
                "tests/docker_smoke_app.py",
                "tests/test_docker_runtime_contract.py",
                ".github/workflows/docker.yml",
            }
        ):
            suites.add("docker")

        if path.startswith(".github/workflows/") and path.endswith(
            (".yml", ".yaml")
        ):
            suites.update(
                ("python", "workflow_lint", "python_requirement")
            )

        if path in {
            "README.md",
            "pyproject.toml",
            "tests/test_python_requirement_contract.py",
            ".github/workflows/python-requirement.yml",
        }:
            suites.add("python_requirement")

        if (
            path
            in {
                "README.md",
                "pyproject.toml",
                "uv.lock",
                "scripts/dependency_audit.py",
                "tests/test_argos_stanza_compatibility.py",
                "tests/test_dependency_audit.py",
                "tests/test_dependency_security_contract.py",
                ".github/workflows/dependency-audit.yml",
            }
            or path.startswith("dependency-audit/")
        ):
            suites.add("dependency_audit")

        return suites


def _read_nul_delimited_paths() -> list[str]:
    return [
        os.fsdecode(path)
        for path in sys.stdin.buffer.read().split(b"\0")
        if path
    ]


def main() -> None:
    """Write GitHub-output-compatible booleans in a stable order."""
    classifier = CiPathClassifier()
    suites = classifier.classify(_read_nul_delimited_paths())
    for suite in classifier.SUITES:
        print(f"{suite}={'true' if suites[suite] else 'false'}")


if __name__ == "__main__":
    main()
