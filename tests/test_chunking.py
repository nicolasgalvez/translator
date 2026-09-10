"""
Integration test: verify that the silence-based chunking method captures
complete utterances from a real Spanish conversation without dropping words.

Uses tests/fixtures/conversation_es.wav (48kHz mono, ~70s of Spanish dialogue).
"""

import os
import wave

import numpy as np
from scipy.signal import resample_poly
from faster_whisper import WhisperModel

# Mirror the constants from app.py (can't import app directly — it has
# module-level side effects like audio device init and model loading)
SAMPLE_RATE = 48000
CAPTURE_CHUNK = 0.25
SILENCE_THRESHOLD = 0.0005
MAX_UTTERANCE = 5
MIN_UTTERANCE = 0.5
SILENCE_CHUNKS_TO_SPLIT = 2

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "conversation_es.wav")

# Key phrases that MUST appear in the combined transcript.
# These span natural chunk boundaries in the old fixed-window approach.
REQUIRED_PHRASES = [
    "cómo estás",
    "cómo te llamas",
    "Brenda",
    "Romina",
    "Argentina",
    "Buenos Aires",
    "oficina",
    "Banco de México",
    "abogada",
    "contadora",
    "placer conocerte",
]


def load_wav_48k(path: str) -> np.ndarray:
    """Load a 48kHz mono WAV as float32 array."""
    with wave.open(path, "rb") as wf:
        assert wf.getframerate() == SAMPLE_RATE
        assert wf.getnchannels() == 1
        frames = wf.readframes(wf.getnframes())
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32767.0
    return audio


def simulate_chunking(audio: np.ndarray) -> list[np.ndarray]:
    """
    Simulate the silence-based chunking from audio_process_loop.
    Splits audio into 0.5s capture chunks, accumulates until silence or max duration.
    Returns list of utterance arrays ready for Whisper.
    """
    chunk_samples = int(SAMPLE_RATE * CAPTURE_CHUNK)
    utterances = []
    buf = np.zeros(0, dtype=np.float32)
    silent_count = 0
    has_speech = False

    for start in range(0, len(audio), chunk_samples):
        chunk = audio[start:start + chunk_samples]
        if len(chunk) < chunk_samples:
            # Pad the last chunk
            chunk = np.pad(chunk, (0, chunk_samples - len(chunk)))

        buf = np.concatenate([buf, chunk])
        duration = len(buf) / SAMPLE_RATE

        is_silent = np.abs(chunk).mean() < SILENCE_THRESHOLD
        if is_silent:
            silent_count += 1
        else:
            silent_count = 0
            has_speech = True

        should_transcribe = (
            (has_speech and silent_count >= SILENCE_CHUNKS_TO_SPLIT and duration >= MIN_UTTERANCE)
            or (has_speech and duration >= MAX_UTTERANCE)
        )

        if should_transcribe:
            utterances.append(buf.copy())
            buf = np.zeros(0, dtype=np.float32)
            silent_count = 0
            has_speech = False

    # Flush any remaining audio with speech
    if has_speech and len(buf) > 0:
        utterances.append(buf.copy())

    return utterances


def transcribe_utterances(utterances: list[np.ndarray], model: WhisperModel) -> list[str]:
    """Transcribe each utterance and return list of text strings."""
    texts = []
    for audio in utterances:
        audio_16k = resample_poly(audio, 1, 3).astype(np.float32)
        segments, _ = model.transcribe(
            audio_16k, task="transcribe", language="es", beam_size=1,
        )
        text = " ".join(seg.text.strip() for seg in segments if seg.text.strip())
        if text:
            texts.append(text)
    return texts


def test_chunking_captures_all_phrases():
    """Silence-based chunking should produce utterances that contain all key phrases."""
    audio = load_wav_48k(FIXTURE)
    utterances = simulate_chunking(audio)

    # Should produce multiple utterances (not one giant blob or 100 tiny fragments)
    assert len(utterances) >= 5, f"Expected at least 5 utterances, got {len(utterances)}"
    assert len(utterances) <= 40, f"Expected at most 40 utterances, got {len(utterances)}"

    # Check utterance durations are reasonable
    for i, utt in enumerate(utterances):
        dur = len(utt) / SAMPLE_RATE
        assert dur >= MIN_UTTERANCE, f"Utterance {i} too short: {dur:.1f}s"
        assert dur <= MAX_UTTERANCE + CAPTURE_CHUNK, f"Utterance {i} too long: {dur:.1f}s"

    # Transcribe
    model = WhisperModel("small", device="cpu", compute_type="int8")
    texts = transcribe_utterances(utterances, model)
    full_transcript = " ".join(texts).lower()

    print(f"\n--- Chunked into {len(utterances)} utterances, {len(texts)} with text ---")
    for i, t in enumerate(texts):
        print(f"  [{i+1}] {t}")
    print("\n--- Checking required phrases ---")

    missing = []
    for phrase in REQUIRED_PHRASES:
        if phrase.lower() not in full_transcript:
            missing.append(phrase)
            print(f"  MISSING: {phrase}")
        else:
            print(f"  OK: {phrase}")

    assert not missing, f"Missing phrases in transcript: {missing}"


def test_no_utterance_exceeds_max():
    """No utterance should exceed MAX_UTTERANCE (plus one capture chunk of slack)."""
    audio = load_wav_48k(FIXTURE)
    utterances = simulate_chunking(audio)
    max_allowed = MAX_UTTERANCE + CAPTURE_CHUNK
    for i, utt in enumerate(utterances):
        dur = len(utt) / SAMPLE_RATE
        assert dur <= max_allowed, f"Utterance {i} is {dur:.1f}s, max allowed {max_allowed}s"


if __name__ == "__main__":
    test_chunking_captures_all_phrases()
    test_no_utterance_exceeds_max()
    print("\nAll tests passed!")
