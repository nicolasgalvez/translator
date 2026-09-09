"""Behavior tests for trusted validation of candidate workflow triggers."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.validate_workflow_triggers import WorkflowTriggerPolicy


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "declaration",
    (
        "on: pull_request\n",
        "on: [push, pull_request]\n",
        "on: {push: null, pull_request: null}\n",
        "\"on\": \"pull_request\"\n",
        "'on': ['push', 'pull_request']\n",
        "on:\n  pull_request:\n",
        "on:\n  'pull_request':\n",
        "on:\n  - push\n  - pull_request\n",
    ),
)
def test_candidate_pull_request_trigger_spellings_are_rejected(
    tmp_path,
    declaration,
):
    workflow = tmp_path / "candidate.yml"
    workflow.write_text(declaration + "jobs: {}\n", encoding="utf-8")

    assert WorkflowTriggerPolicy().validate(workflow) == [
        f"{workflow}: candidate-controlled pull_request trigger is forbidden"
    ]


def test_pull_request_target_and_event_expressions_are_allowed(tmp_path):
    workflow = tmp_path / "candidate.yml"
    workflow.write_text(
        "on:\n"
        "  pull_request_target:\n"
        "jobs:\n"
        "  safe:\n"
        "    if: github.event.pull_request.merged == true\n",
        encoding="utf-8",
    )

    assert not WorkflowTriggerPolicy().validate(workflow)


def test_directory_validation_checks_both_workflow_extensions(tmp_path):
    (tmp_path / "safe.yml").write_text("on: push\njobs: {}\n", encoding="utf-8")
    unsafe = tmp_path / "unsafe.yaml"
    unsafe.write_text("on: pull_request\njobs: {}\n", encoding="utf-8")

    assert WorkflowTriggerPolicy().validate_directory(tmp_path) == [
        f"{unsafe}: candidate-controlled pull_request trigger is forbidden"
    ]


@pytest.mark.parametrize(
    "declaration",
    (
        "on: *events\n",
        "on: &events [push]\n",
        'on: [push, "pull\\u005frequest"]\n',
        "on: |\n  push\n",
        "on: >\n  push\n",
    ),
)
def test_ambiguous_trigger_syntax_fails_closed(tmp_path, declaration):
    workflow = tmp_path / "candidate.yml"
    workflow.write_text(declaration + "jobs: {}\n", encoding="utf-8")

    errors = WorkflowTriggerPolicy().validate(workflow)

    assert len(errors) == 1
    assert "cannot safely validate workflow trigger" in errors[0]


def test_trusted_policy_rejects_candidate_rewrite_of_its_validator(tmp_path):
    policy = tmp_path / "policy" / "scripts"
    candidate = tmp_path / "candidate"
    workflows = candidate / ".github" / "workflows"
    policy.mkdir(parents=True)
    workflows.mkdir(parents=True)
    shutil.copyfile(
        REPOSITORY_ROOT / "scripts" / "validate_workflow_triggers.py",
        policy / "validate_workflow_triggers.py",
    )
    candidate_scripts = candidate / "scripts"
    candidate_scripts.mkdir()
    (candidate_scripts / "validate_workflow_triggers.py").write_text(
        "raise SystemExit(0)\n", encoding="utf-8"
    )
    (workflows / "forged.yml").write_text(
        "on: pull_request\njobs: {}\n", encoding="utf-8"
    )

    result = subprocess.run(
        [
            sys.executable,
            policy / "validate_workflow_triggers.py",
            workflows,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "candidate-controlled pull_request trigger is forbidden" in result.stderr
