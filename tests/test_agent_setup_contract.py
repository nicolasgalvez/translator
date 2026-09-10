"""Contracts for the committed project-level agent skill installation."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def tracked_paths() -> set[str]:
    """Return every path represented in Git's index."""
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    )
    return {
        path.decode("utf-8")
        for path in result.stdout.split(b"\0")
        if path
    }


def test_every_locked_skill_and_claude_link_is_tracked():
    lock = json.loads(
        (REPOSITORY_ROOT / "skills-lock.json").read_text(encoding="utf-8")
    )
    tracked = tracked_paths()

    assert "skills-lock.json" in tracked
    for skill_name in lock["skills"]:
        canonical = Path(".agents") / "skills" / skill_name / "SKILL.md"
        claude_link = Path(".claude") / "skills" / skill_name

        assert canonical.as_posix() in tracked
        assert claude_link.as_posix() in tracked
        assert (REPOSITORY_ROOT / claude_link).is_symlink()
        assert (REPOSITORY_ROOT / claude_link).resolve() == (
            REPOSITORY_ROOT / canonical.parent
        ).resolve()


def test_agent_documentation_routes_to_repository_configuration():
    agents = (REPOSITORY_ROOT / "AGENTS.md").read_text(encoding="utf-8")

    for document in (
        "docs/agents/issue-tracker.md",
        "docs/agents/triage-labels.md",
        "docs/agents/domain.md",
    ):
        assert document in agents


def test_pre_commit_formatters_do_not_rewrite_locked_skill_sources():
    config = (REPOSITORY_ROOT / ".pre-commit-config.yaml").read_text(
        encoding="utf-8"
    )

    for hook_id in ("trailing-whitespace", "end-of-file-fixer"):
        hook = config.split(f"- id: {hook_id}", maxsplit=1)[1].split(
            "- id:", maxsplit=1
        )[0]
        assert "exclude: ^\\.agents/" in hook


def test_third_party_skill_sources_include_their_license_notices():
    lock = json.loads(
        (REPOSITORY_ROOT / "skills-lock.json").read_text(encoding="utf-8")
    )
    required_notices = {
        "mattpocock/skills": ".agents/LICENSES/mattpocock-skills-MIT.txt",
        "obra/superpowers": ".agents/LICENSES/obra-superpowers-MIT.txt",
    }

    assert {skill["source"] for skill in lock["skills"].values()} == set(
        required_notices
    )
    for notice in required_notices.values():
        text = (REPOSITORY_ROOT / notice).read_text(encoding="utf-8")
        assert "MIT License" in text
        assert "permission notice shall be included" in text
