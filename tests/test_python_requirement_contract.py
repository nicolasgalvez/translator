"""Repository contract for the supported Python version."""

import re
import tomllib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_MINIMUM = (3, 11)


class PythonRequirementContractTests(unittest.TestCase):
    """Keep package metadata, setup docs, and Python CI on one minimum."""

    def test_python_minimum_is_3_11_everywhere(self):
        requirements = {
            "pyproject.toml": self._pyproject_minimum(),
            "README.md": self._readme_minimum(),
        }
        requirements.update(self._workflow_versions())

        self.assertEqual(self._wrong_versions(requirements), {})

    def test_new_setup_python_workflow_is_included_in_contract(self):
        with TemporaryDirectory() as temporary_directory:
            repository_root = Path(temporary_directory)
            workflow_directory = repository_root / ".github" / "workflows"
            workflow_directory.mkdir(parents=True)
            for name, version in (
                ("pylint.yml", "3.11"),
                ("pytest.yml", "3.11"),
                ("future.yaml", "3.10"),
            ):
                (workflow_directory / name).write_text(
                    "uses: actions/setup-python@v7\n"
                    f'python-version: "{version}"\n',
                    encoding="utf-8",
                )

            with patch(f"{__name__}.REPOSITORY_ROOT", repository_root):
                versions = self._workflow_versions()

        future_workflow = ".github/workflows/future.yaml"
        self.assertIn(future_workflow, versions)
        self.assertEqual(
            self._wrong_versions(versions),
            {future_workflow: (3, 10)},
        )

    @staticmethod
    def _wrong_versions(requirements):
        return {
            name: version
            for name, version in requirements.items()
            if version != EXPECTED_MINIMUM
        }

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

        workflows = sorted(
            (*workflow_directory.glob("*.yml"), *workflow_directory.glob("*.yaml"))
        )
        for workflow in workflows:
            content = workflow.read_text(encoding="utf-8")
            if "actions/setup-python@" not in content:
                continue

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

        self.assertTrue(versions, "No maintained Python CI workflows were found")
        return versions


if __name__ == "__main__":
    unittest.main()
