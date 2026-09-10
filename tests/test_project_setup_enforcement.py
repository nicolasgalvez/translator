"""Contracts for repository-local commit and merge enforcement."""

from __future__ import annotations

from pathlib import Path
import os
import subprocess


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_pre_commit_checks_secrets_and_conventional_messages():
    config = (REPOSITORY_ROOT / ".pre-commit-config.yaml").read_text(
        encoding="utf-8"
    )

    assert "https://github.com/gitleaks/gitleaks" in config
    assert "- id: gitleaks" in config
    assert "https://github.com/compilerla/conventional-pre-commit" in config
    assert "- id: conventional-pre-commit" in config
    assert "stages: [commit-msg]" in config


def test_hook_bootstrap_selects_the_tracked_dispatchers(tmp_path):
    subprocess.run(["git", "init", "--quiet", tmp_path], check=True)
    (tmp_path / ".githooks").mkdir()

    subprocess.run(
        [
            "python",
            REPOSITORY_ROOT / "scripts" / "git_hooks.py",
            "install",
        ],
        cwd=tmp_path,
        check=True,
    )

    configured = subprocess.run(
        ["git", "config", "--local", "--get", "core.hooksPath"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert configured.stdout.strip() == ".githooks"


def test_tracked_dispatchers_cover_commit_policy_and_user_hooks():
    dispatchers = {
        path.name: path.read_text(encoding="utf-8")
        for path in (REPOSITORY_ROOT / ".githooks").iterdir()
        if path.is_file()
    }

    assert set(dispatchers) == {
        "commit-msg",
        "pre-commit",
        "prepare-commit-msg",
        "pre-push",
    }
    for stage, script in dispatchers.items():
        assert "scripts/git_hooks.py" in script
        assert f"run {stage}" in script


def test_dispatcher_chains_the_matching_user_level_hook(tmp_path):
    repository = tmp_path / "repository"
    user_hooks = tmp_path / "user-hooks"
    record = tmp_path / "hook-record"
    repository.mkdir()
    user_hooks.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)

    hook = user_hooks / "prepare-commit-msg"
    hook.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$*\" > '{record}'\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    global_config = tmp_path / "global-gitconfig"
    subprocess.run(
        [
            "git",
            "config",
            "--file",
            global_config,
            "core.hooksPath",
            str(user_hooks),
        ],
        check=True,
    )

    subprocess.run(
        [
            "python",
            REPOSITORY_ROOT / "scripts" / "git_hooks.py",
            "run",
            "prepare-commit-msg",
            "message-file",
            "message",
        ],
        cwd=repository,
        env={**os.environ, "GIT_CONFIG_GLOBAL": str(global_config)},
        check=True,
    )

    assert record.read_text(encoding="utf-8") == "message-file message\n"


def test_ci_has_an_always_available_required_gate():
    workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    pull_request_trigger = workflow.split("concurrency:", maxsplit=1)[0]
    required_job = workflow.split("  required:", maxsplit=1)[1]

    assert "  pull_request:" in pull_request_trigger
    assert "paths-ignore:" not in pull_request_trigger.split(
        "  pull_request:", maxsplit=1
    )[1]
    assert "if: always()" in required_job
    assert "name: required" in required_job
    assert "policy" in required_job.split("runs-on:", maxsplit=1)[0]


def test_ci_policy_runs_the_repository_pre_commit_checks():
    workflow = (
        REPOSITORY_ROOT / ".github" / "workflows" / "project-policy.yml"
    ).read_text(encoding="utf-8")

    assert "  workflow_call:" in workflow
    assert "pre-commit run --all-files" in workflow
    assert "SKIP: pylint" in workflow
    assert "scripts/check-commit-messages.sh" in workflow
