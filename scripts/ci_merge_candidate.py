#!/usr/bin/env python3
"""Resolve and validate the exact synthetic merge used by required CI."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys


class CandidateValidationError(ValueError):
    """Report an invalid or stale pull-request merge candidate."""


class PullRequestMergeCandidate:
    """Own resolution and validation of a base-repository PR merge ref."""

    _PULL_REQUEST_NUMBER = re.compile(r"[1-9][0-9]*")
    _FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")

    def __init__(
        self,
        repository: Path,
        pull_request_number: str,
        base_sha: str,
        head_sha: str,
    ) -> None:
        self.repository = repository
        self.pull_request_number = pull_request_number
        self.base_sha = self._validated_event_sha(base_sha)
        self.head_sha = self._validated_event_sha(head_sha)
        if not self._PULL_REQUEST_NUMBER.fullmatch(pull_request_number):
            raise CandidateValidationError("Invalid pull request number.")

    @property
    def merge_ref(self) -> str:
        """Return the server-owned synthetic merge ref for this PR."""
        return f"refs/pull/{self.pull_request_number}/merge"

    def validated_sha(self) -> str:
        """Return HEAD only when it is the exact expected two-parent merge."""
        try:
            result = subprocess.run(
                ["git", "rev-list", "--parents", "-n", "1", "HEAD"],
                cwd=self.repository,
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            raise CandidateValidationError(
                "Could not resolve the checked-out merge candidate."
            ) from error

        commit_and_parents = result.stdout.split()
        if len(commit_and_parents) != 3:
            raise CandidateValidationError(
                "The merge candidate must have exactly two parents."
            )
        actual_sha, first_parent, second_parent = commit_and_parents
        actual_sha = self._validated_event_sha(actual_sha)
        if first_parent.lower() != self.base_sha:
            raise CandidateValidationError(
                "The merge candidate first parent is not the event base SHA."
            )
        if second_parent.lower() != self.head_sha:
            raise CandidateValidationError(
                "The merge candidate second parent is not the event head SHA."
            )
        return actual_sha

    @classmethod
    def _validated_event_sha(cls, sha: str) -> str:
        if not cls._FULL_SHA.fullmatch(sha):
            raise CandidateValidationError("Expected a full hexadecimal SHA.")
        return sha.lower()


def argument_parser() -> argparse.ArgumentParser:
    """Build the small CLI used by the trusted workflow controller."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("pull_request_number")
    prepare.add_argument("base_sha")
    prepare.add_argument("head_sha")
    validate = subparsers.add_parser("validate")
    validate.add_argument("repository", type=Path)
    validate.add_argument("pull_request_number")
    validate.add_argument("base_sha")
    validate.add_argument("head_sha")
    return parser


def main() -> int:
    """Print trusted workflow outputs or fail closed with a diagnostic."""
    arguments = argument_parser().parse_args()
    repository = getattr(arguments, "repository", Path("."))
    try:
        candidate = PullRequestMergeCandidate(
            repository,
            arguments.pull_request_number,
            arguments.base_sha,
            arguments.head_sha,
        )
        if arguments.command == "prepare":
            print(f"merge_ref={candidate.merge_ref}")
        else:
            print(f"sha={candidate.validated_sha()}")
    except CandidateValidationError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
