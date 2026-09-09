"""Configuration and resources owned by one running translator application."""

from __future__ import annotations

import asyncio
import json
import queue
import subprocess
import threading
import time
import uuid
import wave
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import WebSocket, WebSocketDisconnect, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from language import LanguageOption
from plugin_loader import load_plugins
from transcript_events import process_transcript_text, queue_transcript_render_event

if TYPE_CHECKING:
    import numpy as np

# Audio and model dependencies are only needed when their work starts.
# pylint: disable=import-outside-toplevel

SAMPLE_RATE = 48000
CAPTURE_CHUNK = 0.25
SILENCE_THRESHOLD = 0.0005
MAX_UTTERANCE = 5
MIN_UTTERANCE = 0.5
SILENCE_CHUNKS_TO_SPLIT = 2


@dataclass(frozen=True)
class RuntimeConfig:
    """Validated environment values; constructing these acquires no resources."""

    host: str
    port: int
    model: str
    device_name: str
    backend_name: str
    language: LanguageOption
    max_upload_bytes: int

    @classmethod
    def from_environment(cls, environ):
        values = {}
        for name, default in (
            ("HOST", "127.0.0.1"), ("MODEL", "small"),
            ("DEVICE", "BlackHole 2ch"), ("BACKEND", "faster-whisper"),
        ):
            value = environ.get(f"TRANSLATOR_{name}", default)
            if not value.strip():
                raise ValueError(f"TRANSLATOR_{name} must not be blank")
            values[name] = value
        try:
            port = int(environ.get("TRANSLATOR_PORT", "8765"))
        except ValueError as exc:
            raise ValueError("TRANSLATOR_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise ValueError("TRANSLATOR_PORT must be between 1 and 65535")
        try:
            max_upload_bytes = int(environ.get("TRANSLATOR_MAX_UPLOAD_BYTES", "1073741824"))
        except ValueError as exc:
            raise ValueError("TRANSLATOR_MAX_UPLOAD_BYTES must be a positive integer") from exc
        if max_upload_bytes <= 0:
            raise ValueError("TRANSLATOR_MAX_UPLOAD_BYTES must be a positive integer")
        if values["BACKEND"] not in ("faster-whisper", "mlx-whisper"):
            raise ValueError("TRANSLATOR_BACKEND must be 'faster-whisper' or 'mlx-whisper'")
        return cls(values["HOST"], port, values["MODEL"], values["DEVICE"],
                   values["BACKEND"], LanguageOption.from_env(environ), max_upload_bytes)


class UploadTooLargeError(Exception):
    """The uploaded file exceeds this runtime's configured byte limit."""


# The runtime owns the existing application surface and its resources together.
# Further separation of caption scheduling and backend access belongs to follow-up work.
# pylint: disable=too-many-instance-attributes,too-many-public-methods
class TranslatorRuntime:
    """One lifespan's audio, model, session, clients, and background work."""

    def __init__(self, config: RuntimeConfig):
        self.config = config
        self.transcripts_dir = Path("transcripts")
        self.captions_dir = Path("captions")
        self.frontend_dist = Path("frontend/dist")
        self.templates = Jinja2Templates(directory="templates")
        self.caption_jobs: dict[str, dict] = {}
        self.clients: list[WebSocket] = []
        self.text_queue: queue.Queue[dict] = queue.Queue()
        self.audio_chunk_queue: queue.Queue[np.ndarray] = queue.Queue()
        self.wav_lock = threading.Lock()
        self._transcript_commit_lock = threading.Lock()
        self._stopping = threading.Event()
        self.device_index = None
        self.backend = None
        self.transcript_file = None
        self.audio_file = None
        self.wav_writer = None
        self.audio_stream = None
        self.worker_threads: list[threading.Thread] = []
        self.broadcast_task = None
        self._started = False

    async def start(self):
        """Acquire resources only when FastAPI enters its lifespan."""
        import sounddevice as sd
        from transcription import get_backend

        if self._started or self._stopping.is_set():
            raise RuntimeError("A runtime can only be started once")
        self._started = True
        try:
            self.device_index = self.find_input_device(self.config.device_name)
            print(f"Using audio device: {self.config.device_name} (index {self.device_index})")
            self.backend = get_backend(self.config.backend_name, self.config.model)
            print(f"Backend ready: {self.backend.name}", flush=True)
            loaded_plugins = load_plugins()
            if loaded_plugins:
                print(f"Loaded plugins: {', '.join(loaded_plugins)}", flush=True)

            self.transcripts_dir.mkdir(exist_ok=True)
            self.captions_dir.mkdir(exist_ok=True)
            session_stem = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            self.transcript_file = self.transcripts_dir / f"{session_stem}.jsonl"
            self.audio_file = self.transcripts_dir / f"{session_stem}.wav"
            print(f"Transcript auto-saving to: {self.transcript_file}", flush=True)
            self.wav_writer = wave.open(str(self.audio_file), "wb")
            self.wav_writer.setnchannels(1)
            self.wav_writer.setsampwidth(2)
            self.wav_writer.setframerate(SAMPLE_RATE)
            self.audio_stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="float32", device=self.device_index,
            )
            self.audio_stream.start()
            for target in (self.audio_capture_loop, self.audio_process_loop):
                worker = threading.Thread(target=target, daemon=True)
                worker.start()
                self.worker_threads.append(worker)
            self.broadcast_task = asyncio.create_task(self.broadcast_loop())
        except BaseException:
            await self.stop()
            raise

    async def stop(self):
        """Signal workers and bound joins so an external inference cannot hang shutdown."""
        self._stopping.set()
        errors = []
        with self._capture_cleanup_error(errors):
            await asyncio.to_thread(self._wait_for_transcript_commit)
        if self.broadcast_task is not None:
            with self._capture_cleanup_error(errors):
                self.broadcast_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self.broadcast_task
        stream = self.audio_stream
        if stream is not None:
            with self._capture_cleanup_error(errors):
                stream.abort()
        with self._capture_cleanup_error(errors):
            await asyncio.to_thread(self._join_workers)
        # In-flight operations retain local references until they return.
        self.backend = None
        self.audio_stream = None
        if stream is not None:
            with self._capture_cleanup_error(errors):
                stream.close()
        with self._capture_cleanup_error(errors):
            with self.wav_lock:
                writer, self.wav_writer = self.wav_writer, None
                if writer is not None:
                    writer.close()
        clients, self.clients = self.clients[:], []
        for client in clients:
            with self._capture_cleanup_error(errors):
                await asyncio.wait_for(client.close(), timeout=1)
        if errors:
            raise errors[0]

    @contextmanager
    def _capture_cleanup_error(self, errors):
        """Defer a cleanup error until every remaining resource has been attempted."""
        try:
            yield
        except Exception as exc:  # pylint: disable=broad-exception-caught
            errors.append(exc)

    def _join_workers(self):
        deadline = time.monotonic() + 2
        errors = []
        for worker in self.worker_threads:
            with self._capture_cleanup_error(errors):
                worker.join(timeout=max(0, deadline - time.monotonic()))
                if worker.is_alive():
                    print(f"Worker still finishing during shutdown: {worker.name}", flush=True)
        if errors:
            raise errors[0]

    def _check_running(self):
        if self._stopping.is_set():
            raise RuntimeError("Runtime stopped")

    def _wait_for_transcript_commit(self):
        """Drain a write already in progress; callbacks never hold this lock."""
        with self._transcript_commit_lock:
            pass

    @contextmanager
    def _transcript_commit(self):
        with self._transcript_commit_lock:
            yield not self._stopping.is_set()

    def find_input_device(self, name: str) -> int:
        """Return the index of the first stereo-capable input device matching `name`."""
        import sounddevice as sd

        for idx, device in enumerate(sd.query_devices()):
            if name in device["name"] and device["max_input_channels"] >= 2:
                return idx
        raise RuntimeError(f"Could not find '{name}' input device")

    def is_silent(self, audio: np.ndarray) -> bool:
        import numpy as np

        return np.abs(audio).mean() < SILENCE_THRESHOLD

    def find_quietest_cut(self, audio: np.ndarray, lookback_seconds: float = 1.0,
                          window_seconds: float = 0.05) -> int:
        """Return a sample index near the end of `audio` where amplitude is lowest.

        Used when we hit MAX_UTTERANCE without natural silence — we cut at the
        quietest spot in the last `lookback_seconds` instead of slicing mid-word.
        """
        import numpy as np

        win = int(SAMPLE_RATE * window_seconds)
        lookback = int(SAMPLE_RATE * lookback_seconds)
        region = audio[-lookback:]
        if len(region) < win * 2:
            return len(audio)
        # Mean amplitude in non-overlapping windows
        n_windows = len(region) // win
        trimmed = region[: n_windows * win].reshape(n_windows, win)
        energies = np.abs(trimmed).mean(axis=1)
        quietest = int(np.argmin(energies))
        return len(audio) - lookback + quietest * win + win  # cut at end of quietest window

    def load_audio_16k(self, path: Path) -> np.ndarray:
        """Load mono 16kHz float32 audio extracted by the captions pipeline."""
        import numpy as np

        with wave.open(str(path), "rb") as wf:
            assert wf.getframerate() == 16000, f"Expected 16kHz, got {wf.getframerate()}"
            assert wf.getnchannels() == 1
            frames = wf.readframes(wf.getnframes())
        return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32767.0

    def extract_text(self, segments) -> str:
        return " ".join(seg.text for seg in segments if seg.text)

    def audio_capture_loop(self):
        """Read from the owned stream until shutdown aborts capture."""
        import numpy as np

        chunk_frames = int(SAMPLE_RATE * CAPTURE_CHUNK)
        print(f"Audio capture started (device {self.device_index}, "
              f"{CAPTURE_CHUNK}s reads)", flush=True)
        while not self._stopping.is_set():
            try:
                audio, overflowed = self.audio_stream.read(chunk_frames)
                if self._stopping.is_set():
                    break
                if overflowed:
                    print("Warning: audio buffer overflowed", flush=True)
                audio = audio.flatten()
                pcm = (audio * 32767).astype(np.int16)
                with self.wav_lock:
                    if self._stopping.is_set():
                        break
                    self.wav_writer.writeframes(pcm.tobytes())
                self.audio_chunk_queue.put(audio)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                if not self._stopping.is_set():
                    print(f"Audio capture error: {exc}", flush=True)

    def audio_process_loop(self):
        """Accumulate audio and transcribe on natural pauses or max duration."""
        import numpy as np

        print("Audio processing started (silence-based splitting)", flush=True)
        utterance_buf = np.zeros(0, dtype=np.float32)
        silent_count = 0
        has_speech = False

        while not self._stopping.is_set():
            try:
                chunk = self.audio_chunk_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            utterance_buf = np.concatenate([utterance_buf, chunk])
            duration = len(utterance_buf) / SAMPLE_RATE

            if self.is_silent(chunk):
                silent_count += 1
            else:
                silent_count = 0
                has_speech = True

            natural_break = (
                has_speech and silent_count >= SILENCE_CHUNKS_TO_SPLIT and duration >= MIN_UTTERANCE
            )
            forced_break = has_speech and duration >= MAX_UTTERANCE

            if not (natural_break or forced_break):
                continue

            if forced_break and not natural_break:
                # No silence found — find the quietest spot in the last ~1s and cut there
                # so we don't slice mid-word. The trailing audio carries forward.
                cut = self.find_quietest_cut(utterance_buf, lookback_seconds=1.0)
                audio = utterance_buf[:cut].copy()
                utterance_buf = utterance_buf[cut:].copy()
                # Don't reset has_speech — there's still speech in the carried tail
                silent_count = 0
            else:
                audio = utterance_buf.copy()
                utterance_buf = np.zeros(0, dtype=np.float32)
                silent_count = 0
                has_speech = False

            try:
                self._transcribe_audio(audio)
            # One bad utterance must not end the processing thread.
            except Exception as e:  # pylint: disable=broad-exception-caught
                print(f"Transcription error: {e}", flush=True)

    def _transcribe_audio(self, audio: np.ndarray):
        import numpy as np
        from scipy.signal import resample_poly

        backend = self.backend
        if self._stopping.is_set():
            return
        audio_16k = resample_poly(audio, 1, 3).astype(np.float32)
        segments, _ = backend.transcribe(
            audio_16k, language=self.config.language.code, beam_size=1,
        )
        text = self.extract_text(segments)
        if self._stopping.is_set():
            return
        context = {"backend": backend.name, "model": self.config.model}
        entry = process_transcript_text(
            text, self.transcript_file, context=context, commit_guard=self._transcript_commit,
        )
        if entry:
            queue_transcript_render_event(
                entry, self.text_queue, context=context, commit_guard=self._transcript_commit,
            )

    async def broadcast_loop(self):
        while True:
            try:
                entry = self.text_queue.get_nowait()
                msg = json.dumps({"type": "transcript", "event": entry})
                disconnected = []
                for ws in self.clients:
                    try:
                        await ws.send_text(msg)
                    # Any send failure means the socket is gone.
                    except Exception:  # pylint: disable=broad-exception-caught
                        disconnected.append(ws)
                for ws in disconnected:
                    if ws in self.clients:
                        self.clients.remove(ws)
            except queue.Empty:
                pass
            await asyncio.sleep(0.1)

    async def index(self, request: Request):
        index_file = self.frontend_dist / "index.html"
        if index_file.exists():
            return FileResponse(index_file)
        return self.templates.TemplateResponse(request=request, name="index.html")

    async def history(self, request: Request):
        files = sorted(self.transcripts_dir.glob("*.jsonl"), reverse=True)
        transcripts = []
        for f in files:
            entries = []
            for line in f.read_text().strip().splitlines():
                if line:
                    entries.append(json.loads(line))
            # Derive display name from filename: 2025-03-02_183045.jsonl
            stem = f.stem  # e.g. "2025-03-02_183045"
            try:
                dt = datetime.strptime(stem, "%Y-%m-%d_%H%M%S")
                label = dt.strftime("%B %d, %Y at %I:%M %p")
            except ValueError:
                label = stem
            transcripts.append({
                "filename": f.name,
                "label": label,
                "count": len(entries),
            })
        return self.templates.TemplateResponse(request=request, name="history.html", context={
            "request": request,
            "transcripts": transcripts,
        })

    async def view_transcript(self, request: Request, filename: str):
        path = self.transcripts_dir / filename
        if not path.exists() or not path.name.endswith(".jsonl"):
            return HTMLResponse("Not found", status_code=404)
        entries = []
        for line in path.read_text().strip().splitlines():
            if line:
                entries.append(json.loads(line))
        stem = path.stem
        try:
            dt = datetime.strptime(stem, "%Y-%m-%d_%H%M%S")
            label = dt.strftime("%B %d, %Y at %I:%M %p")
        except ValueError:
            label = stem
        audio_wav = self.transcripts_dir / f"{stem}.wav"
        has_audio = audio_wav.exists()
        return self.templates.TemplateResponse(request=request, name="view.html", context={
            "request": request,
            "label": label,
            "entries": entries,
            "has_audio": has_audio,
            "audio_url": f"/audio/{stem}.wav" if has_audio else None,
        })

    async def serve_audio(self, filename: str):
        if ".." in filename or "/" in filename:
            return JSONResponse({"error": "Invalid filename"}, status_code=400)
        path = self.transcripts_dir / filename
        if not path.exists():
            return JSONResponse({"error": "File not found"}, status_code=404)
        return FileResponse(path, filename=filename, media_type="audio/wav")

    async def websocket_endpoint(self, ws: WebSocket):
        await ws.accept()
        self.clients.append(ws)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            if ws in self.clients:
                self.clients.remove(ws)

    def format_srt_time(self, seconds: float) -> str:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        ms = int((seconds % 1) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    def write_srt(self, path: Path, entries: list[dict]):
        with open(path, "w", encoding="utf-8") as f:
            for i, entry in enumerate(entries, 1):
                f.write(f"{i}\n")
                f.write(f"{self.format_srt_time(entry['start'])} --> "
                        f"{self.format_srt_time(entry['end'])}\n")
                f.write(f"{entry['text']}\n\n")

    def ensure_argos_packages(self, job: dict):
        """Install argostranslate language packages if not already present."""
        self._check_running()
        import argostranslate.package  # pylint: disable=import-outside-toplevel

        installed = argostranslate.package.get_installed_packages()
        installed_pairs = {(p.from_code, p.to_code) for p in installed}
        needed = [("en", "es"), ("es", "en")]
        missing = [pair for pair in needed if pair not in installed_pairs]

        if missing:
            job.update(progress=job.get("progress", 0), message="Installing translation models...")
            argostranslate.package.update_package_index()
            available = argostranslate.package.get_available_packages()
            for from_code, to_code in missing:
                self._check_running()
                pkg = next(
                    (p for p in available if p.from_code == from_code and p.to_code == to_code),
                    None,
                )
                if pkg:
                    pkg.install()

    def extract_audio_16k(self, video_path: Path, audio_path: Path) -> str | None:
        """Extract mono 16kHz WAV from a video. Returns an error message, or None on success."""
        result = subprocess.run(
            ["ffmpeg", "-i", str(video_path), "-vn", "-acodec", "pcm_s16le",
             "-ar", "16000", "-ac", "1", str(audio_path), "-y"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            return f"ffmpeg failed: {result.stderr[:200]}"
        return None

    def label_segment_languages(self, segments: list[dict], audio_array: np.ndarray,
                                detected_lang: str, job: dict) -> None:
        """Set each segment's "language" by detecting on that segment's own audio slice."""
        backend = self.backend
        for i, seg in enumerate(segments):
            self._check_running()
            start_sample = int(seg["start"] * 16000)
            end_sample = int(seg["end"] * 16000)
            audio_slice = audio_array[start_sample:end_sample]

            # Need at least 0.5s of audio for reliable detection
            if len(audio_slice) < 8000:
                # Too short — fall back to file-level detection
                seg["language"] = detected_lang
            else:
                lang, _ = backend.detect_language(audio_slice)
                seg["language"] = lang

            if (i + 1) % 10 == 0:
                pct = 50 + int(15 * (i + 1) / len(segments))
                job.update(progress=pct, message=f"Detecting language... ({i+1}/{len(segments)})")

    def build_srt_entries(self, segments: list[dict], job: dict) -> tuple[list[dict], list[dict]]:
        """Return (as-spoken entries, translated entries), each segment flipped es<->en."""
        # Lazy import: argostranslate is only needed by the captions pipeline.
        import argostranslate.translate  # pylint: disable=import-outside-toplevel

        original_entries = []
        translated_entries = []

        for i, seg in enumerate(segments):
            self._check_running()
            original_entries.append({
                "start": seg["start"], "end": seg["end"], "text": seg["text"],
            })

            if seg["language"] == "es":
                # Spanish segment → translate to English
                text = argostranslate.translate.translate(seg["text"], "es", "en")
            else:
                # English (or other) segment → translate to Spanish
                text = argostranslate.translate.translate(seg["text"], "en", "es")
            translated_entries.append({
                "start": seg["start"], "end": seg["end"], "text": text,
            })

            if (i + 1) % 10 == 0:
                pct = 70 + int(20 * (i + 1) / len(segments))
                job.update(progress=pct, message=f"Translating... ({i+1}/{len(segments)})")

        return original_entries, translated_entries

    def transcribe_segments(self, audio_array: np.ndarray, job: dict) -> tuple[list[dict], str]:
        """Transcribe a whole file. Returns (segment dicts, file-level detected language)."""
        backend = self.backend
        self._check_running()
        segments_raw, detected_lang = backend.transcribe(
            audio_array, beam_size=5, vad_filter=True,
        )
        segments = [
            {"start": seg.start, "end": seg.end, "text": seg.text}
            for seg in segments_raw
        ]
        job.update(progress=45, message=f"Transcribed {len(segments)} segments...")
        return segments, detected_lang

    def language_summary(self, segments: list[dict]) -> str:
        """Describe the language mix, e.g. "12 Spanish, 3 English"."""
        es_count = sum(1 for s in segments if s["language"] == "es")
        en_count = sum(1 for s in segments if s["language"] == "en")
        return f"{es_count} Spanish, {en_count} English"

    def write_caption_files(self, job_dir: Path, stem: str, original_entries: list[dict],
                            translated_entries: list[dict]) -> list[str]:
        """Write both SRTs into the job directory and return their filenames."""
        original_srt = job_dir / f"{stem}.original.srt"
        translated_srt = job_dir / f"{stem}.translated.srt"
        self.write_srt(original_srt, original_entries)
        self.write_srt(translated_srt, translated_entries)
        return [original_srt.name, translated_srt.name]

    def caption_worker(self, job_id: str, video_path: Path):
        job = self.caption_jobs[job_id]
        job_dir = self.captions_dir / job_id

        try:
            self._check_running()
            # Step 1: Extract audio with ffmpeg
            job.update(status="processing", progress=10, message="Extracting audio...")
            audio_path = job_dir / "audio.wav"
            error = self.extract_audio_16k(video_path, audio_path)
            self._check_running()
            if error:
                job.update(status="error", message=error)
                return

            # Step 2: Load audio as numpy for transcription + per-segment language detection
            job.update(progress=15, message="Loading audio...")
            audio_array = self.load_audio_16k(audio_path)

            # Step 3: Transcribe with Whisper (auto language detection)
            job.update(progress=20, message="Transcribing audio...")
            segments, detected_lang = self.transcribe_segments(audio_array, job)
            self._check_running()

            if not segments:
                job.update(status="error", message="No speech detected in video")
                return

            # Step 4: Detect language per segment by slicing audio
            job.update(progress=50, message="Detecting language per segment...")
            self.label_segment_languages(segments, audio_array, detected_lang, job)
            self._check_running()

            summary = self.language_summary(segments)
            job.update(progress=65, message=f"Found {summary} segments...")

            # Step 5: Install argostranslate packages for both directions
            self.ensure_argos_packages(job)

            # Step 6: Build original SRT (as-spoken) and translated SRT (flipped)
            job.update(progress=70, message="Translating segments...")
            original_entries, translated_entries = self.build_srt_entries(segments, job)
            self._check_running()

            job.update(progress=92, message="Writing SRT files...")
            files = self.write_caption_files(
                job_dir, video_path.stem, original_entries, translated_entries,
            )

            # Clean up working files
            audio_path.unlink(missing_ok=True)
            video_path.unlink(missing_ok=True)

            job.update(
                status="done", progress=100, message="Done!",
                files=files,
                detected_language=summary,
            )

        # The job dict is the only channel back to the client, so report anything that fails.
        except Exception as e:  # pylint: disable=broad-exception-caught
            job.update(status="error", message=str(e)[:300])
            print(f"Caption job {job_id} error: {e}", flush=True)

    async def captions_page(self, request: Request):
        return self.templates.TemplateResponse(request=request, name="captions.html")

    async def _store_caption_upload(self, file: UploadFile, video_path: Path):
        """Copy bounded chunks, rejecting an oversized chunk before writing it."""
        size = 0
        with open(video_path, "xb") as destination:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > self.config.max_upload_bytes:
                    raise UploadTooLargeError(
                        f"Upload exceeds maximum size of {self.config.max_upload_bytes} bytes",
                    )
                destination.write(chunk)

    async def captions_upload(self, file: UploadFile):
        job_id = uuid.uuid4().hex[:12]
        job_dir = self.captions_dir / job_id
        video_path = job_dir / f"{uuid.uuid4().hex}.upload"
        created = False
        try:
            try:
                self._check_running()
                job_dir.mkdir(parents=True)
                created = True
                await self._store_caption_upload(file, video_path)
            finally:
                await file.close()

            self.caption_jobs[job_id] = {
                "status": "queued",
                "progress": 0,
                "message": "Queued...",
                "files": [],
                "original_filename": file.filename,
            }
            self._check_running()
            thread = threading.Thread(
                target=self.caption_worker, args=(job_id, video_path), daemon=True,
            )
            thread.start()
            self.worker_threads.append(thread)
        except BaseException as exc:
            if created:
                self.caption_jobs.pop(job_id, None)
                video_path.unlink(missing_ok=True)
                job_dir.rmdir()
            if isinstance(exc, UploadTooLargeError):
                return JSONResponse({"error": str(exc)}, status_code=413)
            raise
        return JSONResponse({"job_id": job_id})

    async def captions_status(self, job_id: str):
        job = self.caption_jobs.get(job_id)
        if not job:
            return JSONResponse({"error": "Job not found"}, status_code=404)
        return JSONResponse(job)

    async def captions_download(self, job_id: str, filename: str):
        # Prevent path traversal
        if ".." in filename or "/" in filename:
            return JSONResponse({"error": "Invalid filename"}, status_code=400)
        path = self.captions_dir / job_id / filename
        if not path.exists():
            return JSONResponse({"error": "File not found"}, status_code=404)
        return FileResponse(path, filename=filename, media_type="application/x-subrip")
