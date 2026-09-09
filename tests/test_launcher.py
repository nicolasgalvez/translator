"""Behavioral contracts for the native launcher and its config preflight."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
VALUE_OPTIONS = (
    "-H", "--host", "-p", "--port", "-m", "--model",
    "-d", "--device", "-b", "--backend", "-l", "--language",
)
RECOGNIZED_OPTIONS = VALUE_OPTIONS + (
    "--frontend-dev", "--skip-frontend-build", "-h", "--help",
)


class LauncherFixture:
    """A real launcher with observable stand-ins for external package tools."""

    def __init__(self, root: Path):
        self.root = root
        self.bin_directory = root / "bin"
        self.call_log = root / "calls.log"
        self._copy_project_files()
        self._write_tool_stubs()

    def _copy_project_files(self):
        shutil.copyfile(REPOSITORY_ROOT / "run.sh", self.root / "run.sh")
        for name in ("runtime_config.py", "language.py", "websocket_security.py"):
            shutil.copyfile(REPOSITORY_ROOT / name, self.root / name)
        preflight = REPOSITORY_ROOT / "scripts" / "validate_runtime_config.py"
        scripts = self.root / "scripts"
        scripts.mkdir()
        shutil.copyfile(preflight, scripts / preflight.name)

        frontend = self.root / "frontend"
        frontend.mkdir()
        (frontend / "package.json").write_text("{}\n", encoding="utf-8")
        (frontend / "package-lock.json").write_text("{}\n", encoding="utf-8")
        modules = frontend / "node_modules"
        modules.mkdir()
        (modules / ".package-lock.json").write_text("{}\n", encoding="utf-8")

    def _write_tool_stubs(self):
        self.bin_directory.mkdir()
        for command in ("node", "npm"):
            self._write_executable(
                command,
                f'#!/bin/sh\nprintf "{command} %s\\n" "$*" >> "$CALL_LOG"\n',
            )
        self._write_executable(
            "uv",
            """#!/bin/sh
printf 'uv %s\n' "$*" >> "$CALL_LOG"
if [ "$1" = run ]; then
    printf '%s|%s|%s|%s|%s|%s\n' \
        "$TRANSLATOR_HOST" "$TRANSLATOR_PORT" "$TRANSLATOR_MODEL" \
        "$TRANSLATOR_DEVICE" "$TRANSLATOR_BACKEND" "$TRANSLATOR_LANGUAGE"
fi
""",
        )

    def _write_executable(self, name: str, content: str):
        path = self.bin_directory / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def run(self, *arguments: str, environment: dict[str, str] | None = None):
        clean_environment = {
            name: value for name, value in os.environ.items()
            if not name.startswith("TRANSLATOR_")
        }
        return subprocess.run(
            ["bash", str(self.root / "run.sh"), *arguments],
            cwd=self.root,
            env={
                **clean_environment,
                **(environment or {}),
                "CALL_LOG": str(self.call_log),
                "PATH": f"{self.bin_directory}:{os.environ['PATH']}",
            },
            capture_output=True,
            text=True,
            check=False,
        )

    def calls(self) -> str:
        return self.call_log.read_text(encoding="utf-8") if self.call_log.exists() else ""


@pytest.fixture(name="launcher")
def fixture_launcher(tmp_path):
    return LauncherFixture(tmp_path)


@pytest.mark.parametrize("option", VALUE_OPTIONS)
def test_value_option_rejects_a_missing_value_before_side_effects(launcher, option):
    result = launcher.run(option)

    assert result.returncode == 2
    assert result.stderr.strip() == f"translator: {option} requires a value"
    assert "Starting transcriber" not in result.stdout
    assert launcher.calls() == ""


@pytest.mark.parametrize("following_option", RECOGNIZED_OPTIONS)
@pytest.mark.parametrize("option", VALUE_OPTIONS)
def test_value_option_rejects_a_following_option_before_side_effects(
    launcher, option, following_option,
):
    result = launcher.run(option, following_option)

    assert result.returncode == 2
    assert result.stderr.strip() == f"translator: {option} requires a value"
    assert "Starting transcriber" not in result.stdout + result.stderr
    assert launcher.calls() == ""


@pytest.mark.parametrize(("arguments", "message"), [
    (("--port", "invalid"), "TRANSLATOR_PORT must be an integer"),
    (("--port", "0"), "TRANSLATOR_PORT must be between 1 and 65535"),
    (("--port", "65536"), "TRANSLATOR_PORT must be between 1 and 65535"),
    (
        ("--backend", "invalid"),
        "TRANSLATOR_BACKEND must be 'faster-whisper' or 'mlx-whisper'",
    ),
    (("--language", "invalid"), "TRANSLATOR_LANGUAGE"),
])
def test_invalid_option_fails_before_dependency_or_runtime_side_effects(
    launcher, arguments, message,
):
    result = launcher.run(*arguments)

    assert result.returncode == 2
    assert message in result.stderr
    assert "Starting transcriber" not in result.stdout
    assert launcher.calls() == ""


def test_invalid_environment_fails_before_dependency_or_runtime_side_effects(launcher):
    result = launcher.run(environment={"TRANSLATOR_CAPTION_QUEUE_CAPACITY": "0"})

    assert result.returncode == 2
    assert "TRANSLATOR_CAPTION_QUEUE_CAPACITY" in result.stderr
    assert "Starting transcriber" not in result.stdout
    assert launcher.calls() == ""


def test_valid_options_reach_the_application_environment(launcher):
    result = launcher.run(
        "--host", "0.0.0.0", "--port", "9123", "--model", "medium",
        "--device", "USB Mic", "--backend", "mlx-whisper",
        "--language", "auto", "--skip-frontend-build",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines()[-1] == (
        "0.0.0.0|9123|medium|USB Mic|mlx-whisper|auto"
    )


def test_unrecognized_dash_prefixed_text_remains_a_valid_option_value(launcher):
    result = launcher.run("--device", "-USB Mic", "--skip-frontend-build")

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines()[-1].split("|")[3] == "-USB Mic"


def test_help_exits_without_dependency_or_runtime_side_effects(launcher):
    result = launcher.run("--help")

    assert result.returncode == 0
    assert result.stdout.startswith("Usage:")
    assert result.stderr == ""
    assert launcher.calls() == ""


def test_runtime_config_remains_available_from_its_public_module():
    extracted_config = importlib.import_module("runtime_config").RuntimeConfig
    public_config = importlib.import_module("translator_runtime").RuntimeConfig

    assert public_config is extracted_config
    assert public_config.from_environment({}).port == 8765


def test_preflight_runs_without_site_packages():
    clean_environment = {
        name: value for name, value in os.environ.items()
        if not name.startswith("TRANSLATOR_")
    }
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(REPOSITORY_ROOT / "scripts" / "validate_runtime_config.py"),
        ],
        cwd=REPOSITORY_ROOT,
        env=clean_environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
