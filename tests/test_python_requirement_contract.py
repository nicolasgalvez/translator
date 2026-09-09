"""Repository contract for the supported Python version."""

import re
import tomllib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_MINIMUM = (3, 11)
VERSION_KEY = r'''(?:python-version|"python-version"|'python-version')'''
VERSION_DECLARATION = re.compile(rf"^\s*{VERSION_KEY}\s*:")
LITERAL_VERSION_DECLARATION = re.compile(
    rf'''^\s*{VERSION_KEY}\s*:\s*(?:"(\d+\.\d+)"|'(\d+\.\d+)'|(\d+\.\d+))'''
    r"(?:\s+#.*)?\s*$"
)


class PythonRequirementContractTests(unittest.TestCase):
    """Keep package metadata, setup docs, and Python CI on one minimum."""

    def test_python_minimum_is_3_11_everywhere(self):
        requirements = {
            "pyproject.toml": self._pyproject_minimum(),
            "README.md": self._readme_minimum(),
        }
        requirements.update(self._workflow_versions())

        self.assertEqual(self._wrong_versions(requirements), {})

    def test_contract_workflow_covers_every_requirement_source(self):
        workflow = (
            REPOSITORY_ROOT / ".github" / "workflows" / "python-requirement.yml"
        )
        self.assertTrue(workflow.is_file(), "Python requirement workflow is missing")
        content = workflow.read_text(encoding="utf-8")

        for fragment in (
            "  pull_request:",
            "  push:",
            "    branches: [main]",
            '      - "README.md"',
            '      - "pyproject.toml"',
            '      - "tests/test_python_requirement_contract.py"',
            '      - ".github/workflows/*.yml"',
            '      - ".github/workflows/*.yaml"',
            "python -m unittest tests/test_python_requirement_contract.py",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, content)

    def test_new_setup_python_workflow_is_included_in_contract(self):
        versions = self._temporary_workflow_versions(
            {
                "pylint.yml": 'python-version: "3.11"\n',
                "pytest.yml": 'python-version: "3.11"\n',
                "future.yaml": 'python-version: "3.10"\n',
            }
        )

        future_workflow = ".github/workflows/future.yaml"
        self.assertEqual(
            self._wrong_versions(versions),
            {f"{future_workflow}:2": (3, 10)},
        )

    def test_inline_commented_version_cannot_be_skipped(self):
        versions = self._temporary_workflow_versions(
            {
                "future.yml": (
                    'python-version: "3.11"\n'
                    'python-version: "3.10" # legacy\n'
                )
            }
        )

        self.assertIn((3, 10), versions.values())

    def test_dynamic_version_declaration_is_rejected(self):
        with self.assertRaisesRegex(
            AssertionError,
            "literal major.minor version",
        ):
            self._temporary_workflow_versions(
                {
                    "future.yml": (
                        'python-version: "3.11"\n'
                        "python-version: ${{ matrix.python }}\n"
                    )
                }
            )

    def test_quoted_keys_cannot_hide_old_version(self):
        for key in ('"python-version"', "'python-version'"):
            with self.subTest(key=key):
                versions = self._temporary_workflow_versions(
                    {
                        "future.yml": (
                            'python-version: "3.11"\n'
                            f'{key}: "3.10" # legacy\n'
                        )
                    }
                )
                self.assertIn((3, 10), versions.values())

    def test_malformed_quoted_key_declaration_is_rejected(self):
        for key in ('"python-version"', "'python-version'"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(
                    AssertionError,
                    "literal major.minor version",
                ):
                    self._temporary_workflow_versions(
                        {
                            "future.yml": (
                                'python-version: "3.11"\n'
                                f"{key}: " + "${{ matrix.python }}\n"
                            )
                        }
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

    def _temporary_workflow_versions(self, workflows):
        with TemporaryDirectory() as temporary_directory:
            repository_root = Path(temporary_directory)
            workflow_directory = repository_root / ".github" / "workflows"
            workflow_directory.mkdir(parents=True)
            for name, declarations in workflows.items():
                (workflow_directory / name).write_text(
                    "uses: actions/setup-python@v7\n" + declarations,
                    encoding="utf-8",
                )
            return self._workflow_versions(repository_root)

    def _workflow_versions(self, repository_root=REPOSITORY_ROOT):
        workflow_directory = repository_root / ".github" / "workflows"
        versions = {}

        workflows = sorted(
            (*workflow_directory.glob("*.yml"), *workflow_directory.glob("*.yaml"))
        )
        for workflow in workflows:
            content = workflow.read_text(encoding="utf-8")
            if "actions/setup-python@" not in content:
                continue

            relative_workflow = workflow.relative_to(repository_root)
            declarations = []
            for line_number, line in enumerate(content.splitlines(), start=1):
                if not VERSION_DECLARATION.match(line):
                    continue

                match = LITERAL_VERSION_DECLARATION.fullmatch(line)
                self.assertIsNotNone(
                    match,
                    f"{relative_workflow}:{line_number} must use a literal "
                    "major.minor version",
                )
                version = next(part for part in match.groups() if part is not None)
                declarations.append(
                    (line_number, tuple(map(int, version.split("."))))
                )

            self.assertTrue(
                declarations,
                f"{relative_workflow} must declare at least one Python version",
            )
            for line_number, version in declarations:
                versions[f"{relative_workflow}:{line_number}"] = version

        self.assertTrue(versions, "No maintained Python CI workflows were found")
        return versions


if __name__ == "__main__":
    unittest.main()
