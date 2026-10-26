"""After-the-fact transcription of a recorded session.

A record-only session leaves a WAV and no transcript. Processing it runs the
whole file through the backend in one pass and writes the same JSONL a live
session would have written, so history and the transcript view treat it alike.
"""

import re
import threading
import wave
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly

from transcript_events import process_transcript_text


class SessionProcessor:
    """Transcribes recorded sessions in the background, one job per session stem.

    Job state is held in memory, like the captions jobs: a restart forgets it,
    and a session left half-processed simply has no transcript yet.
    """

    STEM_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}$")
    STEM_FORMAT = "%Y-%m-%d_%H%M%S"
    BLOCK_SECONDS = 60  # resample in blocks so a long WAV is never held as float48k
    # Who each recording carries: system audio is the far end of the call, the mic is you.
    SOURCE_LABELS = {"system": "Remote", "mic": "You"}

    def __init__(self, backend, transcripts_dir: Path, *, language: str | None,
                 context: dict, active_stem: str) -> None:
        self._backend = backend
        self._dir = Path(transcripts_dir)
        self._language = language
        self._context = context
        self._active_stem = active_stem
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def can_process(self, stem: str) -> bool:
        """True for a finished recording that has audio and no transcript yet."""
        return (
            bool(self.STEM_PATTERN.match(stem))
            and stem != self._active_stem
            and self._audio_path(stem).exists()
            and not self._transcript_path(stem).exists()
        )

    def status(self, stem: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(stem)
            return dict(job) if job else None

    def start(self, stem: str) -> dict:
        """Begin processing `stem`. Raises ValueError when it is not processable."""
        with self._lock:
            job = self._jobs.get(stem)
            if job and job["status"] == "processing":
                return dict(job)
            if not self.can_process(stem):
                raise ValueError(f"Session {stem!r} cannot be processed")
            self._jobs[stem] = {"status": "processing", "message": "Starting..."}
        threading.Thread(target=self._run, args=(stem,), daemon=True).start()
        return self.status(stem)

    def _run(self, stem: str) -> None:
        final = self._transcript_path(stem)
        # Written beside the final file and renamed at the end, so a failure
        # leaves no partial transcript and the session stays processable.
        partial = final.with_suffix(".jsonl.part")
        try:
            # Each source is transcribed on its own, then merged by start time so
            # the transcript reads as the conversation happened.
            timeline = []
            for source, path in self._sources(stem):
                self._update(stem, message=f"Transcribing {source} audio...")
                segments, _ = self._backend.transcribe(
                    self._load_16k(path), language=self._language,
                    beam_size=5, vad_filter=True,
                )
                timeline += [(seg.start, source, seg.text) for seg in segments]
            timeline.sort(key=lambda item: item[0])

            started = datetime.strptime(stem, self.STEM_FORMAT)
            partial.unlink(missing_ok=True)
            count = 0
            for start, source, text in timeline:
                entry = process_transcript_text(
                    text, partial, context=self._context,
                    now=started + timedelta(seconds=start),
                    metadata={"source": source},
                )
                count += entry is not None
            if not partial.exists():
                partial.touch()
            partial.rename(final)
            self._update(stem, status="done", message=f"{count} segments")
        # The job dict is the only channel back to the page, so report anything that fails.
        except Exception as e:  # pylint: disable=broad-exception-caught
            partial.unlink(missing_ok=True)
            self._update(stem, status="error", message=str(e)[:300])
            print(f"Processing {stem} failed: {e}", flush=True)

    def _update(self, stem: str, **fields) -> None:
        with self._lock:
            self._jobs[stem].update(fields)

    @classmethod
    def _load_16k(cls, path: Path) -> np.ndarray:
        """Read a 48kHz mono 16-bit WAV and return it at 16kHz float32."""
        with wave.open(str(path), "rb") as wf:
            if wf.getframerate() != 48000 or wf.getnchannels() != 1:
                raise ValueError(f"Expected 48kHz mono audio in {path.name}")
            block = wf.getframerate() * cls.BLOCK_SECONDS
            parts = []
            while frames := wf.readframes(block):
                pcm = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32767.0
                parts.append(resample_poly(pcm, 1, 3).astype(np.float32))
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)

    def _sources(self, stem: str) -> list[tuple[str, Path]]:
        """The session's recordings as (source, path); the mic only when it was recorded."""
        sources = [("system", self._audio_path(stem))]
        mic = self._dir / f"{stem}.mic.wav"
        if mic.exists():
            sources.append(("mic", mic))
        return sources

    def _audio_path(self, stem: str) -> Path:
        return self._dir / f"{stem}.wav"

    def _transcript_path(self, stem: str) -> Path:
        return self._dir / f"{stem}.jsonl"
