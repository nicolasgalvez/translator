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

from audio_pipeline import (
    SAMPLE_RATE, CAPTURE_CHUNK, MAX_UTTERANCE, MIN_UTTERANCE, UtteranceChunker,
)

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


def chunk_audio(audio: np.ndarray) -> list[np.ndarray]:
    """Feed the real production chunker, including an end-of-file silence pause."""
    chunk_samples = int(SAMPLE_RATE * CAPTURE_CHUNK)
    utterances = []
    chunker = UtteranceChunker()

    for start in range(0, len(audio), chunk_samples):
        chunk = audio[start:start + chunk_samples]
        if len(chunk) < chunk_samples:
            # Pad the last chunk
            chunk = np.pad(chunk, (0, chunk_samples - len(chunk)))

        output = chunker.add(chunk)
        if output is not None:
            utterances.append(output)
    for _ in range(2):
        output = chunker.add(np.zeros(chunk_samples, dtype=np.float32))
        if output is not None:
            utterances.append(output)

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
    utterances = chunk_audio(audio)

    # Should produce multiple utterances (not one giant blob or 100 tiny fragments)
    assert len(utterances) >= 5, f"Expected at least 5 utterances, got {len(utterances)}"
    assert len(utterances) <= 40, f"Expected at most 40 utterances, got {len(utterances)}"

    # Check utterance durations are reasonable
    for i, utt in enumerate(utterances):
        dur = len(utt) / SAMPLE_RATE
        assert dur >= MIN_UTTERANCE, f"Utterance {i} too short: {dur:.1f}s"
        assert dur <= MAX_UTTERANCE, f"Utterance {i} too long: {dur:.1f}s"

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
    """No emitted utterance should exceed MAX_UTTERANCE."""
    audio = load_wav_48k(FIXTURE)
    utterances = chunk_audio(audio)
    max_allowed = MAX_UTTERANCE
    for i, utt in enumerate(utterances):
        dur = len(utt) / SAMPLE_RATE
        assert dur <= max_allowed, f"Utterance {i} is {dur:.1f}s, max allowed {max_allowed}s"


if __name__ == "__main__":
    test_chunking_captures_all_phrases()
    test_no_utterance_exceeds_max()
    print("\nAll tests passed!")
