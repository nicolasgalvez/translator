# Backends import their engine lazily so an absent optional one — mlx-whisper off
# Apple Silicon, faster-whisper without CUDA — does not break importing this package.
# pylint: disable=import-outside-toplevel

from .base import Segment, TranscriptionBackend


def get_backend(name: str, model_size: str) -> TranscriptionBackend:
    """Factory: instantiate a transcription backend by name."""
    if name == "faster-whisper":
        from .faster_whisper_backend import FasterWhisperBackend
        return FasterWhisperBackend(model_size)
    if name == "mlx-whisper":
        from .mlx_whisper_backend import MlxWhisperBackend
        return MlxWhisperBackend(model_size)
    raise ValueError(f"Unknown backend: {name!r}. Choose 'faster-whisper' or 'mlx-whisper'.")


__all__ = ["Segment", "TranscriptionBackend", "get_backend"]
