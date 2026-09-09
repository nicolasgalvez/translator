"""Contracts for production dependency security and auditing."""

import re
import tomllib
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MINIMUM_SAFE_STANZA = (1, 12, 2)
STANZA_REQUIREMENT = "stanza>=1.12.2"
PINNED_UV = "uvx --from 'uv==0.12.12' uv"
LOCKED_AUDIT_RUN = "uv run --project dependency-audit --locked"


def read(name):
    return (REPOSITORY_ROOT / name).read_text(encoding="utf-8")


def load_toml(name):
    with (REPOSITORY_ROOT / name).open("rb") as toml_file:
        return tomllib.load(toml_file)


def version_tuple(version):
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    assert match is not None, f"Expected a three-part version, got {version!r}"
    return tuple(map(int, match.groups()))


def test_locked_runtime_uses_a_non_vulnerable_stanza():
    lock = load_toml("uv.lock")
    stanza_packages = [
        package for package in lock["package"] if package["name"] == "stanza"
    ]

    assert len(stanza_packages) == 1
    assert version_tuple(stanza_packages[0]["version"]) >= MINIMUM_SAFE_STANZA


def test_stanza_override_is_narrow_and_explicit():
    pyproject = load_toml("pyproject.toml")

    assert STANZA_REQUIREMENT in pyproject["project"]["dependencies"]
    assert pyproject["tool"]["uv"]["override-dependencies"] == [
        STANZA_REQUIREMENT
    ]


def test_lock_has_no_stale_uv_platform_marker_expansion():
    lock = read("uv.lock")

    assert (
        "(platform_machine == 'arm64' and sys_platform == 'darwin') or "
        "(platform_machine == 'x86_64' and sys_platform == 'linux') or "
        "(sys_platform != 'darwin'"
    ) not in lock


def test_dependency_scanner_environment_is_locked():
    workflow = read(".github/workflows/dependency-audit.yml")
    readme = read("README.md")
    audit_project = load_toml("dependency-audit/pyproject.toml")
    audit_lock = load_toml("dependency-audit/uv.lock")

    assert audit_project["project"]["dependencies"] == ["pip-audit==2.10.1"]
    pip_audit_packages = [
        package
        for package in audit_lock["package"]
        if package["name"] == "pip-audit"
    ]
    assert [package["version"] for package in pip_audit_packages] == ["2.10.1"]
    assert f"{LOCKED_AUDIT_RUN} pip-audit" in workflow
    assert f"{PINNED_UV} run --project dependency-audit --locked pip-audit" in readme
    assert "uvx --from 'pip-audit" not in workflow
    assert "uvx --from 'pip-audit" not in readme


def test_dependency_audit_exports_the_locked_production_graph():
    workflow_path = ".github/workflows/dependency-audit.yml"
    workflow = read(workflow_path)

    for fragment in (
        "  pull_request:",
        "  push:",
        "    branches: [main]",
        '      - "pyproject.toml"',
        '      - "uv.lock"',
        '      - "dependency-audit/pyproject.toml"',
        '      - "dependency-audit/uv.lock"',
        '      - "tests/test_argos_stanza_compatibility.py"',
        '      - "tests/test_dependency_security_contract.py"',
        '      - "README.md"',
        f'      - "{workflow_path}"',
        'version: "0.12.12"',
        "name: linux-cpu",
        "extra: cpu",
        "platform: x86_64-unknown-linux-gnu",
        "name: linux-cuda",
        "extra: cuda",
        "name: macos-mlx",
        "extra: mlx",
        "platform: aarch64-apple-darwin",
        'MACOSX_DEPLOYMENT_TARGET: "14.0"',
        (
            "uv export --locked --no-default-groups --extra "
            "${{ matrix.extra }} --no-emit-project --emit-index-url"
        ),
        "uv pip compile requirements-${{ matrix.name }}.in",
        "--python-platform ${{ matrix.platform }}",
        "--python-version 3.11 --no-deps",
        "--index-strategy unsafe-best-match",
        f"{LOCKED_AUDIT_RUN} python scripts/dependency_audit.py uv.lock",
        (
            "uv run --project dependency-audit --locked pip-audit --requirement "
            "requirements-${{ matrix.name }}.txt --disable-pip --no-deps "
            "--strict --vulnerability-service osv"
        ),
        "uv sync --locked --group dev --extra cpu",
        (
            "uv run --no-sync pytest "
            "tests/test_argos_stanza_compatibility.py -q"
        ),
    ):
        assert fragment in workflow

    assert "--only-group app-test" not in workflow
    assert "--generate-hashes" not in workflow


def test_override_removal_gate_and_local_audit_are_documented():
    readme = read("README.md")

    assert "published Argos Translate release" in readme
    assert "no longer pins Stanza 1.10.1" in readme
    assert "TRAN-47" in readme
    assert "All three target audits must pass" in readme
    assert "CUDA audit fails" not in readme
    assert "torch==2.6.0+cu124" not in readme
    assert "set -euo pipefail\naudit_status=0" in readme
    for fragment in (
        "linux-cpu cpu x86_64-unknown-linux-gnu",
        "linux-cuda cuda x86_64-unknown-linux-gnu",
        "macos-mlx mlx aarch64-apple-darwin",
        "audit_status=0",
        f"{PINNED_UV} export --locked --no-default-groups",
        f"MACOSX_DEPLOYMENT_TARGET=14.0 {PINNED_UV} pip compile",
        '--python-platform "$platform"',
        (
            f"{PINNED_UV} run --project dependency-audit --locked python "
            'scripts/dependency_audit.py uv.lock "requirements-$name.txt"'
        ),
        (
            f"{PINNED_UV} run --project dependency-audit --locked pip-audit "
            "--requirement "
            '"requirements-$name.txt" --disable-pip --no-deps --strict '
            "--vulnerability-service osv"
        ),
        "|| audit_status=1",
        'test "$audit_status" -eq 0',
    ):
        assert fragment in readme

    assert "--generate-hashes" not in readme


def test_lightweight_pytest_leaves_real_package_coverage_to_audit_job():
    pytest_workflow = read(".github/workflows/pytest.yml")

    assert "--ignore=tests/test_argos_stanza_compatibility.py" in pytest_workflow
