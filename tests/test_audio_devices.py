"""Input selection contracts without PortAudio or microphone access."""

import importlib
import asyncio
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest


DEVICES = [
    {"name": "Built-in Speakers", "max_input_channels": 0},
    {"name": "MacBook Pro Microphone", "max_input_channels": 1},
    {"name": "BlackHole 2ch", "max_input_channels": 2},
    {"name": "USB Mic", "max_input_channels": 1},
    {"name": "USB Mic Pro", "max_input_channels": 2},
]


@pytest.mark.parametrize("default_input,expected", [(1, 1), ("usb mic", 3), ("BlackHole", 2)])
def test_selector_resolves_defaults_from_plain_data(default_input, expected):
    selector = importlib.import_module("audio_devices").AudioDeviceSelector(DEVICES, default_input)
    assert selector.select("default") == expected


@pytest.mark.parametrize("default_input", [
    True, False, -1, 99, None, 1.0, "default", "Mic", "gone",
])
def test_selector_rejects_invalid_default_with_choices(default_input):
    selector = importlib.import_module("audio_devices").AudioDeviceSelector(DEVICES, default_input)
    with pytest.raises(RuntimeError) as error:
        selector.select("default")
    assert "default input" in str(error.value)
    assert "Available input devices: 1: MacBook Pro Microphone" in str(error.value)


def test_selector_rejects_duplicate_exact_names():
    selector = importlib.import_module("audio_devices").AudioDeviceSelector([
        {"name": "USB Mic", "max_input_channels": 1},
        {"name": "USB MIC", "max_input_channels": 2},
    ])
    with pytest.raises(RuntimeError, match="ambiguous: 0: USB Mic.*1: USB MIC"):
        selector.select("usb mic")


@pytest.mark.parametrize("name", ["default", "Mic", " "])
def test_selector_reports_no_inputs(name):
    selector = importlib.import_module("audio_devices").AudioDeviceSelector([])
    with pytest.raises(RuntimeError, match="Available input devices: none"):
        selector.select(name)


def test_runtime_reads_indexable_sounddevice_default(monkeypatch):
    module = importlib.import_module("translator_runtime")

    class DefaultPair:  # pylint: disable=too-few-public-methods
        def __getitem__(self, index):
            return ("usb mic", 0)[index]

    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(
        query_devices=lambda: DEVICES, default=SimpleNamespace(device=DefaultPair()),
    ))
    runtime = module.TranslatorRuntime(module.RuntimeConfig.from_environment({}))
    assert runtime.find_input_device("default") == 3


@pytest.mark.parametrize("name,default_input,expected", [
    ("default", 1, 1), ("DeFaUlT", 3, 3), ("MacBook Pro Microphone", -1, 1),
    ("usb MIC", -1, 3), ("blackhole", -1, 2), ("MacBook", -1, 1),
])
def test_runtime_selects_default_or_unambiguous_input(monkeypatch, name, default_input, expected):
    module = importlib.import_module("translator_runtime")
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(
        query_devices=lambda: DEVICES, default=SimpleNamespace(device=(default_input, 0)),
    ))
    runtime = module.TranslatorRuntime(module.RuntimeConfig.from_environment({}))
    assert runtime.find_input_device(name) == expected


@pytest.mark.parametrize("name,default_input,reason", [
    ("missing", 1, "not found"), ("Built-in Speakers", 1, "input channels"),
    ("Mic", 1, "ambiguous"), ("default", -1, "default input"),
    ("default", 0, "input channels"), ("default", 99, "default input"),
    ("default", None, "default input"),
])
def test_input_errors_identify_reason_and_available_choices(
    monkeypatch, name, default_input, reason,
):
    module = importlib.import_module("translator_runtime")
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(
        query_devices=lambda: DEVICES, default=SimpleNamespace(device=(default_input, 0)),
    ))
    runtime = module.TranslatorRuntime(module.RuntimeConfig.from_environment({}))
    with pytest.raises(RuntimeError) as error:
        runtime.find_input_device(name)
    message = str(error.value)
    assert reason in message
    assert '1: MacBook Pro Microphone (1 input channel)' in message
    assert '2: BlackHole 2ch (2 input channels)' in message
    assert '3: USB Mic (1 input channel)' in message
    assert '--device' in message


@pytest.mark.parametrize("arguments,expected", [
    ([], "default"), (["--device", "USB Mic"], "USB Mic"),
])
def test_launcher_exports_selected_input(tmp_path, arguments, expected):
    """Execute the real launcher with only the external package runner replaced."""
    repository_root = Path(__file__).resolve().parents[1]
    shutil.copyfile(repository_root / "run.sh", tmp_path / "run.sh")
    for name in ("runtime_config.py", "language.py", "websocket_security.py"):
        shutil.copyfile(repository_root / name, tmp_path / name)
    (tmp_path / "scripts").mkdir()
    shutil.copyfile(
        repository_root / "scripts" / "validate_runtime_config.py",
        tmp_path / "scripts" / "validate_runtime_config.py",
    )
    runner = tmp_path / "uv"
    runner.write_text('#!/bin/sh\nif [ "$1" = run ]; then printenv TRANSLATOR_DEVICE; fi\n')
    runner.chmod(0o755)
    result = subprocess.run(
        ["bash", str(tmp_path / "run.sh"), *arguments], check=True, capture_output=True,
        text=True, env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
    )
    assert result.stdout.splitlines()[-1] == expected


@pytest.mark.parametrize("default", [None, SimpleNamespace(device=(-1, 0))])
def test_invalid_default_startup_fails_before_creating_resources(tmp_path, monkeypatch, default):
    module = importlib.import_module("translator_runtime")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(
        query_devices=lambda: DEVICES, default=default,
    ))

    def unexpected_backend(*_args):
        pytest.fail("Invalid device selection must fail before loading a model")

    monkeypatch.setitem(sys.modules, "transcription", SimpleNamespace(
        get_backend=unexpected_backend,
    ))
    runtime = module.TranslatorRuntime(module.RuntimeConfig.from_environment({}))
    with pytest.raises(RuntimeError, match="default input.*Available input devices"):
        asyncio.run(runtime.start())
    assert runtime.audio_stream is None
    assert runtime.wav_writer is None
    assert runtime.worker_threads == []
    assert not list(tmp_path.iterdir())
