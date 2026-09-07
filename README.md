# Live Transcriber

Real-time transcription app that captures system audio, transcribes with Whisper, saves live transcript sessions, and exposes backend/frontend plugin hooks for reacting to transcript events. The live UI is a React/Tailwind app with a resizable transcript pane and a main plugin-output pane.

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

First run creates a Python virtual environment, installs Python dependencies, installs frontend dependencies, builds the React app, and starts FastAPI at `http://localhost:8765`.

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

### Video Captions (`/captions`)

Upload a video or audio file to generate subtitle files. This workflow is preserved from the previous app.

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
