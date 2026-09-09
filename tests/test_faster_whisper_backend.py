"""Device-selection contracts for the Faster Whisper backend."""

import sys
from types import SimpleNamespace

from transcription.faster_whisper_backend import FasterWhisperBackend


def test_uses_cuda_float16_when_ctranslate2_reports_it(monkeypatch):
    """Select CUDA when CTranslate2 lists float16 as a supported compute type."""
    selected = {}

    class WhisperModel:  # pylint: disable=too-few-public-methods
        """Record the device configuration without loading a model."""

        def __init__(self, _model_size, *, device, compute_type):
            selected.update(device=device, compute_type=compute_type)

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=WhisperModel))
    monkeypatch.setitem(
        sys.modules,
        "ctranslate2",
        SimpleNamespace(
            get_supported_compute_types=lambda _device: {
                "float32",
                "float16",
                "int8_float16",
                "int8",
            }
        ),
    )

    FasterWhisperBackend("small")

    assert selected == {"device": "cuda", "compute_type": "float16"}
