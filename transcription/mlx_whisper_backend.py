import numpy as np

from .base import Segment, TranscriptionBackend


# Map our short names to mlx-community HuggingFace repos
MLX_MODEL_MAP = {
    "tiny": "mlx-community/whisper-tiny-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
}


class MlxWhisperBackend(TranscriptionBackend):
    name = "mlx-whisper"

    def __init__(self, model_size: str):
        import mlx_whisper  # noqa: F401 — surface ImportError early

        self._repo = MLX_MODEL_MAP.get(model_size, model_size)
        print(f"Loading mlx-whisper ({self._repo})...", flush=True)
        # mlx_whisper lazy-loads the model on first transcribe() call. Warm it up
        # with a tiny silent buffer so startup-time cost is paid up front.
        import mlx_whisper as mw
        self._mw = mw
        try:
            silent = np.zeros(16000, dtype=np.float32)
            mw.transcribe(silent, path_or_hf_repo=self._repo, verbose=False)
        except Exception as e:
            print(f"Warning: mlx-whisper warmup failed: {e}", flush=True)

    def transcribe(
        self,
        audio: np.ndarray,
        *,
        language: str | None = None,
        beam_size: int = 1,
        vad_filter: bool = False,
    ) -> tuple[list[Segment], str]:
        # mlx-whisper ignores beam_size/vad_filter — these knobs are for parity only.
        result = self._mw.transcribe(
            audio.astype(np.float32),
            path_or_hf_repo=self._repo,
            language=language,
            verbose=False,
        )
        segments = [
            Segment(
                start=float(seg["start"]),
                end=float(seg["end"]),
                text=seg["text"].strip(),
            )
            for seg in result.get("segments", [])
        ]
        return segments, result.get("language", language or "")

    def detect_language(self, audio: np.ndarray) -> tuple[str, float]:
        # mlx-whisper has no separate detect_language; transcribe a short slice
        # without specifying language and read the detected one off the result.
        # Use the first 30s (Whisper's chunk length) for stable detection.
        slice_len = min(len(audio), 16000 * 30)
        result = self._mw.transcribe(
            audio[:slice_len].astype(np.float32),
            path_or_hf_repo=self._repo,
            verbose=False,
        )
        return result.get("language", ""), 1.0
