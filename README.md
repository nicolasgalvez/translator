# Live Transcriber

Real-time transcription app that captures an audio input, transcribes with Whisper, saves live transcript sessions, and exposes backend/frontend plugin hooks for reacting to transcript events. The live UI is a React/Tailwind app with a resizable transcript pane and a main plugin-output pane.

The existing video captions tool is still available at `/captions`.

## Prerequisites

### Python 3.10+

```bash
python3 --version
```

### Node.js 20+

Required to install and build the React frontend.

```bash
node --version
npm --version
```

### ffmpeg

Required for the video captions feature.

```bash
brew install ffmpeg
```

### Audio input on macOS

By default, the transcriber uses the system's default microphone, selected in
System Settings → Sound → Input. Mono microphones are supported. Grant microphone
access to your terminal app when macOS prompts, or enable it in System Settings →
Privacy & Security → Microphone and restart the terminal if necessary.

To capture system audio, install [BlackHole](https://existential.audio/blackhole/),
a virtual audio loopback driver:

1. Install BlackHole 2ch:
   ```bash
   brew install blackhole-2ch
   ```
2. Create a Multi-Output Device in Audio MIDI Setup so you can hear audio and capture it through BlackHole.
3. Start with `./run.sh --device "BlackHole 2ch"`.

## Quick Start

```bash
./run.sh
```

First run creates a Python virtual environment, installs Python dependencies, installs frontend dependencies, builds the React app, and starts FastAPI at `http://localhost:8765`.

`./run.sh` captures the default microphone. Use `--device "NAME"` to select a
different input (or `TRANSLATOR_DEVICE=NAME` when running `app.py` directly).
Names match without regard to case; an exact name takes priority, and a unique
substring is accepted. Ambiguous or unavailable selections report the available
input devices. Startup prints the selected name and index.

### Frontend Dev Server

```bash
./run.sh --frontend-dev
```

This starts Vite at `http://127.0.0.1:5173` and FastAPI at `http://localhost:8765`.

### Options

```bash
./run.sh --model large-v3
./run.sh --port 9000
./run.sh --host 0.0.0.0
./run.sh --device "MacBook Pro Microphone"
./run.sh --device "BlackHole 2ch"
./run.sh --backend mlx-whisper
./run.sh --skip-frontend-build
```

## Plugin Hooks

Backend plugins live in `plugins/` as either `plugins/name.py` or `plugins/name/__init__.py`. Importing a plugin should register hooks from `hooks.py`.

```python
from hooks import add_filter


def uppercase(event, context):
    event = event.copy()
    event["text"] = event["text"].upper()
    return event


add_filter("transcript.before_save", uppercase)
```

Available backend hooks:

- `transcript.before_save`
- `transcript.before_render`
- `transcript.after_save`
- `transcript.after_render`

Transcript events include:

- `id`
- `text`
- `time`
- optional `metadata`
- optional `render`

Frontend plugins register transcript filters and main-pane renderers through `frontend/src/plugins/registry.tsx`. The included `highlightKeyword` plugin highlights the word `important` and renders matching transcript events in the main pane.

## Features

### Live Transcriber (`/`)

Captures the selected audio input in real time, transcribes Spanish with Whisper, saves transcript JSONL entries, and broadcasts transcript events to the React UI.

Capture, chunking, and inference run in separate workers. Live audio buffering has
fixed limits at 48 kHz mono float32:

- Capture queue: 8 reads of 0.25 seconds (2 seconds, 384,000 bytes).
  The capture worker also holds its current 0.25-second read and PCM conversion.
- Chunker: 2 silent reads of pre-roll (0.5 seconds). During speech it retains
  less than 5 seconds between reads, briefly less than 5.25 seconds before a cut.
  It joins chunks only when emitting, with a temporary array under 1,008,000 bytes;
  forced cuts also copy the emitted audio and the carried remainder.
- Pending inference: 2 utterances, each at most 5 seconds (1,920,000 bytes total).
  One additional utterance can be actively transcribing (960,000 bytes at 48 kHz,
  plus its resampled 16 kHz input and backend working memory).

Speech splits after two silent reads once at least 0.5 seconds is available. At
5 seconds, the chunker cuts at the quietest 50 ms window in the last second and
carries the remainder forward. Full queues discard the oldest pending item so
the session catches up to current audio. This can omit words from live transcripts
under sustained overload; the captured WAV is written before queueing and retains
those captured samples. Runtime counters `audio_chunk_queue.dropped_count` and
`utterance_queue.dropped_count` count items discarded by overload. Warnings identify the queue
at the first drop and at cumulative counts 2, 4, 8, and so on. Shutdown discards
pending audio and prevents late inference from publishing transcript output.

The live-transcript WebSocket at `/ws` accepts browser connections whose Origin
matches the request Host, normalizing hostname casing, IPv6 addresses, and default
HTTP(S) ports. The trusted ASGI connection scheme determines the request origin
(`ws` maps to `http`; `wss` maps to `https`), including default ports. The frontend
development proxy preserves Host and the scheme and works without additional
configuration.

If a trusted proxy rewrites Host or terminates TLS while leaving a different
internal ASGI scheme, configure the browser's public origin explicitly:

```bash
TRANSLATOR_ALLOWED_ORIGINS=https://transcripts.example,http://127.0.0.1:5173 ./run.sh
```

This optional comma-separated list adds exact HTTP(S) origins. Entries cannot
contain paths (including a trailing `/`), queries, fragments, credentials, or
wildcards. Invalid entries fail configuration before audio or model startup.
An unset or blank value adds no origins. Forwarded headers do not grant trust.
Malformed, duplicate, opaque (`null`), and untrusted browser origins are rejected
before the socket is accepted or registered for transcripts (ASGI close code 1008;
the WebSocket server may report a denied handshake as HTTP 403).

Clients without an Origin header are allowed for non-browser integrations.
Origin protection is not authentication: non-browser clients can omit or forge
Origin, so restrict network access to trusted clients.

### Transcript History (`/history`)

Browse saved transcript sessions. Each session saves a `.jsonl` transcript and a `.wav` audio recording.

### Video Captions (`/captions`)

Upload a video or audio file to generate subtitle files. This workflow is preserved from the previous app.

Each file can be up to 1 GiB (1073741824 bytes). Set `TRANSLATOR_MAX_UPLOAD_BYTES`
to a positive integer number of bytes to override this limit, for example:

```bash
TRANSLATOR_MAX_UPLOAD_BYTES=524288000 ./run.sh
```

Files over the limit receive HTTP 413 and leave no caption job or partial upload.
The complete request body is also capped before multipart parsing at the configured
file limit plus 64 KiB (65536 bytes) for multipart headers, boundaries, and fields.
This request cap counts actual streamed bytes even without a reliable Content-Length;
an advertised Content-Length above the cap is rejected before the body is read.
Uploaded files and generated subtitles use server-generated storage names; the
original upload filename is retained as job metadata.

Caption processing uses one worker and two waiting slots by default. Uploads in
progress also occupy slots; a fourth unfinished upload receives HTTP 429 with a
`Retry-After` header. Set `TRANSLATOR_CAPTION_CONCURRENCY` and
`TRANSLATOR_CAPTION_QUEUE_CAPACITY` to positive integers to change these limits.
Whisper inference and Argos translation resources are serialized across workers;
live transcription shares the same Whisper lock.

Completed and failed jobs, including their files, expire after 24 hours. Set
`TRANSLATOR_CAPTION_RETENTION_SECONDS` to a positive integer to change retention.
Cleanup runs at startup, on caption requests, and at least once per minute.
Expired job status and downloads return HTTP 404. Old generated job directories
from prior runs are also removed. Jobs are held in memory: restarting cancels
unfinished work and does not restore prior job status.
Downloads require a retained job record, so prior-run or expired artifacts are
unavailable even if a filesystem error delays their deletion; cleanup retries
those old generated directories on later sweeps.

## Docker

```bash
docker compose up --build
```

Requires NVIDIA GPU runtime for CUDA acceleration. Falls back to CPU if unavailable.

## Running Tests

```bash
uv run --only-group dev python -m unittest discover -s tests -p 'test_hooks.py'
uv run --only-group dev python -m unittest discover -s tests -p 'test_transcript_events.py'

cd frontend
npm test
```

The existing chunking test still verifies silence-based audio splitting against a real fixture:

```bash
uv run python tests/test_chunking.py
```
