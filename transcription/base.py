from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np


@dataclass
class Segment:
    start: float
    end: float
    text: str


class TranscriptionBackend(ABC):
    """Backend-agnostic interface for Whisper transcription.

    All audio inputs are expected to be 16kHz float32 mono numpy arrays.
    """

    @abstractmethod
    def transcribe(
        self,
        audio: np.ndarray,
        *,
        language: str | None = None,
        beam_size: int = 1,
        vad_filter: bool = False,
    ) -> tuple[list[Segment], str]:
        """Transcribe audio. Returns (segments, detected_language)."""

    @abstractmethod
    def detect_language(self, audio: np.ndarray) -> tuple[str, float]:
        """Detect spoken language. Returns (language_code, probability)."""
