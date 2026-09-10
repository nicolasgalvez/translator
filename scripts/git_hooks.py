"""Install and run the repository's tracked Git hook dispatchers."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
from collections.abc import Sequence


SUPPORTED_STAGES = frozenset(
    {"commit-msg", "pre-commit", "prepare-commit-msg", "pre-push"}
)
PRE_COMMIT_STAGES = frozenset({"commit-msg", "pre-commit"})


class GitHooks:
    """Own repository hook installation and dispatch."""

    def __init__(self, repository_root: Path | None = None) -> None:
        self.repository_root = repository_root or self._repository_root()

    def install(self) -> None:
        """Select the tracked hook directory for the current repository."""
        hooks_path = self.repository_root / ".githooks"
        if not hooks_path.is_dir():
            raise SystemExit(f"Tracked hook directory is missing: {hooks_path}")

        subprocess.run(
            [
                "git",
                "-C",
                str(self.repository_root),
                "config",
                "--local",
                "core.hooksPath",
                ".githooks",
            ],
            check=True,
        )
        print("Git hooks enabled from .githooks")

    def run(self, stage: str, arguments: Sequence[str]) -> None:
        """Run repository policy first, then the matching user-level hook."""
        if stage not in SUPPORTED_STAGES:
            raise SystemExit(f"Unsupported Git hook stage: {stage}")

        if stage in PRE_COMMIT_STAGES:
            self._run_pre_commit(stage, arguments)
        self._run_user_hook(stage, arguments)

    def _run_pre_commit(self, stage: str, arguments: Sequence[str]) -> None:
        command = [
            sys.executable,
            "-m",
            "pre_commit",
            "run",
            "--hook-stage",
            stage,
        ]
        if stage == "commit-msg":
            if not arguments:
                raise SystemExit("commit-msg requires the message filename")
            command.extend(["--commit-msg-filename", arguments[0]])

        subprocess.run(command, cwd=self.repository_root, check=True)

    def _run_user_hook(self, stage: str, arguments: Sequence[str]) -> None:
        configured = subprocess.run(
            ["git", "config", "--global", "--path", "--get", "core.hooksPath"],
            check=False,
            capture_output=True,
            text=True,
        )
        if configured.returncode != 0 or not configured.stdout.strip():
            return

        user_hook = Path(configured.stdout.strip()).expanduser() / stage
        repository_hook = (self.repository_root / ".githooks" / stage).resolve()
        if (
            not user_hook.is_file()
            or not user_hook.stat().st_mode & 0o111
            or user_hook.resolve() == repository_hook
        ):
            return

        subprocess.run([str(user_hook), *arguments], check=True)

    @staticmethod
    def _repository_root() -> Path:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        )
        return Path(result.stdout.strip())


def main(arguments: Sequence[str] | None = None) -> None:
    """Parse CLI arguments and delegate to GitHooks."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("install")
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("stage", choices=sorted(SUPPORTED_STAGES))
    run_parser.add_argument("hook_arguments", nargs=argparse.REMAINDER)
    options = parser.parse_args(arguments)

    hooks = GitHooks()
    try:
        if options.command == "install":
            hooks.install()
        else:
            hooks.run(options.stage, options.hook_arguments)
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.returncode) from None


if __name__ == "__main__":
    main()
