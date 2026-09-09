"""Repository contract for the supported Python version."""

import re
import tomllib
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_MINIMUM = (3, 11)
PYTHON_WORKFLOWS = ("pylint.yml", "pytest.yml")


class PythonRequirementContractTests(unittest.TestCase):
    """Keep package metadata, setup docs, and Python CI on one minimum."""

    def test_python_minimum_is_3_11_everywhere(self):
        requirements = {
            "pyproject.toml": self._pyproject_minimum(),
            "README.md": self._readme_minimum(),
        }
        requirements.update(self._workflow_versions())

        self.assertEqual(
            requirements,
            {name: EXPECTED_MINIMUM for name in requirements},
        )

    def _pyproject_minimum(self):
        with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as pyproject_file:
            requires_python = tomllib.load(pyproject_file)["project"][
                "requires-python"
            ]

        match = re.fullmatch(r">=(\d+)\.(\d+)", requires_python)
        self.assertIsNotNone(
            match,
            "pyproject.toml must declare a single inclusive minimum Python version",
        )
        return tuple(map(int, match.groups()))

    def _readme_minimum(self):
        readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
        match = re.search(r"^### Python (\d+)\.(\d+)\+$", readme, re.MULTILINE)
        self.assertIsNotNone(
            match,
            "README.md must document the minimum as a 'Python X.Y+' prerequisite",
        )
        return tuple(map(int, match.groups()))

    def _workflow_versions(self):
        workflow_directory = REPOSITORY_ROOT / ".github" / "workflows"
        versions = {}

        for workflow_name in PYTHON_WORKFLOWS:
            workflow = workflow_directory / workflow_name
            content = workflow.read_text(encoding="utf-8")
            self.assertIn(
                "actions/setup-python@",
                content,
                f"{workflow.relative_to(REPOSITORY_ROOT)} must set up Python",
            )

            matches = re.findall(
                r'^\s*python-version:\s*["\']?(\d+)\.(\d+)["\']?\s*$',
                content,
                re.MULTILINE,
            )
            self.assertEqual(
                len(matches),
                1,
                f"{workflow.relative_to(REPOSITORY_ROOT)} must pin one Python version",
            )
            versions[str(workflow.relative_to(REPOSITORY_ROOT))] = tuple(
                map(int, matches[0])
            )

        return versions


if __name__ == "__main__":
    unittest.main()
