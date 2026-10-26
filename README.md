# Live Transcriber

Native macOS demo that captures system audio, transcribes with Whisper, saves live transcript sessions, and exposes backend/frontend plugin hooks for reacting to transcript events. The live UI is a React/Tailwind app with a resizable transcript pane and a main plugin-output pane.

The existing video captions tool is still available at `/captions`.

## Scope

This is a single-user interview demo and code sample. It is designed to run as one
native macOS process, not as a durable, multi-process production service. Caption
jobs and their status are intentionally held in memory.

## Prerequisites

### Python 3.11+

```bash
python3 --version
```

### uv

`run.sh` uses `uv` to manage the repository's `.venv`.

```bash
brew install uv
uv --version
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

### BlackHole on macOS

The transcriber captures system audio through [BlackHole](https://existential.audio/blackhole/), a virtual audio loopback driver.

1. Install BlackHole 2ch:
   ```bash
   brew install blackhole-2ch
   ```
2. Create a Multi-Output Device in Audio MIDI Setup so you can hear audio and capture it through BlackHole.
3. Grant microphone access to your terminal app in System Settings.

## Quick Start

```bash
./run.sh
```

First run creates a Python virtual environment, installs Python dependencies,
enables the repository's tracked Git hooks, installs frontend dependencies,
builds the React app, and starts FastAPI at `http://localhost:8765`.

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
./run.sh --mic
./run.sh --mic "Shure MV5"
./run.sh --record-only --mic
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

Captures system audio in real time, transcribes Spanish with Whisper, saves transcript JSONL entries, and broadcasts transcript events to the React UI.

### Transcript History (`/history`)

Browse saved transcript sessions. Each session saves a `.jsonl` transcript and a `.wav` audio recording.

### Mic Recording (`--mic`)

Records a microphone to `transcripts/<session>.mic.wav` alongside the live session,
for example to keep your side of an interview. The mic is not transcribed live; it is
transcribed when the session is processed (see below). Bare `--mic` records the system default input; `--mic NAME` records the
first input device whose name contains `NAME`. `./run.sh --list-mics` lists the
devices it can record and marks the system default.

### Record-Only Mode (`--record-only`)

Records the session audio (and the mic, with `--mic`) without transcribing it live.
Afterward, open `/history` and press **Process** on the session to transcribe the
recording in one pass. The result is saved as the session's `.jsonl` transcript.
Process is offered for any finished session that has audio and no transcript. When
the session has a mic recording, it is transcribed too and merged in by time, and the
transcript view labels each line **Remote** (system audio) or **You** (mic). Wear
headphones: without them the mic also picks up the remote side, which then appears
twice.

### Video Captions (`/captions`)

Upload a video or audio file to generate subtitle files. This workflow is preserved from the previous app.

## Docker

The optional container configuration targets Linux with NVIDIA/CUDA and an ALSA
device at `/dev/snd`. Docker Desktop cannot pass macOS CoreAudio input through that
Linux device interface. Use native `./run.sh` for the Mac demo.

## Running Tests

```bash
.venv/bin/python -m pytest -q

cd frontend
npm test
```

The normal quick start enables the tracked hooks automatically. To enable them
without starting the app, run `uv sync --group dev` followed by
`./scripts/install-git-hooks.sh`. Commits then run staged lint and secret checks,
and commit messages must follow the Conventional Commits format.

The existing chunking test still verifies silence-based audio splitting against a real fixture:

```bash
.venv/bin/python tests/test_chunking.py
```
