"""Contracts for the supported Docker deployment profile."""

import re
import subprocess
import tomllib
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.version import Version


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def read(name):
    return (REPOSITORY_ROOT / name).read_text(encoding="utf-8")


def load_toml(name):
    with (REPOSITORY_ROOT / name).open("rb") as toml_file:
        return tomllib.load(toml_file)


def docker_workflow_paths():
    workflow = read(".github/workflows/docker.yml")
    push_paths = workflow.split("  push:", maxsplit=1)[1]
    push_paths = push_paths.split("permissions:", maxsplit=1)[0]
    return re.findall(r'^\s+- "([^"]+)"$', push_paths, flags=re.MULTILINE)


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

    assert "nvidia/cuda:12.6.3-cudnn-runtime-ubuntu22.04" in dockerfile
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
    assert 'torch.version.cuda == "12.6"' in smoke_script
    assert "torch_version.release >= (2, 14, 0)" in smoke_script
    assert 'torch_version.local == "cu126"' in smoke_script


def test_cuda_extra_uses_the_official_cu126_torch_build():
    project = load_toml("pyproject.toml")
    cuda_dependencies = project["project"]["optional-dependencies"]["cuda"]
    cuda_indexes = {
        index["name"]: index["url"] for index in project["tool"]["uv"]["index"]
    }

    assert cuda_dependencies == ["torch>=2.14.0; sys_platform == 'linux'"]
    assert cuda_indexes["pytorch-cuda"] == "https://download.pytorch.org/whl/cu126"


def test_lock_keeps_cpu_cuda_and_macos_torch_sources_separate():
    lock = load_toml("uv.lock")
    torch_packages = {
        package["source"]["registry"]: package["version"]
        for package in lock["package"]
        if package["name"] == "torch"
    }

    assert Version(torch_packages["https://download.pytorch.org/whl/cu126"]) \
        >= Version("2.14.0+cu126")
    assert torch_packages["https://download.pytorch.org/whl/cu126"].endswith("+cu126")
    assert torch_packages["https://download.pytorch.org/whl/cpu"].endswith("+cpu")
    assert "+" not in torch_packages["https://pypi.org/simple"]


def test_exported_cuda_target_uses_the_locked_cuda_12_6_package_set():
    result = subprocess.run(
        [
            "uv", "export", "--locked", "--no-default-groups", "--extra", "cuda",
            "--no-emit-project", "--no-annotate", "--no-header", "--no-hashes",
        ],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    environment = default_environment()
    environment.update({
        "platform_machine": "x86_64",
        "python_full_version": "3.11.16",
        "python_version": "3.11",
        "sys_platform": "linux",
    })
    cuda_packages = {}
    for line in result.stdout.splitlines():
        if not line or line.startswith(("#", "--")):
            continue
        requirement = Requirement(line)
        if requirement.marker is not None and not requirement.marker.evaluate(environment):
            continue
        pins = [
            specifier.version for specifier in requirement.specifier
            if specifier.operator == "=="
        ]
        assert len(pins) == 1, f"Expected one exact pin for {requirement.name}"
        cuda_packages[requirement.name.lower()] = pins[0]

    expected_nvidia_packages = {
        "nvidia-cublas-cu12",
        "nvidia-cuda-cupti-cu12",
        "nvidia-cuda-nvrtc-cu12",
        "nvidia-cuda-runtime-cu12",
        "nvidia-cudnn-cu12",
        "nvidia-cufft-cu12",
        "nvidia-cufile-cu12",
        "nvidia-curand-cu12",
        "nvidia-cusolver-cu12",
        "nvidia-cusparse-cu12",
        "nvidia-cusparselt-cu12",
        "nvidia-nccl-cu12",
        "nvidia-nvjitlink-cu12",
        "nvidia-nvshmem-cu12",
        "nvidia-nvtx-cu12",
    }
    selected_nvidia_packages = {
        name for name in cuda_packages if name.startswith("nvidia-")
    }
    lock = load_toml("uv.lock")
    locked_packages = {
        (package["name"], package["version"]) for package in lock["package"]
    }

    assert cuda_packages["cuda-toolkit"] == "12.6.3"
    assert Version(cuda_packages["torch"]) >= Version("2.14.0+cu126")
    assert Version(cuda_packages["torch"]).local == "cu126"
    assert selected_nvidia_packages == expected_nvidia_packages
    assert not any("-cu13" in name for name in cuda_packages)
    assert set(cuda_packages.items()) <= locked_packages


def test_cuda_documentation_matches_the_supported_runtime_and_driver_contract():
    readme = read("README.md")
    readme_words = " ".join(readme.split())
    evaluation = read("docs/uv-evaluation.md")

    for expected in (
        "nvidia/cuda:12.6.3-cudnn-runtime-ubuntu22.04",
        "560.35.05",
        "525.60.13",
        "data-center GPUs",
        "select RTX SKUs",
        "Jetson systems",
    ):
        assert expected in readme_words
    assert "Do not treat those legacy branches as general GeForce support" in readme_words
    assert 'url = "https://download.pytorch.org/whl/cu126"' in evaluation
    assert "torch 2.14.0+cu126" in evaluation
    assert "current cu126/Torch 2.14 dependency" in evaluation
    assert 'url = "https://download.pytorch.org/whl/cu124"' not in evaluation
    assert "torch 2.6.0+cu124" not in evaluation


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
