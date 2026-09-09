"""Bounded decoding and loading for caption audio."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import wave


class InvalidAudioMetadataError(ValueError):
    """The extracted caption audio does not match the decoding contract."""


class DecodedAudioTooLargeError(ValueError):
    """The extracted caption audio exceeds the configured PCM byte limit."""


@dataclass(frozen=True)
class DecodedAudioPolicy:
    """Build bounded decoder commands and safely load their PCM output."""

    max_pcm_bytes: int
    sample_rate: int = 16000
    channels: int = 1
    sample_width: int = 2

    @property
    def maximum_frames(self) -> int:
        """Return the number of complete PCM frames allowed in memory."""
        return self.max_pcm_bytes // (self.channels * self.sample_width)

    def ffmpeg_arguments(self, input_path: Path, output_path: Path) -> list[str]:
        """Return an extraction command capped at one frame beyond the limit."""
        probe_frames = self.maximum_frames + 1
        return [
            "ffmpeg", "-i", str(input_path), "-vn",
            "-af", f"aresample={self.sample_rate},atrim=end_sample={probe_frames}",
            "-acodec", "pcm_s16le", "-ar", str(self.sample_rate),
            "-ac", str(self.channels), str(output_path), "-y",
        ]

    def load(self, path: Path):
        """Validate size and format before reading decoded PCM into memory."""
        import numpy as np  # pylint: disable=import-outside-toplevel

        with wave.open(str(path), "rb") as wav_file:
            self._validate_format(wav_file)
            frame_count = wav_file.getnframes()
            expected_bytes = frame_count * self.channels * self.sample_width
            if expected_bytes > self.max_pcm_bytes:
                raise DecodedAudioTooLargeError(
                    f"Decoded audio exceeds {self.max_pcm_bytes} bytes. "
                    "Increase TRANSLATOR_MAX_DECODED_AUDIO_BYTES to accept longer media.",
                )
            frames = wav_file.readframes(frame_count + 1)
            actual_bytes = len(frames)
            if actual_bytes != expected_bytes:
                raise InvalidAudioMetadataError(
                    f"Invalid extracted audio frame data: got {actual_bytes} bytes, "
                    f"expected {expected_bytes}",
                )
        return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32767.0

    def _validate_format(self, wav_file: wave.Wave_read) -> None:
        checks = (
            ("sample rate", wav_file.getframerate(), self.sample_rate),
            ("channels", wav_file.getnchannels(), self.channels),
            ("sample width", wav_file.getsampwidth(), self.sample_width),
        )
        for label, actual, expected in checks:
            if actual != expected:
                raise InvalidAudioMetadataError(
                    f"Invalid extracted audio {label}: got {actual}, expected {expected}",
                )
