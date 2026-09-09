"""Regression tests for the repository's Python lint entry point."""

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
LINT_RUNNER = REPOSITORY_ROOT / "scripts" / "lint_python.py"


def test_lint_runner_preserves_tracked_python_paths_as_process_arguments():
    with TemporaryDirectory() as temporary_directory:
        repository = Path(temporary_directory)
        tracked_paths = (
            Path("-option-like.py"),
            Path("ordinary.py"),
            Path("package/name with spaces.py"),
            Path("package/semicolon;dollar$.py"),
        )
        for relative_path in tracked_paths:
            path = repository / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("VALUE = 1\n", encoding="utf-8")

        untracked_path = repository / "untracked file.py"
        untracked_path.write_text("VALUE = 2\n", encoding="utf-8")

        subprocess.run(
            ["git", "init", "--quiet"],
            cwd=repository,
            check=True,
        )
        subprocess.run(
            ["git", "add", "--", *(os.fspath(path) for path in tracked_paths)],
            cwd=repository,
            check=True,
        )

        invocation_path = repository / "lint-invocation.json"
        fake_pylint = repository / "pylint"
        fake_pylint.mkdir()
        (fake_pylint / "__init__.py").write_text("", encoding="utf-8")
        (fake_pylint / "__main__.py").write_text(
            "import json, os, sys\n"
            "with open(os.environ['LINT_INVOCATION_PATH'], 'w', encoding='utf-8') as output:\n"
            "    json.dump({'executable': sys.executable, 'arguments': sys.argv[1:]}, output)\n",
            encoding="utf-8",
        )

        environment = os.environ.copy()
        environment["LINT_INVOCATION_PATH"] = os.fspath(invocation_path)
        result = subprocess.run(
            [sys.executable, os.fspath(LINT_RUNNER)],
            cwd=repository,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        invocation = json.loads(invocation_path.read_text(encoding="utf-8"))
        assert invocation == {
            "executable": sys.executable,
            "arguments": [
                "--",
                *(os.fspath(path) for path in tracked_paths),
            ],
        }
        assert os.fspath(untracked_path.relative_to(repository)) not in invocation[
            "arguments"
        ]
