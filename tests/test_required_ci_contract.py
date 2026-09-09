"""Contracts for the stable pull-request check and main-branch ruleset."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIRECTORY = REPOSITORY_ROOT / ".github" / "workflows"
WORKFLOW_PATHS = sorted(
    (*WORKFLOW_DIRECTORY.glob("*.yml"), *WORKFLOW_DIRECTORY.glob("*.yaml"))
)
SUITE_WORKFLOWS = {
    "python_test": "pytest.yml",
    "python_lint": "pylint.yml",
    "frontend": "frontend.yml",
    "docker": "docker.yml",
    "workflow_lint": "actionlint.yml",
    "python_requirement": "python-requirement.yml",
    "dependency_audit": "dependency-audit.yml",
}


def read_workflow(name):
    return (WORKFLOW_DIRECTORY / name).read_text(encoding="utf-8")


def test_required_workflow_runs_from_the_trusted_base_for_every_pull_request():
    workflow = read_workflow("required.yml")
    trigger = workflow.split("permissions:", maxsplit=1)[0]
    workflow_permissions = workflow.split("jobs:", maxsplit=1)[0]

    assert "name: Required" in trigger
    assert "  pull_request_target:" in trigger
    assert "types: [opened, reopened, synchronize]" in trigger
    assert "  pull_request:" not in trigger
    assert "paths:" not in trigger
    assert "paths-ignore:" not in trigger
    assert "permissions:\n  contents: read\n" in workflow
    assert "id-token:" not in workflow
    assert "secrets: inherit" not in workflow
    assert "write" not in workflow_permissions


def test_classifier_uses_the_exact_pull_request_range_and_nul_paths():
    workflow = read_workflow("required.yml")

    assert "github.event.pull_request.base.sha" in workflow
    assert "github.event.pull_request.head.sha" in workflow
    assert "github.event.pull_request.merge_commit_sha" not in workflow
    assert "git -C candidate diff --no-renames --name-only -z" in workflow
    assert "python policy/scripts/ci_path_classifier.py" in workflow
    assert '>> "$GITHUB_OUTPUT"' in workflow


def test_controller_separates_trusted_policy_from_the_candidate_checkout():
    workflow = read_workflow("required.yml")

    assert "ref: ${{ github.event.pull_request.base.sha }}" in workflow
    assert workflow.count("repository: ${{ github.repository }}") == 2
    assert "path: policy" in workflow
    assert "path: candidate" in workflow
    assert "persist-credentials: false" in workflow
    assert "ref: ${{ steps.candidate_input.outputs.merge_ref }}" in workflow
    assert "PR_NUMBER: ${{ github.event.pull_request.number }}" in workflow
    assert "python policy/scripts/ci_merge_candidate.py prepare" in workflow
    assert "python policy/scripts/ci_merge_candidate.py validate" in workflow
    assert "candidate/.github/workflows" in workflow
    assert "candidate_sha: ${{ steps.validated_candidate.outputs.sha }}" in workflow
    assert "head_sha: ${{" not in workflow
    assert "candidate_sha: ${{ needs.classify.outputs.candidate_sha }}" in workflow


def test_controller_validates_candidate_workflows_with_trusted_policy_first():
    workflow = read_workflow("required.yml")
    validator = (
        "python policy/scripts/validate_workflow_triggers.py\n"
        "          candidate/.github/workflows"
    )

    assert validator in workflow
    assert workflow.index(validator) < workflow.index("id: classify")


def test_candidate_policy_rewrites_cannot_bypass_trusted_classification_or_gate(
    tmp_path,
):
    policy = tmp_path / "policy" / "scripts"
    candidate = tmp_path / "candidate" / "scripts"
    policy.mkdir(parents=True)
    candidate.mkdir(parents=True)
    for script in ("ci_path_classifier.py", "ci_result_gate.py"):
        shutil.copyfile(REPOSITORY_ROOT / "scripts" / script, policy / script)
    (candidate / "ci_path_classifier.py").write_text(
        "print('python=false')\n", encoding="utf-8"
    )
    (candidate / "ci_result_gate.py").write_text(
        "raise SystemExit(0)\n", encoding="utf-8"
    )

    classification = subprocess.run(
        [sys.executable, policy / "ci_path_classifier.py"],
        input=b"app.py\0",
        capture_output=True,
        check=True,
    )
    environment = {
        **os.environ,
        "CLASSIFY_RESULT": "success",
        "PYTHON_REQUIRED": "true",
        "PYTHON_TEST_RESULT": "failure",
        "PYTHON_LINT_RESULT": "success",
        "FRONTEND_REQUIRED": "false",
        "FRONTEND_RESULT": "skipped",
        "DOCKER_REQUIRED": "false",
        "DOCKER_RESULT": "skipped",
        "WORKFLOW_LINT_REQUIRED": "false",
        "WORKFLOW_LINT_RESULT": "skipped",
        "PYTHON_REQUIREMENT_REQUIRED": "false",
        "PYTHON_REQUIREMENT_RESULT": "skipped",
        "DEPENDENCY_AUDIT_REQUIRED": "false",
        "DEPENDENCY_AUDIT_RESULT": "skipped",
    }
    gate = subprocess.run(
        [sys.executable, policy / "ci_result_gate.py"],
        env=environment,
        capture_output=True,
        check=False,
    )

    assert b"python=true" in classification.stdout
    assert gate.returncode == 1
    assert b"python-test: expected success, got failure" in gate.stderr


def test_every_suite_is_reusable_without_a_duplicate_pull_request_trigger():
    for workflow_name in SUITE_WORKFLOWS.values():
        workflow = read_workflow(workflow_name)
        trigger = workflow.split("permissions:", maxsplit=1)[0]

        assert "  workflow_call:" in trigger, workflow_name
        assert "  pull_request:" not in trigger, workflow_name


def test_no_workflow_uses_a_candidate_controlled_pull_request_trigger():
    offenders = []
    for workflow_path in WORKFLOW_PATHS:
        workflow = workflow_path.read_text(encoding="utf-8")
        if "  pull_request:" in workflow:
            offenders.append(workflow_path.name)

    assert not offenders


def test_every_reusable_suite_tests_the_exact_candidate_without_credentials():
    for workflow_name in SUITE_WORKFLOWS.values():
        workflow = read_workflow(workflow_name)
        checkout_count = workflow.count("uses: actions/checkout@v7")

        assert "candidate_sha:" in workflow, workflow_name
        assert "required: true" in workflow, workflow_name
        assert workflow.count(
            "ref: ${{ inputs.candidate_sha || github.sha }}"
        ) == checkout_count, workflow_name
        assert workflow.count("persist-credentials: false") == checkout_count, (
            workflow_name
        )


def test_candidate_execution_cannot_restore_or_save_shared_caches():
    for workflow_name in SUITE_WORKFLOWS.values():
        workflow = read_workflow(workflow_name)
        for forbidden in (
            "enable-cache: true",
            "cache: npm",
            "cache-from:",
            "cache-to:",
        ):
            assert forbidden not in workflow, workflow_name

    for workflow_name in ("pytest.yml", "pylint.yml", "dependency-audit.yml"):
        workflow = read_workflow(workflow_name)
        setup_count = workflow.count("uses: astral-sh/setup-uv@v7")
        assert workflow.count("enable-cache: false") == setup_count
        assert workflow.count("restore-cache: false") == setup_count
        assert workflow.count("save-cache: false") == setup_count

    frontend = read_workflow("frontend.yml")
    assert "package-manager-cache: false" in frontend


def test_reusable_workflow_concurrency_cannot_cancel_a_sibling_suite():
    concurrent_workflows = {
        "frontend.yml": "frontend",
        "docker.yml": "docker",
        "python-requirement.yml": "python-requirement",
        "dependency-audit.yml": "dependency-audit",
    }

    for workflow_name, suite in concurrent_workflows.items():
        workflow = read_workflow(workflow_name)
        assert (
            f"group: ${{{{ github.workflow }}}}-{suite}-"
            "${{ github.event.pull_request.number || github.ref }}"
            in workflow
        ), workflow_name


def test_required_workflow_calls_every_suite_and_waits_for_each_result():
    workflow = read_workflow("required.yml")

    for job_name, workflow_name in SUITE_WORKFLOWS.items():
        assert f"  {job_name}:" in workflow
        assert f"uses: ./.github/workflows/{workflow_name}" in workflow
        assert f"needs.{job_name}.result" in workflow
    assert workflow.count(
        "candidate_sha: ${{ needs.classify.outputs.candidate_sha }}"
    ) == len(SUITE_WORKFLOWS)

    required_job = workflow.split("  required:", maxsplit=1)[1]
    assert "if: always()" in required_job
    assert "name: policy-gate" in required_job
    for dependency in ("classify", *SUITE_WORKFLOWS):
        assert dependency in required_job.split("runs-on:", maxsplit=1)[0]


def test_required_gate_rejects_failed_applicable_and_run_inapplicable_suites():
    workflow = read_workflow("required.yml")
    required_job = workflow.split("  required:", maxsplit=1)[1]

    assert "python policy/scripts/ci_result_gate.py" in required_job
    assert "CLASSIFY_RESULT: ${{ needs.classify.result }}" in required_job
    for suite in SUITE_WORKFLOWS:
        result_name = f"{suite.upper()}_RESULT"
        assert result_name in required_job


def test_isolated_publisher_reports_the_gate_on_the_validated_merge_sha():
    workflow = read_workflow("required.yml")
    publisher = workflow.split("  publish_required:", maxsplit=1)[1]

    assert "if: always()" in publisher
    assert "needs:\n      - classify\n      - required" in publisher
    assert "permissions:\n      checks: write" in publisher
    assert "actions/checkout" not in publisher
    assert "candidate/" not in publisher
    assert "policy/" not in publisher
    assert "CANDIDATE_SHA: ${{ needs.classify.outputs.candidate_sha }}" in publisher
    assert "needs.classify.outputs.head_sha" not in publisher
    assert "github.event.pull_request.head.sha" not in publisher
    assert "needs.classify.result" in publisher
    assert "needs.required.result" in publisher
    assert 'CONCLUSION="failure"' in publisher
    assert 'CONCLUSION="success"' in publisher
    assert '"name": "required"' in publisher
    assert '--arg head_sha "$CANDIDATE_SHA"' in publisher
    assert '"head_sha": $head_sha' in publisher
    assert '"conclusion": $conclusion' in publisher
    assert "/repos/$GITHUB_REPOSITORY/check-runs" in publisher


def test_publisher_fails_closed_without_a_validated_candidate_sha():
    workflow = read_workflow("required.yml")
    before_publisher, publisher = workflow.split(
        "  publish_required:", maxsplit=1
    )

    assert "checks: write" not in before_publisher
    assert '""|*[!0-9a-fA-F]*)' in publisher
    assert '[ "$CLASSIFY_RESULT" != "success" ]' in publisher
    assert '[ "${#CANDIDATE_SHA}" -ne 40 ]' in publisher
    assert 'echo "Cannot publish without a validated candidate SHA." >&2' in publisher
    assert publisher.index('[ "$CLASSIFY_RESULT" != "success" ]') < publisher.index(
        "/repos/$GITHUB_REPOSITORY/check-runs"
    )
    assert publisher.count("checks: write") == 1


def test_validated_merge_identity_prevents_stale_or_cross_base_publication():
    workflow = read_workflow("required.yml")
    classifier = workflow.split("  python_test:", maxsplit=1)[0]
    publisher = workflow.split("  publish_required:", maxsplit=1)[1]

    assert '"$PR_NUMBER" "$BASE_SHA" "$HEAD_SHA"' in classifier
    assert "python policy/scripts/ci_merge_candidate.py validate" in classifier
    assert "ref: ${{ steps.candidate_input.outputs.merge_ref }}" in classifier
    assert "id: validated_candidate" in classifier
    assert "CANDIDATE_SHA: ${{ needs.classify.outputs.candidate_sha }}" in publisher
    assert "github.event.pull_request.head.sha" not in publisher
    assert "github.event.pull_request.merge_commit_sha" not in publisher


def test_candidate_sha_is_derived_from_the_checked_out_base_repository_merge_ref():
    workflow = read_workflow("required.yml")
    classifier = workflow.split("  python_test:", maxsplit=1)[0]

    assert "steps.candidate_input.outputs.merge_ref" in classifier
    assert "github.event.pull_request.merge_commit_sha" not in classifier
    assert "candidate_input.outputs.sha" not in classifier
    assert '"$PR_NUMBER" "$BASE_SHA" "$HEAD_SHA"' in classifier


def test_jira_failure_reporting_observes_the_required_workflow():
    workflow = read_workflow("jira.yml")

    assert 'workflows: ["CI", "Required"]' in workflow


def test_jira_pr_automation_is_base_owned_and_never_executes_candidate_code():
    workflow = read_workflow("jira.yml")
    trigger = workflow.split("jobs:", maxsplit=1)[0]

    assert "  pull_request_target:" in trigger
    assert "types: [opened, reopened, closed]" in trigger
    assert "  pull_request:" not in trigger
    assert "github.event_name == 'pull_request_target'" in workflow
    assert "actions/checkout" not in workflow
    assert "permissions:\n  contents: read\n" in trigger
    assert "write" not in trigger


def test_only_the_isolated_publisher_can_write_checks():
    grants = []
    for workflow_path in WORKFLOW_PATHS:
        workflow = workflow_path.read_text(encoding="utf-8")
        grants.extend(
            workflow_path.name for _ in range(workflow.count("checks: write"))
        )

    assert grants == ["required.yml"]


def test_versioned_ruleset_protects_the_default_branch_without_bypass():
    payload = json.loads(
        (REPOSITORY_ROOT / "config" / "main-ruleset.json").read_text(
            encoding="utf-8"
        )
    )

    assert payload["target"] == "branch"
    assert payload["enforcement"] == "active"
    assert payload["bypass_actors"] == []
    assert payload["conditions"] == {
        "ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}
    }
    assert {rule["type"] for rule in payload["rules"]} >= {
        "deletion",
        "non_fast_forward",
        "pull_request",
        "required_status_checks",
    }

    status_rule = next(
        rule for rule in payload["rules"]
        if rule["type"] == "required_status_checks"
    )
    assert status_rule["parameters"] == {
        "strict_required_status_checks_policy": True,
        "do_not_enforce_on_create": False,
        "required_status_checks": [
            {"context": "required", "integration_id": 15368}
        ],
    }
