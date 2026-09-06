import asyncio
import json
import os
import sys
import subprocess
import threading
import queue
import uuid
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd
from scipy.signal import resample_poly
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from language import LanguageOption
from plugin_loader import load_plugins
from transcript_events import process_transcript_text, queue_transcript_render_event
from transcription import get_backend

# Config from env vars (set by run.sh) with defaults
HOST = os.environ.get("TRANSLATOR_HOST", "127.0.0.1")
PORT = int(os.environ.get("TRANSLATOR_PORT", "8765"))
MODEL = os.environ.get("TRANSLATOR_MODEL", "small")
DEVICE_NAME = os.environ.get("TRANSLATOR_DEVICE", "BlackHole 2ch")
BACKEND = os.environ.get("TRANSLATOR_BACKEND", "faster-whisper")

# Fail here, on one clear line, rather than once per utterance forever: the
# transcribe call is wrapped in a broad except so a bad language would otherwise
# just print and retry, leaving the app connected and silently producing nothing.
try:
    LANGUAGE = LanguageOption.from_env(os.environ)
except ValueError as exc:
    sys.exit(f"translator: {exc}")

app = FastAPI()
templates = Jinja2Templates(directory="templates")
FRONTEND_DIST = Path("frontend/dist")

if (FRONTEND_DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

TRANSCRIPTS_DIR = Path("transcripts")
TRANSCRIPTS_DIR.mkdir(exist_ok=True)

CAPTIONS_DIR = Path("captions")
CAPTIONS_DIR.mkdir(exist_ok=True)

# Caption job tracking: job_id -> {status, progress, message, files}
caption_jobs: dict[str, dict] = {}

# Audio config
SAMPLE_RATE = 48000
CAPTURE_CHUNK = 0.25  # seconds per capture read (small for responsive silence detection)
SILENCE_THRESHOLD = 0.0005
MAX_UTTERANCE = 5   # seconds — force transcribe even without a pause
MIN_UTTERANCE = 0.5 # seconds — don't transcribe tiny fragments
SILENCE_CHUNKS_TO_SPLIT = 2  # consecutive silent chunks to trigger transcription (0.5 seconds)


def find_input_device(name: str) -> int:
    """Return the index of the first stereo-capable input device matching `name`."""
    for idx, device in enumerate(sd.query_devices()):
        if name in device["name"] and device["max_input_channels"] >= 2:
            return idx
    raise RuntimeError(f"Could not find '{name}' input device")


device_index = find_input_device(DEVICE_NAME)
print(f"Using audio device: {sd.query_devices(device_index)['name']} (index {device_index})")

# Transcription backend — loaded once at startup
backend = get_backend(BACKEND, MODEL)
print(f"Backend ready: {backend.name}", flush=True)

# Auto-save: one JSONL file per session, append each segment
session_start = datetime.now()
session_stem = session_start.strftime('%Y-%m-%d_%H%M%S')
transcript_file = TRANSCRIPTS_DIR / f"{session_stem}.jsonl"
audio_file = TRANSCRIPTS_DIR / f"{session_stem}.wav"
print(f"Transcript auto-saving to: {transcript_file}", flush=True)

# Open WAV file for appending audio chunks (48kHz mono 16-bit)
wav_writer = wave.open(str(audio_file), "wb")
wav_writer.setnchannels(1)
wav_writer.setsampwidth(2)  # 16-bit
wav_writer.setframerate(SAMPLE_RATE)
wav_lock = threading.Lock()

# Thread-safe queue for broadcasting
text_queue: queue.Queue[dict] = queue.Queue()

# Queue to pass recorded audio from capture thread to processing thread
audio_chunk_queue: queue.Queue[np.ndarray] = queue.Queue()


def is_silent(audio: np.ndarray) -> bool:
    return np.abs(audio).mean() < SILENCE_THRESHOLD


def find_quietest_cut(audio: np.ndarray, lookback_seconds: float = 1.0,
                      window_seconds: float = 0.05) -> int:
    """Return a sample index near the end of `audio` where amplitude is lowest.

    Used when we hit MAX_UTTERANCE without natural silence — we cut at the
    quietest spot in the last `lookback_seconds` instead of slicing mid-word.
    """
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


def load_audio_16k(path: Path) -> np.ndarray:
    """Load a WAV at 16kHz mono float32. The captions pipeline already extracts to this format."""
    with wave.open(str(path), "rb") as wf:
        assert wf.getframerate() == 16000, f"Expected 16kHz, got {wf.getframerate()}"
        assert wf.getnchannels() == 1
        frames = wf.readframes(wf.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32767.0


def extract_text(segments) -> str:
    return " ".join(seg.text for seg in segments if seg.text)


def audio_capture_loop():
    """Record audio using a persistent stream. Small reads for responsive silence detection."""
    chunk_frames = int(SAMPLE_RATE * CAPTURE_CHUNK)
    print(f"Audio capture started (device {device_index}, {CAPTURE_CHUNK}s reads)", flush=True)
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                        device=device_index) as stream:
        while True:
            try:
                audio, overflowed = stream.read(chunk_frames)
                if overflowed:
                    print("Warning: audio buffer overflowed", flush=True)
                audio = audio.flatten()

                # Save to WAV
                pcm = (audio * 32767).astype(np.int16)
                with wav_lock:
                    wav_writer.writeframes(pcm.tobytes())

                # Hand off to processing thread
                audio_chunk_queue.put(audio)

            # A dropped read must not end the capture thread.
            except Exception as e:  # pylint: disable=broad-exception-caught
                print(f"Audio capture error: {e}", flush=True)


def audio_process_loop():
    """Accumulate audio and transcribe on natural pauses or max duration."""
    print("Audio processing started (silence-based splitting)", flush=True)
    utterance_buf = np.zeros(0, dtype=np.float32)
    silent_count = 0
    has_speech = False

    while True:
        chunk = audio_chunk_queue.get()
        utterance_buf = np.concatenate([utterance_buf, chunk])
        duration = len(utterance_buf) / SAMPLE_RATE

        if is_silent(chunk):
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
            cut = find_quietest_cut(utterance_buf, lookback_seconds=1.0)
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
            # Resample 48kHz -> 16kHz for Whisper
            audio_16k = resample_poly(audio, 1, 3).astype(np.float32)

            # Single Whisper pass (greedy). LANGUAGE.code is None for auto-detect.
            segments, _ = backend.transcribe(
                audio_16k, language=LANGUAGE.code, beam_size=1
            )
            text = extract_text(segments)
            entry = process_transcript_text(
                text,
                transcript_file,
                context={"backend": backend.name, "model": MODEL},
            )
            if entry:
                queue_transcript_render_event(
                    entry,
                    text_queue,
                    context={"backend": backend.name, "model": MODEL},
                )

        # One bad utterance must not end the processing thread.
        except Exception as e:  # pylint: disable=broad-exception-caught
            print(f"Transcription error: {e}", flush=True)


# Connected WebSocket clients
clients: list[WebSocket] = []


@app.on_event("startup")
async def startup():
    loaded_plugins = load_plugins()
    if loaded_plugins:
        print(f"Loaded plugins: {', '.join(loaded_plugins)}", flush=True)
    # Capture thread: records audio continuously via sd.rec()
    capture = threading.Thread(target=audio_capture_loop, daemon=True)
    capture.start()
    # Processing thread: transcribes chunks from the queue
    process = threading.Thread(target=audio_process_loop, daemon=True)
    process.start()
    asyncio.create_task(broadcast_loop())


async def broadcast_loop():
    while True:
        try:
            entry = text_queue.get_nowait()
            msg = json.dumps({"type": "transcript", "event": entry})
            disconnected = []
            for ws in clients:
                try:
                    await ws.send_text(msg)
                # Any send failure means the socket is gone.
                except Exception:  # pylint: disable=broad-exception-caught
                    disconnected.append(ws)
            for ws in disconnected:
                clients.remove(ws)
        except queue.Empty:
            pass
        await asyncio.sleep(0.1)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    index_file = FRONTEND_DIST / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/history", response_class=HTMLResponse)
async def history(request: Request):
    files = sorted(TRANSCRIPTS_DIR.glob("*.jsonl"), reverse=True)
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
    return templates.TemplateResponse("history.html", {
        "request": request,
        "transcripts": transcripts,
    })


@app.get("/history/{filename}", response_class=HTMLResponse)
async def view_transcript(request: Request, filename: str):
    path = TRANSCRIPTS_DIR / filename
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
    audio_wav = TRANSCRIPTS_DIR / f"{stem}.wav"
    has_audio = audio_wav.exists()
    return templates.TemplateResponse("view.html", {
        "request": request,
        "label": label,
        "entries": entries,
        "has_audio": has_audio,
        "audio_url": f"/audio/{stem}.wav" if has_audio else None,
    })


@app.get("/audio/{filename}")
async def serve_audio(filename: str):
    if ".." in filename or "/" in filename:
        return JSONResponse({"error": "Invalid filename"}, status_code=400)
    path = TRANSCRIPTS_DIR / filename
    if not path.exists():
        return JSONResponse({"error": "File not found"}, status_code=404)
    return FileResponse(path, filename=filename, media_type="audio/wav")


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    clients.append(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        clients.remove(ws)


# --- Captions feature ---

def format_srt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(path: Path, entries: list[dict]):
    with open(path, "w", encoding="utf-8") as f:
        for i, entry in enumerate(entries, 1):
            f.write(f"{i}\n")
            f.write(f"{format_srt_time(entry['start'])} --> {format_srt_time(entry['end'])}\n")
            f.write(f"{entry['text']}\n\n")


def ensure_argos_packages(job: dict):
    """Install argostranslate language packages if not already present."""
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
            pkg = next(
                (p for p in available if p.from_code == from_code and p.to_code == to_code),
                None,
            )
            if pkg:
                pkg.install()


def extract_audio_16k(video_path: Path, audio_path: Path) -> str | None:
    """Extract mono 16kHz WAV from a video. Returns an error message, or None on success."""
    result = subprocess.run(
        ["ffmpeg", "-i", str(video_path), "-vn", "-acodec", "pcm_s16le",
         "-ar", "16000", "-ac", "1", str(audio_path), "-y"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return f"ffmpeg failed: {result.stderr[:200]}"
    return None


def label_segment_languages(segments: list[dict], audio_array: np.ndarray,
                            detected_lang: str, job: dict) -> None:
    """Set each segment's "language" by detecting on that segment's own audio slice."""
    for i, seg in enumerate(segments):
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


def build_srt_entries(segments: list[dict], job: dict) -> tuple[list[dict], list[dict]]:
    """Return (as-spoken entries, translated entries), each segment flipped es<->en."""
    # Lazy import: argostranslate is only needed by the captions pipeline.
    import argostranslate.translate  # pylint: disable=import-outside-toplevel

    original_entries = []
    translated_entries = []

    for i, seg in enumerate(segments):
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


def transcribe_segments(audio_array: np.ndarray, job: dict) -> tuple[list[dict], str]:
    """Transcribe a whole file. Returns (segment dicts, file-level detected language)."""
    segments_raw, detected_lang = backend.transcribe(
        audio_array, beam_size=5, vad_filter=True,
    )
    segments = [
        {"start": seg.start, "end": seg.end, "text": seg.text}
        for seg in segments_raw
    ]
    job.update(progress=45, message=f"Transcribed {len(segments)} segments...")
    return segments, detected_lang


def language_summary(segments: list[dict]) -> str:
    """Describe the language mix, e.g. "12 Spanish, 3 English"."""
    es_count = sum(1 for s in segments if s["language"] == "es")
    en_count = sum(1 for s in segments if s["language"] == "en")
    return f"{es_count} Spanish, {en_count} English"


def write_caption_files(job_dir: Path, stem: str, original_entries: list[dict],
                        translated_entries: list[dict]) -> list[str]:
    """Write both SRTs into the job directory and return their filenames."""
    original_srt = job_dir / f"{stem}.original.srt"
    translated_srt = job_dir / f"{stem}.translated.srt"
    write_srt(original_srt, original_entries)
    write_srt(translated_srt, translated_entries)
    return [original_srt.name, translated_srt.name]


def caption_worker(job_id: str, video_path: Path):
    job = caption_jobs[job_id]
    stem = video_path.stem
    job_dir = CAPTIONS_DIR / job_id

    try:
        # Step 1: Extract audio with ffmpeg
        job.update(status="processing", progress=10, message="Extracting audio...")
        audio_path = job_dir / "audio.wav"
        error = extract_audio_16k(video_path, audio_path)
        if error:
            job.update(status="error", message=error)
            return

        # Step 2: Load audio as numpy for transcription + per-segment language detection
        job.update(progress=15, message="Loading audio...")
        audio_array = load_audio_16k(audio_path)

        # Step 3: Transcribe with Whisper (auto language detection)
        job.update(progress=20, message="Transcribing audio...")
        segments, detected_lang = transcribe_segments(audio_array, job)

        if not segments:
            job.update(status="error", message="No speech detected in video")
            return

        # Step 4: Detect language per segment by slicing audio
        job.update(progress=50, message="Detecting language per segment...")
        label_segment_languages(segments, audio_array, detected_lang, job)

        summary = language_summary(segments)
        job.update(progress=65, message=f"Found {summary} segments...")

        # Step 5: Install argostranslate packages for both directions
        ensure_argos_packages(job)

        # Step 6: Build original SRT (as-spoken) and translated SRT (flipped)
        job.update(progress=70, message="Translating segments...")
        original_entries, translated_entries = build_srt_entries(segments, job)

        job.update(progress=92, message="Writing SRT files...")
        files = write_caption_files(job_dir, stem, original_entries, translated_entries)

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


@app.get("/captions", response_class=HTMLResponse)
async def captions_page(request: Request):
    return templates.TemplateResponse("captions.html", {"request": request})


@app.post("/captions/upload")
async def captions_upload(file: UploadFile = File(...)):
    job_id = uuid.uuid4().hex[:12]
    job_dir = CAPTIONS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    # Save uploaded video
    video_path = job_dir / file.filename
    with open(video_path, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)

    caption_jobs[job_id] = {
        "status": "queued",
        "progress": 0,
        "message": "Queued...",
        "files": [],
    }

    thread = threading.Thread(target=caption_worker, args=(job_id, video_path), daemon=True)
    thread.start()

    return JSONResponse({"job_id": job_id})


@app.get("/captions/status/{job_id}")
async def captions_status(job_id: str):
    job = caption_jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    return JSONResponse(job)


@app.get("/captions/download/{job_id}/{filename}")
async def captions_download(job_id: str, filename: str):
    # Prevent path traversal
    if ".." in filename or "/" in filename:
        return JSONResponse({"error": "Invalid filename"}, status_code=400)
    path = CAPTIONS_DIR / job_id / filename
    if not path.exists():
        return JSONResponse({"error": "File not found"}, status_code=404)
    return FileResponse(path, filename=filename, media_type="application/x-subrip")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)
