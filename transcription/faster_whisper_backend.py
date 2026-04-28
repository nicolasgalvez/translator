import numpy as np

from .base import Segment, TranscriptionBackend


class FasterWhisperBackend(TranscriptionBackend):
    name = "faster-whisper"

    def __init__(self, model_size: str):
        from faster_whisper import WhisperModel
        import ctranslate2

        try:
            if "cuda" in ctranslate2.get_supported_compute_types("cuda"):
                device, compute_type = "cuda", "float16"
            else:
                device, compute_type = "cpu", "int8"
        except ValueError:
            device, compute_type = "cpu", "int8"

        print(f"Loading faster-whisper ({model_size}) on {device} ({compute_type})...", flush=True)
        self._model = WhisperModel(model_size, device=device, compute_type=compute_type)
        self._device = device
        self._compute_type = compute_type

    def transcribe(
        self,
        audio: np.ndarray,
        *,
        language: str | None = None,
        beam_size: int = 1,
        vad_filter: bool = False,
    ) -> tuple[list[Segment], str]:
        segments_iter, info = self._model.transcribe(
            audio,
            task="transcribe",
            language=language,
            beam_size=beam_size,
            vad_filter=vad_filter,
        )
        segments = [
            Segment(start=seg.start, end=seg.end, text=seg.text.strip())
            for seg in segments_iter
        ]
        return segments, info.language

    def detect_language(self, audio: np.ndarray) -> tuple[str, float]:
        lang, prob, _ = self._model.detect_language(audio=audio)
        return lang, prob
