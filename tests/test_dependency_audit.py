"""Tests for locked dependency-audit validation."""

import tomllib

import pytest

from scripts.dependency_audit import LockedRequirementValidator


def write_lock(path, packages):
    path.write_text(
        'version = 1\nrevision = 3\nrequires-python = ">=3.11"\n\n'
        + "\n".join(
            f'[[package]]\nname = "{name}"\nversion = "{version}"\n'
            for name, version in packages
        ),
        encoding="utf-8",
    )
    with path.open("rb") as lock_file:
        tomllib.load(lock_file)


def test_validator_accepts_only_versions_present_in_uv_lock(tmp_path):
    lock_path = tmp_path / "uv.lock"
    requirements_path = tmp_path / "requirements.txt"
    write_lock(lock_path, [("stanza", "1.14.0"), ("torch", "2.14.0+cpu")])
    requirements_path.write_text(
        "--index-url https://pypi.org/simple\n"
        "stanza==1.14.0\n"
        "torch==2.14.0+cpu\n",
        encoding="utf-8",
    )

    validated = LockedRequirementValidator(lock_path).validate(requirements_path)

    assert validated == {("stanza", "1.14.0"), ("torch", "2.14.0+cpu")}


def test_validator_rejects_a_version_not_present_in_uv_lock(tmp_path):
    lock_path = tmp_path / "uv.lock"
    requirements_path = tmp_path / "requirements.txt"
    write_lock(lock_path, [("torch", "2.14.0+cpu")])
    requirements_path.write_text("torch==2.14.0\n", encoding="utf-8")

    with pytest.raises(ValueError, match="torch==2.14.0"):
        LockedRequirementValidator(lock_path).validate(requirements_path)
