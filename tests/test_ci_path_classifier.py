"""Behavior tests for the pull-request CI path policy."""

import subprocess
import sys
from pathlib import Path

from scripts.ci_path_classifier import CiPathClassifier


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SUITES = (
    "python",
    "frontend",
    "docker",
    "workflow_lint",
    "python_requirement",
    "dependency_audit",
)


def expected(**enabled):
    """Return a complete expected result with unspecified suites disabled."""
    return {suite: enabled.get(suite, False) for suite in SUITES}


def test_documentation_only_changes_need_no_expensive_suite():
    classifier = CiPathClassifier()

    assert classifier.classify(["docs/operator-guide.md"]) == expected()


def test_backend_changes_run_python_and_the_runtime_image():
    classifier = CiPathClassifier()

    assert classifier.classify(["transcription/audio_processor.py"]) == expected(
        python=True,
        docker=True,
    )


def test_frontend_changes_run_frontend_and_the_runtime_image():
    classifier = CiPathClassifier()

    assert classifier.classify(["frontend/src/App.tsx"]) == expected(
        frontend=True,
        docker=True,
    )


def test_docker_configuration_changes_run_only_the_docker_suite():
    classifier = CiPathClassifier()

    assert classifier.classify(["docker-compose.yml"]) == expected(
        python=True,
        docker=True,
    )


def test_workflow_changes_run_lint_and_affected_workflow_contracts():
    classifier = CiPathClassifier()

    assert classifier.classify(
        [".github/workflows/dependency-audit.yml"]
    ) == expected(
        python=True,
        workflow_lint=True,
        python_requirement=True,
        dependency_audit=True,
    )


def test_suite_workflow_changes_run_the_suite_they_define():
    classifier = CiPathClassifier()

    expectations = {
        ".github/workflows/pytest.yml": {"python"},
        ".github/workflows/pylint.yml": {"python"},
        ".github/workflows/frontend.yml": {"python", "frontend"},
        ".github/workflows/docker.yml": {"python", "docker"},
        ".github/workflows/actionlint.yml": {"python", "workflow_lint"},
        ".github/workflows/python-requirement.yml": {
            "python",
            "python_requirement",
        },
        ".github/workflows/dependency-audit.yml": {
            "python",
            "dependency_audit",
        },
    }

    for path, required_suites in expectations.items():
        result = classifier.classify([path])
        for suite in required_suites:
            assert result[suite], f"{path} did not select {suite}"


def test_python_requirement_changes_run_all_dependent_suites():
    classifier = CiPathClassifier()

    assert classifier.classify(["pyproject.toml"]) == expected(
        python=True,
        docker=True,
        python_requirement=True,
        dependency_audit=True,
    )


def test_dependency_scanner_lock_changes_run_the_dependency_suite():
    classifier = CiPathClassifier()

    assert classifier.classify(["dependency-audit/uv.lock"]) == expected(
        python=True,
        dependency_audit=True,
    )


def test_mixed_changes_union_each_applicable_suite():
    classifier = CiPathClassifier()

    assert classifier.classify(
        ["docs/guide.md", "frontend/src/App.tsx", "app.py", "Dockerfile"]
    ) == expected(python=True, frontend=True, docker=True)


def test_readme_changes_keep_documented_runtime_contracts_covered():
    classifier = CiPathClassifier()

    assert classifier.classify(["README.md"]) == expected(
        python=True,
        frontend=True,
        python_requirement=True,
        dependency_audit=True,
    )


def test_every_non_python_input_read_by_python_tests_selects_python():
    classifier = CiPathClassifier()
    contract_inputs = (
        ".dockerignore",
        ".pylintrc",
        "Dockerfile",
        "README.md",
        "docker-compose.yml",
        "docs/uv-evaluation.md",
        "run.sh",
        "scripts/smoke-docker-runtime.sh",
        "templates/captions.html",
        "templates/history.html",
        "templates/index.html",
        "templates/view.html",
        "tests/docker-context.Dockerfile",
        "tests/fixtures/conversation_es.wav",
        "uv.lock",
    )

    uncovered = [
        path for path in contract_inputs
        if not classifier.classify([path])["python"]
    ]

    assert uncovered == []


def test_python_contract_input_directories_select_python():
    classifier = CiPathClassifier()
    contract_inputs = (
        ".github/workflows/future.yml",
        "config/future-ruleset.json",
        "dependency-audit/future.lock",
        "templates/future.html",
        "tests/fixtures/future.wav",
    )

    assert all(
        classifier.classify([path])["python"] for path in contract_inputs
    )


def test_git_rename_exposes_both_old_and_new_paths_to_classification(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "ci@example.com"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "CI"], cwd=repository, check=True
    )
    old_path = repository / "backend.py"
    old_path.write_text("print('covered')\n", encoding="utf-8")
    subprocess.run(["git", "add", "backend.py"], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "fixture"], cwd=repository, check=True
    )
    (repository / "docs").mkdir()
    old_path.rename(repository / "docs" / "operator-guide.md")
    subprocess.run(["git", "add", "-A"], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "rename fixture"],
        cwd=repository,
        check=True,
    )

    diff = subprocess.run(
        [
            "git",
            "diff",
            "--no-renames",
            "--name-only",
            "-z",
            "HEAD^",
            "HEAD",
        ],
        cwd=repository,
        check=True,
        capture_output=True,
    ).stdout
    paths = [path.decode() for path in diff.split(b"\0") if path]

    assert paths == ["backend.py", "docs/operator-guide.md"]
    assert CiPathClassifier().classify(paths)["python"] is True


def test_cli_reads_hostile_filenames_from_a_nul_delimited_stream():
    paths = (
        b"docs/line\nbreak.md\0"
        b"--leading-option.py\0"
        b"frontend/src/space name.tsx\0"
    )

    result = subprocess.run(
        [sys.executable, "scripts/ci_path_classifier.py"],
        cwd=REPOSITORY_ROOT,
        input=paths,
        capture_output=True,
        check=True,
    )

    assert result.stdout.decode("ascii").splitlines() == [
        "python=true",
        "frontend=true",
        "docker=true",
        "workflow_lint=false",
        "python_requirement=false",
        "dependency_audit=false",
    ]


def test_empty_change_set_disables_every_suite():
    classifier = CiPathClassifier()

    assert classifier.classify([]) == expected()
