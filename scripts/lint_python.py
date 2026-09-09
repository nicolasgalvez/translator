#!/usr/bin/env python3
"""Run Pylint against every Python file tracked by Git."""

import os
import subprocess
import sys
from pathlib import Path


class PythonLintRunner:
    """Preserve tracked filenames from Git through the Pylint process boundary."""

    def __init__(self, repository: Path | None = None):
        self.repository = repository or Path.cwd()

    def tracked_python_files(self) -> list[str]:
        """Return tracked Python paths decoded with filesystem semantics."""
        result = subprocess.run(
            ["git", "ls-files", "-z", "--", "*.py"],
            cwd=self.repository,
            check=True,
            stdout=subprocess.PIPE,
        )
        return [os.fsdecode(path) for path in result.stdout.split(b"\0") if path]

    def run(self) -> int:
        """Run Pylint with each tracked path as a distinct argument."""
        result = subprocess.run(
            [sys.executable, "-m", "pylint", "--", *self.tracked_python_files()],
            cwd=self.repository,
            check=False,
        )
        return result.returncode


def main() -> int:
    """Run the repository linter from the command line."""
    return PythonLintRunner().run()


if __name__ == "__main__":
    raise SystemExit(main())
