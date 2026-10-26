"""Record-only capture of a microphone alongside the live session.

The live loop transcribes system audio. The mic is a different job: it is kept
as a record of the session, written to its own WAV, and never transcribed.
"""

import queue
import threading
import wave
from pathlib import Path

import numpy as np


class MicRecorder:
    """Records one input device to a 16-bit mono WAV until stopped.

    `default` means the system's current default input, so whichever mic is
    selected in System Settings is the one recorded. Any other value matches the
    first input device whose name contains it.

    Raises ValueError at construction when the device cannot be found, so a bad
    name fails at startup instead of after the session has begun.
    """

    DEFAULT = "default"
    ENV_VAR = "TRANSLATOR_MIC"

    def __init__(self, device: str, path: Path, sample_rate: int = 48000, sd=None) -> None:
        if sd is None:
            # Imported here so the module loads, and tests run, without PortAudio.
            import sounddevice as sd  # pylint: disable=import-outside-toplevel
        self._sd = sd
        self.path = Path(path)
        self.sample_rate = sample_rate
        self.device_index = self._resolve(device)
        self._chunks: queue.Queue[np.ndarray | None] = queue.Queue()
        self._stream = None
        self._writer_thread: threading.Thread | None = None

    @classmethod
    def from_env(cls, environ, path: Path, **kwargs) -> "MicRecorder | None":
        """Build from an environment mapping. Absent or blank means no mic recording."""
        device = (environ.get(cls.ENV_VAR) or "").strip()
        return cls(device, path, **kwargs) if device else None

    @classmethod
    def available_inputs(cls, sd=None) -> list[tuple[str, bool]]:
        """Every device that can record, as (name, is_system_default) pairs."""
        if sd is None:
            import sounddevice as sd  # pylint: disable=import-outside-toplevel
        default_index = sd.default.device[0]
        return [
            (info["name"], index == default_index)
            for index, info in enumerate(sd.query_devices())
            if info["max_input_channels"] >= 1
        ]

    @property
    def device_name(self) -> str:
        return self._sd.query_devices(self.device_index)["name"]

    def start(self) -> None:
        """Open the device and begin writing to `path`."""
        self._writer_thread = threading.Thread(target=self._write_loop, daemon=True)
        self._writer_thread.start()
        self._stream = self._sd.InputStream(
            samplerate=self.sample_rate, channels=1, dtype="float32",
            device=self.device_index, callback=self._on_audio,
        )
        self._stream.start()

    def stop(self) -> None:
        """Close the device, flush what is queued, and finalize the WAV."""
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        if self._writer_thread is not None:
            self._chunks.put(None)
            self._writer_thread.join()
            self._writer_thread = None

    def _on_audio(self, indata, _frames, _time, status) -> None:
        # Runs on the PortAudio thread: hand off a copy and return, no file I/O here.
        if status:
            print(f"Mic recorder: {status}", flush=True)
        self._chunks.put(indata[:, 0].copy())

    def _write_loop(self) -> None:
        wav = wave.open(str(self.path), "wb")  # pylint: disable=consider-using-with
        try:
            wav.setnchannels(1)
            wav.setsampwidth(2)  # 16-bit
            wav.setframerate(self.sample_rate)
            while (chunk := self._chunks.get()) is not None:
                pcm = (np.clip(chunk, -1.0, 1.0) * 32767).astype(np.int16)
                wav.writeframes(pcm.tobytes())
        finally:
            wav.close()

    def _resolve(self, device: str) -> int:
        if device.lower() == self.DEFAULT:
            index = self._sd.default.device[0]
            if index is None or index < 0:
                raise ValueError("No default input device is set")
            return index
        for index, info in enumerate(self._sd.query_devices()):
            if device in info["name"] and info["max_input_channels"] >= 1:
                return index
        raise ValueError(f"Could not find mic input device {device!r}")


if __name__ == "__main__":
    for name, is_default in MicRecorder.available_inputs():
        print(f"{'*' if is_default else ' '} {name}")
    print("\n* = system default (what bare --mic records)")
