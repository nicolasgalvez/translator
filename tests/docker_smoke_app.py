"""Start the production app with only model and audio hardware replaced."""

import runpy
import sys
import threading
from pathlib import Path
from types import SimpleNamespace


class SmokeInputStream:
    """Block like a live input until the runtime aborts the stream."""

    def __init__(self, **_kwargs):
        self.stopped = threading.Event()

    def start(self):
        return None

    def read(self, _frames):
        self.stopped.wait()
        raise RuntimeError("smoke input stopped")

    def abort(self):
        self.stopped.set()

    def close(self):
        self.stopped.set()


class SmokeBackend:  # pylint: disable=too-few-public-methods
    """Avoid model downloads while retaining the production runtime lifecycle."""

    name = "container-smoke"

    def transcribe(self, _audio, **_kwargs):
        return [], "en"


sys.path.insert(0, str(Path("/app")))
sys.modules["sounddevice"] = SimpleNamespace(
    default=SimpleNamespace(device=(0, 0)),
    query_devices=lambda: [{"name": "Smoke ALSA input", "max_input_channels": 1}],
    InputStream=SmokeInputStream,
)
sys.modules["transcription"] = SimpleNamespace(
    get_backend=lambda _backend, _model: SmokeBackend(),
)

runpy.run_path("/app/app.py", run_name="__main__")
