"""Behavior tests for resolving and validating the PR synthetic merge."""

import subprocess

import pytest

from scripts.ci_merge_candidate import (
    CandidateValidationError,
    PullRequestMergeCandidate,
)


def run_git(repository, *arguments):
    """Run Git in a temporary repository and return stripped stdout."""
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def create_merge(repository):
    """Create a two-parent merge and return its merge, base, and head SHAs."""
    run_git(repository, "init", "--initial-branch=main")
    run_git(repository, "config", "user.name", "CI Test")
    run_git(repository, "config", "user.email", "ci@example.test")
    tracked = repository / "tracked.txt"
    tracked.write_text("base\n", encoding="utf-8")
    run_git(repository, "add", "tracked.txt")
    run_git(repository, "commit", "-m", "base")
    base_sha = run_git(repository, "rev-parse", "HEAD")

    run_git(repository, "checkout", "-b", "feature")
    tracked.write_text("base\nhead\n", encoding="utf-8")
    run_git(repository, "commit", "-am", "head")
    head_sha = run_git(repository, "rev-parse", "HEAD")

    run_git(repository, "checkout", "main")
    run_git(repository, "merge", "--no-ff", "feature", "-m", "merge")
    merge_sha = run_git(repository, "rev-parse", "HEAD")
    return merge_sha, base_sha, head_sha


def test_candidate_derives_the_merge_ref_and_sha_from_a_validated_checkout(tmp_path):
    merge_sha, base_sha, head_sha = create_merge(tmp_path)
    candidate = PullRequestMergeCandidate(tmp_path, "52", base_sha, head_sha)

    assert candidate.merge_ref == "refs/pull/52/merge"
    assert candidate.validated_sha() == merge_sha


@pytest.mark.parametrize("pr_number", ("", "0", "01", "-1", "52x"))
def test_candidate_rejects_a_malformed_pull_request_number(tmp_path, pr_number):
    full_sha = "a" * 40

    with pytest.raises(CandidateValidationError, match="pull request number"):
        PullRequestMergeCandidate(tmp_path, pr_number, full_sha, full_sha)


@pytest.mark.parametrize(
    "base_sha,head_sha",
    (("", "b" * 40), ("a" * 39, "b" * 40), ("a" * 40, "g" * 40)),
)
def test_candidate_rejects_malformed_event_shas(tmp_path, base_sha, head_sha):
    with pytest.raises(CandidateValidationError, match="full hexadecimal SHA"):
        PullRequestMergeCandidate(tmp_path, "52", base_sha, head_sha)


def test_candidate_rejects_a_stale_merge_for_an_old_head(tmp_path):
    _, base_sha, _ = create_merge(tmp_path)
    stale_head = base_sha
    candidate = PullRequestMergeCandidate(tmp_path, "52", base_sha, stale_head)

    with pytest.raises(CandidateValidationError, match="second parent"):
        candidate.validated_sha()


def test_candidate_rejects_a_merge_for_an_unexpected_base(tmp_path):
    _, _, head_sha = create_merge(tmp_path)
    unexpected_base = head_sha
    candidate = PullRequestMergeCandidate(
        tmp_path, "52", unexpected_base, head_sha
    )

    with pytest.raises(CandidateValidationError, match="first parent"):
        candidate.validated_sha()


def test_candidate_rejects_a_non_merge_checkout(tmp_path):
    _, base_sha, head_sha = create_merge(tmp_path)
    run_git(tmp_path, "checkout", "--detach", head_sha)
    candidate = PullRequestMergeCandidate(tmp_path, "52", base_sha, head_sha)

    with pytest.raises(CandidateValidationError, match="exactly two parents"):
        candidate.validated_sha()


def test_candidate_fails_closed_when_no_merge_was_checked_out(tmp_path):
    full_sha = "a" * 40
    candidate = PullRequestMergeCandidate(tmp_path / "missing", "52", full_sha, full_sha)

    with pytest.raises(CandidateValidationError, match="Could not resolve"):
        candidate.validated_sha()
