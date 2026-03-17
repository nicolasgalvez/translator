# Bilingual Live Translator

Real-time Spanish-to-English translator that captures system audio, transcribes with Whisper, and translates via argostranslate. Includes transcript history with audio playback and a video captions tool that generates dual SRT subtitle files.

## Prerequisites

### Python 3.10+

```bash
python3 --version
```

### ffmpeg

Required for the video captions feature.

```bash
# macOS
brew install ffmpeg
```

### BlackHole (virtual audio device)

The translator captures system audio through [BlackHole](https://existential.audio/blackhole/), a virtual audio loopback driver.

1. **Install BlackHole 2ch**:
   ```bash
   brew install blackhole-2ch
   ```

2. **Create a Multi-Output Device** (so you hear audio AND it gets captured):
   - Open **Audio MIDI Setup** (Spotlight → "Audio MIDI Setup")
   - Click **+** in the bottom left → **Create Multi-Output Device**
   - Check both your speakers/headphones AND **BlackHole 2ch**
   - Right-click the new device → **Use This Device For Sound Output**

3. **Grant microphone access**:
   - The first time you run the app, macOS will prompt for microphone permission for your terminal (Terminal.app, iTerm2, etc.)
   - If you denied it or it didn't prompt: **System Settings → Privacy & Security → Microphone** → toggle on your terminal app
   - You may need to restart your terminal after granting access

### macOS Security Settings

If you see `PortAudio` or `sounddevice` errors about device access:

- **System Settings → Privacy & Security → Microphone** — ensure your terminal is listed and enabled
- **System Settings → Privacy & Security → Input Monitoring** — add your terminal if audio capture still fails
- After changing permissions, **restart your terminal**

## Quick Start

```bash
./run.sh
```

First run creates a virtual environment and installs dependencies. The app runs at **http://localhost:8765**.

### Options

```
./run.sh --model large-v3    # best accuracy (needs more RAM)
./run.sh --port 9000         # different port
./run.sh --device "MacBook Pro Microphone"  # different audio input
```

### Whisper Models

| Model | Speed | RAM | Notes |
|-------|-------|-----|-------|
| `tiny` | Fastest | ~1GB | Low accuracy |
| `base` | Fast | ~1GB | Decent |
| `small` | Balanced | ~2GB | Default |
| `medium` | Slower | ~5GB | Better accuracy |
| `large-v3` | Slowest | ~10GB | Best accuracy, needs GPU for real-time |

## Features

### Live Translator (`/`)

Captures system audio in real-time, transcribes Spanish, and shows the English translation side-by-side. Uses silence-based chunking to capture complete utterances without cutting words at boundaries.

### Transcript History (`/history`)

Browse past sessions. Each session saves a `.jsonl` transcript and a `.wav` audio recording. Click into any session to view the transcript with an audio player.

### Video Captions (`/captions`)

Upload a video file to generate dual subtitle files:
- `{name}.original.srt` — subtitles in the spoken language (as-is)
- `{name}.translated.srt` — subtitles translated to the other language

Supports mixed Spanish/English audio with per-segment language detection.

## Docker

```bash
docker compose up --build
```

Requires NVIDIA GPU runtime for CUDA acceleration. Falls back to CPU if unavailable.

## Running Tests

```bash
source .venv/bin/activate
python tests/test_chunking.py
```

The chunking test verifies that the silence-based audio splitting captures complete utterances from a Spanish conversation without dropping words.
