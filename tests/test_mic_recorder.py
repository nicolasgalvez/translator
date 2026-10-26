"""Mic recorder — exercised against a fake sounddevice, so no hardware is needed."""

import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from mic_recorder import MicRecorder

DEVICES = [
    {"name": "BlackHole 2ch", "max_input_channels": 2},
    {"name": "MacBook Pro Speakers", "max_input_channels": 0},
    {"name": "MacBook Pro Microphone", "max_input_channels": 1},
]


class FakeStream:
    def __init__(self, callback, **_kwargs):
        self.callback = callback

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        pass


class FakeSoundDevice:
    class default:  # pylint: disable=invalid-name,too-few-public-methods
        device = [2, 1]

    def __init__(self):
        self.stream = None

    def query_devices(self, index=None):
        return DEVICES if index is None else DEVICES[index]

    def InputStream(self, **kwargs):  # pylint: disable=invalid-name
        self.stream = FakeStream(**kwargs)
        return self.stream


class ResolveDeviceTests(unittest.TestCase):

    def test_default_uses_the_system_default_input(self):
        recorder = MicRecorder("default", Path("unused.wav"), sd=FakeSoundDevice())
        self.assertEqual(recorder.device_name, "MacBook Pro Microphone")

    def test_single_channel_device_is_accepted_by_name(self):
        recorder = MicRecorder("MacBook Pro Mic", Path("unused.wav"), sd=FakeSoundDevice())
        self.assertEqual(recorder.device_index, 2)

    def test_output_only_device_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Speakers"):
            MicRecorder("Speakers", Path("unused.wav"), sd=FakeSoundDevice())

    def test_missing_default_input_is_rejected(self):
        sd = FakeSoundDevice()
        sd.default = type("D", (), {"device": [-1, 1]})
        with self.assertRaises(ValueError):
            MicRecorder("default", Path("unused.wav"), sd=sd)


class AvailableInputsTests(unittest.TestCase):

    def test_lists_only_devices_that_can_record_and_marks_the_default(self):
        self.assertEqual(
            MicRecorder.available_inputs(sd=FakeSoundDevice()),
            [("BlackHole 2ch", False), ("MacBook Pro Microphone", True)],
        )


class FromEnvTests(unittest.TestCase):

    def test_unset_or_blank_means_no_recording(self):
        for environ in [{}, {"TRANSLATOR_MIC": ""}, {"TRANSLATOR_MIC": "  "}]:
            with self.subTest(environ=environ):
                self.assertIsNone(
                    MicRecorder.from_env(environ, Path("unused.wav"), sd=FakeSoundDevice())
                )

    def test_set_builds_a_recorder(self):
        recorder = MicRecorder.from_env(
            {"TRANSLATOR_MIC": "default"}, Path("unused.wav"), sd=FakeSoundDevice()
        )
        self.assertIsNotNone(recorder)


class RecordingTests(unittest.TestCase):

    def test_audio_delivered_before_stop_lands_in_the_wav(self):
        sd = FakeSoundDevice()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.mic.wav"
            recorder = MicRecorder("default", path, sample_rate=48000, sd=sd)
            recorder.start()
            block = np.full((480, 1), 0.5, dtype=np.float32)
            sd.stream.callback(block, 480, None, None)
            sd.stream.callback(block, 480, None, None)
            recorder.stop()

            with wave.open(str(path), "rb") as wav:
                self.assertEqual(wav.getnchannels(), 1)
                self.assertEqual(wav.getframerate(), 48000)
                self.assertEqual(wav.getnframes(), 960)
                samples = np.frombuffer(wav.readframes(960), dtype=np.int16)
            self.assertTrue(np.all(samples == int(0.5 * 32767)))


if __name__ == "__main__":
    unittest.main()
