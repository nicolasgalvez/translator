"""Contracts for the supported Docker deployment profile."""

import re
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def read(name):
    return (REPOSITORY_ROOT / name).read_text(encoding="utf-8")


def docker_workflow_paths():
    workflow = read(".github/workflows/docker.yml")
    pull_request_paths = workflow.split("paths: &docker_paths", maxsplit=1)[1]
    pull_request_paths = pull_request_paths.split("push:", maxsplit=1)[0]
    return re.findall(r'^\s+- "([^"]+)"$', pull_request_paths, flags=re.MULTILINE)


def github_path_matches(pattern, path):
    """Match the *, **, and ? forms used by this workflow's path filters."""
    expression = ""
    index = 0
    while index < len(pattern):
        if pattern[index:index + 2] == "**":
            expression += ".*"
            index += 2
        elif pattern[index] == "*":
            expression += "[^/]*"
            index += 1
        elif pattern[index] == "?":
            expression += "[^/]"
            index += 1
        else:
            expression += re.escape(pattern[index])
            index += 1
    return re.fullmatch(expression, path) is not None


def test_image_uses_the_locked_python_3_11_environment():
    dockerfile = read("Dockerfile")

    assert "nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04" in dockerfile
    assert "uv python install 3.11" in dockerfile
    assert "UV_PROJECT_ENVIRONMENT=/opt/translator/.venv" in dockerfile
    assert 'CMD ["/opt/translator/.venv/bin/python", "app.py"]' in dockerfile


def test_container_uses_a_public_bind_address():
    dockerfile = read("Dockerfile")
    compose = read("docker-compose.yml")

    assert "ENV TRANSLATOR_HOST=0.0.0.0" in dockerfile
    assert "TRANSLATOR_HOST=0.0.0.0" in compose


def test_compose_requires_an_explicit_audio_input_and_nvidia_gpu():
    compose = read("docker-compose.yml")

    assert "platform: linux/amd64" in compose
    assert re.search(r"TRANSLATOR_DEVICE=\$\{TRANSLATOR_DEVICE:\?[^}]+}", compose)
    assert "driver: nvidia" in compose
    assert "capabilities: [gpu]" in compose
    assert "/dev/snd:/dev/snd" in compose


def test_documented_profile_does_not_promise_an_unsupported_cpu_fallback():
    readme = read("README.md")
    docker_section = readme.split("## Docker", maxsplit=1)[1]

    assert "Falls back to CPU" not in docker_section
    for expected in ("Linux", "NVIDIA Container Toolkit", "ALSA", "macOS", "./run.sh"):
        assert expected in docker_section


def test_ci_builds_and_smoke_tests_the_final_runtime_image():
    dockerfile = read("Dockerfile")
    dockerignore = read(".dockerignore")
    workflow = read(".github/workflows/docker.yml")
    smoke_script = read("scripts/smoke-docker-runtime.sh")

    assert "tests/" in dockerignore
    assert "runtime-smoke" not in dockerfile
    assert "/smoke/docker_smoke_app.py:ro" in smoke_script
    assert "./scripts/smoke-docker-runtime.sh" in workflow
    assert "Build and smoke test the runtime image" in workflow
    assert "import ctranslate2, faster_whisper, torch" in smoke_script
    assert 'torch.version.cuda == "12.4"' in smoke_script


def test_docker_ci_runs_for_every_kind_of_production_runtime_input():
    paths = docker_workflow_paths()
    runtime_inputs = (
        "app.py",
        "audio_devices.py",
        "translator_runtime.py",
        "transcription/faster_whisper_backend.py",
        "templates/index.html",
        "plugins/example.py",
    )

    uncovered = [
        path
        for path in runtime_inputs
        if not any(github_path_matches(pattern, path) for pattern in paths)
    ]

    assert uncovered == []
