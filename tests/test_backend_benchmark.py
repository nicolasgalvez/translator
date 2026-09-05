"""
Benchmark transcription speed of available backends on the same audio fixture.

Runs each requested backend over the conversation fixture, reports wall-clock
time and real-time factor (RTF = audio_duration / wall_time; higher is faster).

Usage:
    python tests/test_backend_benchmark.py                         # both, model=small
    python tests/test_backend_benchmark.py --backends mlx-whisper  # one only
    python tests/test_backend_benchmark.py --model large-v3
"""

import argparse
import os
import sys
import time
import wave

import numpy as np
from scipy.signal import resample_poly

# Make sibling 'transcription' package importable when run as a script
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from transcription import get_backend  # noqa: E402  # pylint: disable=wrong-import-position

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "conversation_es.wav")

# Phrases that should appear in any correct transcription of the fixture.
REQUIRED_PHRASES = [
    "cómo estás",
    "Brenda",
    "Argentina",
    "Buenos Aires",
    "Banco de México",
]


def load_fixture_16k() -> tuple[np.ndarray, float]:
    """Load the 48kHz fixture, resample to 16kHz mono float32. Returns (audio, duration_s)."""
    with wave.open(FIXTURE, "rb") as wf:
        sr = wf.getframerate()
        assert wf.getnchannels() == 1
        frames = wf.readframes(wf.getnframes())
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32767.0
    duration = len(audio) / sr
    if sr != 16000:
        # 48k -> 16k via polyphase
        audio = resample_poly(audio, 1, sr // 16000).astype(np.float32)
    return audio, duration


def benchmark(backend_name: str, model_size: str, audio: np.ndarray, duration: float) -> dict:
    print(f"\n=== {backend_name} ({model_size}) ===")
    load_start = time.perf_counter()
    backend = get_backend(backend_name, model_size)
    load_time = time.perf_counter() - load_start

    # Warm pass not counted (first call may JIT/allocate buffers)
    _ = backend.transcribe(audio[:16000], language="es", beam_size=1)

    t0 = time.perf_counter()
    segments, _ = backend.transcribe(audio, language="es", beam_size=1)
    elapsed = time.perf_counter() - t0

    text = " ".join(s.text for s in segments).lower()
    missing = [p for p in REQUIRED_PHRASES if p.lower() not in text]

    rtf = duration / elapsed if elapsed > 0 else float("inf")
    print(f"  load:      {load_time:.2f}s")
    print(f"  transcribe: {elapsed:.2f}s for {duration:.1f}s of audio")
    print(f"  RTF:       {rtf:.2f}x realtime")
    print(f"  segments:  {len(segments)}")
    if missing:
        print(f"  MISSING phrases: {missing}")
    else:
        print("  all required phrases found")

    return {
        "backend": backend_name,
        "load_s": load_time,
        "transcribe_s": elapsed,
        "audio_s": duration,
        "rtf": rtf,
        "segments": len(segments),
        "missing": missing,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--backends",
        nargs="+",
        default=["faster-whisper", "mlx-whisper"],
        help="Backends to benchmark",
    )
    ap.add_argument("--model", default="small", help="Model size (default: small)")
    args = ap.parse_args()

    audio, duration = load_fixture_16k()
    print(f"Fixture: {duration:.1f}s of audio")

    results = []
    for name in args.backends:
        try:
            results.append(benchmark(name, args.model, audio, duration))
        except ImportError as e:
            print(f"\n=== {name} ===\n  SKIPPED: {e}")
        # A benchmark run should report a broken backend, not stop at the first one.
        except Exception as e:  # pylint: disable=broad-exception-caught
            print(f"\n=== {name} ===\n  FAILED: {e}")

    print("\n=== Summary ===")
    print(f"{'backend':<20} {'load':>8} {'transcribe':>12} {'RTF':>8} {'segs':>6}")
    for r in results:
        print(
            f"{r['backend']:<20} {r['load_s']:>7.2f}s "
            f"{r['transcribe_s']:>11.2f}s {r['rtf']:>7.2f}x {r['segments']:>6}"
        )


if __name__ == "__main__":
    main()
