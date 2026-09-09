"""Configuration and resources owned by one running translator application."""

from __future__ import annotations

import asyncio
import json
import queue
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import WebSocket, WebSocketDisconnect, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from audio_devices import AudioDeviceSelector
from audio_pipeline import (
    SAMPLE_RATE, CAPTURE_CHUNK, CAPTURE_QUEUE_CAPACITY, UTTERANCE_QUEUE_CAPACITY,
    DropOldestQueue, UtteranceChunker,
)
from caption_jobs import CaptionJobManager
from caption_translation import CaptionTranslationPolicy
from decoded_audio import DecodedAudioPolicy, DecodedAudioTooLargeError
from decoded_audio import InvalidAudioMetadataError  # pylint: disable=unused-import
from plugin_loader import load_plugins
from runtime_config import RuntimeConfig
from session_files import LiveSessionFiles
from transcript_events import process_transcript_text, queue_transcript_render_event
from transcript_history import TranscriptHistoryReader
from websocket_security import WebSocketOriginPolicy
from websocket_delivery import WebSocketDeliveryRegistry
from wav_recorder import RecordingError, RotatingWavRecorder  # pylint: disable=unused-import

if TYPE_CHECKING:
    import numpy as np

# Audio and model dependencies are only needed when their work starts.
# pylint: disable=import-outside-toplevel


class UploadTooLargeError(Exception):
    """The uploaded file exceeds this runtime's configured byte limit."""


# The runtime owns the existing application surface and its resources together.
# pylint: disable=too-many-instance-attributes,too-many-public-methods
class TranslatorRuntime:
    """One lifespan's audio, model, session, clients, and background work."""

    def __init__(self, config: RuntimeConfig):
        self.config = config
        self.origin_policy = WebSocketOriginPolicy(config.allowed_origins)
        self.transcripts_dir = Path("transcripts")
        self.captions_dir = Path("captions")
        self.frontend_dist = Path("frontend/dist")
        self.templates = Jinja2Templates(directory="templates")
        self.caption_manager = CaptionJobManager(self)
        self.caption_jobs = self.caption_manager.jobs
        self.caption_translation_policy = CaptionTranslationPolicy()
        self.decoded_audio = DecodedAudioPolicy(config.max_decoded_audio_bytes)
        self._backend_lock = threading.Lock()
        self._translation_lock = threading.Lock()
        self.client_deliveries = WebSocketDeliveryRegistry(config.websocket_queue_capacity)
        self.text_queue: queue.Queue[dict] = queue.Queue()
        self.audio_chunk_queue = DropOldestQueue(CAPTURE_QUEUE_CAPACITY, "capture")
        self.utterance_queue = DropOldestQueue(UTTERANCE_QUEUE_CAPACITY, "utterance")
        self.audio_chunker = UtteranceChunker()
        self.wav_lock = threading.Lock()
        self._transcript_commit_lock = threading.Lock()
        self._stopping = threading.Event()
        self.device_index = None
        self.backend = None
        self.transcript_file = None
        self.audio_file = None
        self.audio_recorder = None
        self._recording_status_event = None
        self._recording_status_broadcast = False
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
            self.backend = get_backend(self.config.backend_name, self.config.model)
            print(f"Backend ready: {self.backend.name}", flush=True)
            loaded_plugins = load_plugins()
            if loaded_plugins:
                print(f"Loaded plugins: {', '.join(loaded_plugins)}", flush=True)

            self.transcripts_dir.mkdir(exist_ok=True)
            self.captions_dir.mkdir(exist_ok=True)
            await asyncio.to_thread(self.caption_manager.sweep)
            self.caption_manager.start()
            session_files = LiveSessionFiles.reserve(self.transcripts_dir)
            self.transcript_file = session_files.transcript_path
            self.audio_file = session_files.audio_path
            print(f"Transcript auto-saving to: {self.transcript_file}", flush=True)
            self.audio_recorder = session_files.create_audio_recorder(
                self.config.recording_storage_bytes,
            )
            try:
                self.audio_recorder.open()
            except RecordingError as exc:
                self._publish_recording_error(exc)
            self.audio_stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="float32", device=self.device_index,
            )
            self.audio_stream.start()
            for target in (self.audio_capture_loop, self.audio_process_loop,
                           self.audio_transcription_loop):
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
        self.caption_manager.stop()
        self.audio_chunk_queue.close()
        self.utterance_queue.close()
        errors = []
        with self._capture_cleanup_error(errors):
            await asyncio.to_thread(self._wait_for_transcript_commit)
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
        try:
            with self.wav_lock:
                recorder, self.audio_recorder = self.audio_recorder, None
                if recorder is not None:
                    recorder.close()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            event = self._publish_recording_error(exc, enqueue=False)
            if event is not None:
                self.client_deliveries.broadcast(json.dumps(event))
                self._recording_status_broadcast = True
            errors.append(exc)
        if self.broadcast_task is not None:
            with self._capture_cleanup_error(errors):
                self.broadcast_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self.broadcast_task
        with self._capture_cleanup_error(errors):
            await self.client_deliveries.close_all()
        if errors:
            raise errors[0]

    @property
    def clients(self) -> list[WebSocket]:
        """Return the currently registered sockets for runtime observability."""
        return self.client_deliveries.websockets

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
        """Discover inputs and resolve the requested name or configured default."""
        import sounddevice as sd

        devices = sd.query_devices()
        default_input = None
        if name.strip().casefold() == "default":
            try:
                default_input = sd.default.device[0]
            except (AttributeError, IndexError, TypeError):
                pass
        selector = AudioDeviceSelector(devices, default_input)
        index = selector.select(name)
        print(f"Using audio device: {devices[index]['name']} (index {index})", flush=True)
        return index

    def load_audio_16k(self, path: Path) -> np.ndarray:
        """Load mono 16kHz float32 audio extracted by the captions pipeline."""
        return self.decoded_audio.load(path)

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
                    recorder = self.audio_recorder
                    if recorder is not None and recorder.enabled:
                        try:
                            recorder.write(pcm.tobytes())
                        except RecordingError as exc:
                            self._publish_recording_error(exc)
                self.audio_chunk_queue.put(audio)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                if not self._stopping.is_set():
                    print(f"Audio capture error: {exc}", flush=True)
                break

    def _publish_recording_error(self, error: Exception, enqueue: bool = True) -> dict | None:
        if self._recording_status_event is not None:
            return None
        print(f"Recording disabled: {error}", flush=True)
        event = {
            "type": "status",
            "status": "recording-error",
            "message": "Recording stopped; live transcription continues.",
        }
        self._recording_status_event = event
        self._recording_status_broadcast = False
        if enqueue:
            self.text_queue.put(event)
        return event

    def audio_process_loop(self):
        """Split capture promptly even while the backend is still transcribing."""
        print("Audio processing started (silence-based splitting)", flush=True)
        try:
            while not self._stopping.is_set():
                try:
                    chunk = self.audio_chunk_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                try:
                    if not self._stopping.is_set():
                        audio = self.audio_chunker.add(chunk)
                        if audio is not None:
                            self.utterance_queue.put(audio)
                        del audio
                finally:
                    self.audio_chunk_queue.task_done()
                    del chunk
        finally:
            self.audio_chunker.reset()

    def audio_transcription_loop(self):
        """Consume a bounded backlog without holding up capture or chunking."""
        while not self._stopping.is_set():
            try:
                audio = self.utterance_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self._transcribe_audio(audio)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                print(f"Transcription error: {exc}", flush=True)
            finally:
                self.utterance_queue.task_done()
                del audio

    def _transcribe_audio(self, audio: np.ndarray):
        import numpy as np
        from scipy.signal import resample_poly

        backend = self.backend
        if self._stopping.is_set():
            return
        audio_16k = resample_poly(audio, 1, 3).astype(np.float32)
        with self._backend_lock:
            if self._stopping.is_set():
                return
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
            except queue.Empty:
                await asyncio.sleep(0.1)
            else:
                try:
                    message = entry if entry.get("type") == "status" else {
                        "type": "transcript", "event": entry,
                    }
                    self.client_deliveries.broadcast(json.dumps(message))
                    if entry is self._recording_status_event:
                        self._recording_status_broadcast = True
                finally:
                    self.text_queue.task_done()
                await asyncio.sleep(0)

    async def index(self, request: Request):
        index_file = self.frontend_dist / "index.html"
        if index_file.exists():
            return FileResponse(index_file)
        return self.templates.TemplateResponse(request=request, name="index.html")

    async def history(self, request: Request):
        reader = TranscriptHistoryReader(
            self.transcripts_dir, self.config.history_session_limit,
            self.config.history_entry_limit,
        )
        history = await asyncio.to_thread(reader.list_sessions)
        return self.templates.TemplateResponse(request=request, name="history.html", context={
            "request": request,
            **history,
        })

    async def view_transcript(self, request: Request, filename: str):
        reader = TranscriptHistoryReader(
            self.transcripts_dir, self.config.history_session_limit,
            self.config.history_entry_limit,
        )
        detail = await asyncio.to_thread(reader.read_session, filename)
        if detail is None:
            return HTMLResponse("Not found", status_code=404)
        return self.templates.TemplateResponse(request=request, name="view.html", context={
            "request": request,
            **detail,
        })

    async def serve_audio(self, filename: str):
        if ".." in filename or "/" in filename:
            return JSONResponse({"error": "Invalid filename"}, status_code=400)
        reader = TranscriptHistoryReader(
            self.transcripts_dir, self.config.history_session_limit,
            self.config.history_entry_limit,
        )
        path = await asyncio.to_thread(reader.audio_file, filename)
        if path is None:
            return JSONResponse({"error": "File not found"}, status_code=404)
        return FileResponse(path, filename=filename, media_type="audio/wav")

    async def websocket_endpoint(self, ws: WebSocket):
        if not self.origin_policy.allows(ws.scope):
            await ws.close(code=1008)
            return
        await ws.accept()
        session = self.client_deliveries.register(ws)
        if (self._recording_status_event is not None
                and self._recording_status_broadcast):
            message = json.dumps(self._recording_status_event)
            if not session.enqueue(message):
                self.client_deliveries.detach(session, 1013)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            await self.client_deliveries.unregister(session)

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
        with self._translation_lock:
            self._ensure_argos_packages(job)

    def _ensure_argos_packages(self, job: dict):
        self._check_running()
        import argostranslate.package  # pylint: disable=import-outside-toplevel

        installed = argostranslate.package.get_installed_packages()
        installed_pairs = {(p.from_code, p.to_code) for p in installed}
        needed = self.caption_translation_policy.routes
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
            self.decoded_audio.ffmpeg_arguments(video_path, audio_path),
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
                with self._backend_lock:
                    self._check_running()
                    lang, _ = backend.detect_language(audio_slice)
                seg["language"] = lang

            if (i + 1) % 10 == 0:
                pct = 50 + int(15 * (i + 1) / len(segments))
                job.update(progress=pct, message=f"Detecting language... ({i+1}/{len(segments)})")

    def build_srt_entries(self, segments: list[dict], job: dict) -> tuple[list[dict], list[dict]]:
        """Return (as-spoken entries, translated entries), each segment flipped es<->en."""
        self.caption_translation_policy.validate_segments(segments)
        with self._translation_lock:
            self._check_running()
            return self._build_srt_entries(segments, job)

    def _build_srt_entries(self, segments: list[dict], job: dict):
        # Lazy import: argostranslate is only needed by the captions pipeline.
        import argostranslate.translate  # pylint: disable=import-outside-toplevel

        original_entries = []
        translated_entries = []

        for i, seg in enumerate(segments):
            self._check_running()
            original_entries.append({
                "start": seg["start"], "end": seg["end"], "text": seg["text"],
            })

            source, target = self.caption_translation_policy.route_for(seg["language"])
            text = argostranslate.translate.translate(seg["text"], source, target)
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
        with self._backend_lock:
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
        audio_path = job_dir / "audio.wav"

        try:
            self._check_running()
            # Step 1: Extract audio with ffmpeg
            job.update(status="processing", progress=10, message="Extracting audio...")
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

            self.caption_translation_policy.validate_segments(segments)

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

        except DecodedAudioTooLargeError as error:
            audio_path.unlink(missing_ok=True)
            video_path.unlink(missing_ok=True)
            job.update(status="error", message=str(error))
        # The job dict is the only channel back to the client, so report anything that fails.
        except Exception as e:  # pylint: disable=broad-exception-caught
            job.update(status="error", message=str(e)[:300])
            print(f"Caption job {job_id} error: {e}", flush=True)
        finally:
            self.caption_manager.complete(job_id)

    async def captions_page(self, request: Request):
        return self.templates.TemplateResponse(request=request, name="captions.html")

    async def _store_caption_upload(self, file: UploadFile, video_path: Path):
        """Copy bounded chunks, rejecting an oversized chunk before writing it."""
        size = 0
        with open(video_path, "xb") as destination:
            while chunk := await file.read(1024 * 1024):
                self._check_running()
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
                await asyncio.to_thread(self.caption_manager.sweep)
                if not self.caption_manager.reserve(job_id):
                    return JSONResponse(
                        {"error": "Caption capacity is full; retry after current jobs finish"},
                        status_code=429, headers={"Retry-After": "5"},
                    )
                job_dir.mkdir(parents=True)
                created = True
                await self._store_caption_upload(file, video_path)
            finally:
                await file.close()

            self._check_running()
            self.caption_manager.submit(job_id, video_path, file.filename)
        except BaseException as exc:
            try:
                if created:
                    video_path.unlink(missing_ok=True)
                    job_dir.rmdir()
            finally:
                self.caption_manager.cancel_upload(job_id)
            if isinstance(exc, UploadTooLargeError):
                return JSONResponse({"error": str(exc)}, status_code=413)
            raise
        return JSONResponse({"job_id": job_id})

    async def captions_status(self, job_id: str):
        await asyncio.to_thread(self.caption_manager.sweep)
        job = self.caption_jobs.get(job_id)
        if not job:
            return JSONResponse({"error": "Job not found"}, status_code=404)
        return JSONResponse(job)

    async def captions_download(self, job_id: str, filename: str):
        await asyncio.to_thread(self.caption_manager.sweep)
        # Prevent path traversal
        if ".." in filename or "/" in filename:
            return JSONResponse({"error": "Invalid filename"}, status_code=400)
        try:
            return await self.caption_manager.download_response(job_id, filename)
        except OSError:
            return JSONResponse({"error": "File not found"}, status_code=404)
