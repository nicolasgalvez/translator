"""After-the-fact session processing — a fake backend stands in for Whisper."""

import json
import tempfile
import time
import unittest
import wave
from pathlib import Path

import numpy as np

from session_processor import SessionProcessor
from transcription.base import Segment

STEM = "2026-10-26_140000"
ACTIVE = "2026-10-26_150000"


class FakeBackend:  # pylint: disable=too-few-public-methods
    name = "fake"

    def __init__(self, *per_call_segments, error=None):
        # One segment list per transcribe call: system audio first, then the mic.
        self.per_call_segments = list(per_call_segments)
        self.error = error
        self.audio = None

    def transcribe(self, audio, **_kwargs):
        if self.error:
            raise self.error
        self.audio = audio
        return (self.per_call_segments.pop(0) if self.per_call_segments else []), "es"


def write_wav(path: Path, seconds: float, rate: int = 48000) -> None:
    wav = wave.Wave_write(str(path))
    wav.setnchannels(1)
    wav.setsampwidth(2)
    wav.setframerate(rate)
    wav.writeframes(np.zeros(int(rate * seconds), dtype=np.int16).tobytes())
    wav.close()


class SessionProcessorTests(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.dir = Path(self.enterContext(tmp))

    def processor(self, backend):
        return SessionProcessor(backend, self.dir, language="es",
                                context={}, active_stem=ACTIVE)

    def wait(self, processor, stem=STEM):
        for _ in range(200):
            job = processor.status(stem)
            if job["status"] != "processing":
                return job
            time.sleep(0.01)
        self.fail("processing did not finish")

    def test_only_finished_untranscribed_recordings_are_processable(self):
        write_wav(self.dir / f"{STEM}.wav", 0.1)
        write_wav(self.dir / f"{ACTIVE}.wav", 0.1)
        write_wav(self.dir / "2026-10-26_130000.wav", 0.1)
        (self.dir / "2026-10-26_130000.jsonl").write_text("")
        processor = self.processor(FakeBackend())
        self.assertTrue(processor.can_process(STEM))
        self.assertFalse(processor.can_process(ACTIVE))  # still recording
        self.assertFalse(processor.can_process("2026-10-26_130000"))  # already transcribed
        self.assertFalse(processor.can_process("2026-10-26_120000"))  # no audio
        self.assertFalse(processor.can_process("../etc"))

    def test_start_rejects_an_unprocessable_session(self):
        with self.assertRaises(ValueError):
            self.processor(FakeBackend()).start(STEM)

    def test_segments_are_saved_with_times_offset_from_the_session_start(self):
        write_wav(self.dir / f"{STEM}.wav", 3)
        backend = FakeBackend([Segment(0.0, 1.0, " hola "), Segment(65.0, 66.0, "adiós")])
        processor = self.processor(backend)
        processor.start(STEM)
        self.assertEqual(self.wait(processor)["status"], "done")

        lines = (self.dir / f"{STEM}.jsonl").read_text().splitlines()
        entries = [json.loads(line) for line in lines]
        self.assertEqual([e["text"] for e in entries], ["hola", "adiós"])
        self.assertEqual([e["time"] for e in entries], ["14:00:00", "14:01:05"])
        self.assertEqual(len(backend.audio), 3 * 16000)  # resampled to 16kHz
        self.assertFalse(processor.can_process(STEM))
        self.assertEqual({e["metadata"]["source"] for e in entries}, {"system"})

    def test_mic_lines_are_merged_into_the_transcript_by_start_time(self):
        write_wav(self.dir / f"{STEM}.wav", 0.5)
        write_wav(self.dir / f"{STEM}.mic.wav", 0.5)
        backend = FakeBackend(
            [Segment(0.0, 2.0, "¿Cómo estás?"), Segment(6.0, 8.0, "¿Y tú?")],
            [Segment(3.0, 5.0, "Muy bien.")],
        )
        processor = self.processor(backend)
        processor.start(STEM)
        self.assertEqual(self.wait(processor)["status"], "done")

        lines = (self.dir / f"{STEM}.jsonl").read_text().splitlines()
        entries = [json.loads(line) for line in lines]
        self.assertEqual(
            [(e["metadata"]["source"], e["text"]) for e in entries],
            [("system", "¿Cómo estás?"), ("mic", "Muy bien."), ("system", "¿Y tú?")],
        )

    def test_no_speech_still_marks_the_session_processed(self):
        write_wav(self.dir / f"{STEM}.wav", 0.5)
        processor = self.processor(FakeBackend([]))
        processor.start(STEM)
        self.assertEqual(self.wait(processor)["status"], "done")
        self.assertTrue((self.dir / f"{STEM}.jsonl").exists())

    def test_failure_leaves_no_transcript_so_it_can_be_retried(self):
        write_wav(self.dir / f"{STEM}.wav", 0.5)
        processor = self.processor(FakeBackend(error=RuntimeError("boom")))
        processor.start(STEM)
        job = self.wait(processor)
        self.assertEqual(job["status"], "error")
        self.assertIn("boom", job["message"])
        self.assertEqual(list(self.dir.glob("*.jsonl*")), [])
        self.assertTrue(processor.can_process(STEM))


if __name__ == "__main__":
    unittest.main()
