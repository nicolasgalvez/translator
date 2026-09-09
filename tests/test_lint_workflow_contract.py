"""Contracts for safe Python linting and GitHub Actions validation."""

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIRECTORY = REPOSITORY_ROOT / ".github" / "workflows"
ACTIONLINT_VERSION = "1.7.12"
ACTIONLINT_LINUX_AMD64_SHA256 = (
    "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8"
)


def read_workflow(name):
    workflow = WORKFLOW_DIRECTORY / name
    assert workflow.is_file(), f"Missing workflow: {name}"
    return workflow.read_text(encoding="utf-8")


def test_pylint_workflow_uses_the_filename_safe_runner():
    workflow = read_workflow("pylint.yml")

    assert (
        "uv run --locked --extra cpu python scripts/lint_python.py" in workflow
    )
    assert "$(git ls-files" not in workflow


def test_ci_calls_the_reusable_workflow_syntax_check():
    workflow = read_workflow("ci.yml")

    assert "workflow-syntax:" in workflow
    assert "uses: ./.github/workflows/actionlint.yml" in workflow


def test_actionlint_release_is_versioned_and_checksum_verified():
    workflow = read_workflow("actionlint.yml")

    assert f'ACTIONLINT_VERSION: "{ACTIONLINT_VERSION}"' in workflow
    assert "actionlint_${ACTIONLINT_VERSION}_linux_amd64.tar.gz" in workflow
    assert ACTIONLINT_LINUX_AMD64_SHA256 in workflow
    assert "sha256sum --check" in workflow
    assert "curl --fail --location --show-error" in workflow
    assert '"$tool_dir/actionlint"' in workflow


def test_ci_does_not_ignore_lint_runner_or_workflow_changes():
    workflow = read_workflow("ci.yml")
    ignored_paths = workflow.split("paths-ignore:", maxsplit=1)[1]
    ignored_paths = ignored_paths.split("concurrency:", maxsplit=1)[0]

    assert "scripts/" not in ignored_paths
    assert ".github/" not in ignored_paths
