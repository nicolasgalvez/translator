"""Runtime boundaries: import, configuration, application lifecycle, and cleanup."""

import asyncio
import gc
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
import wave
import weakref

import pytest
import httpx
import numpy as np

from hooks import add_action, add_filter, clear_hooks


@pytest.mark.parametrize("language", ["es", "invalid-language"])
def test_import_does_not_start_resources(tmp_path, language):
    """Moving any resource acquisition back to import must fail this test."""
    script = """
import importlib.abc
import sys
import threading
import wave
import plugin_loader
def forbidden(*args, **kwargs):
    raise AssertionError('resource acquired during import')
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {
            'sounddevice', 'numpy', 'scipy', 'transcription',
            'faster_whisper', 'mlx_whisper', 'argostranslate',
        }:
            forbidden()
sys.meta_path.insert(0, Guard())
plugin_loader.load_plugins = forbidden
threading.Thread.start = forbidden
wave.open = forbidden
import app
assert app.app is not None
"""
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    # Validation belongs to startup too; even a bad environment permits import.
    environment["TRANSLATOR_LANGUAGE"] = language
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, env=environment,
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not list(tmp_path.iterdir())


def test_configuration_retains_environment_without_resources(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    module = importlib.import_module("translator_runtime")
    config = module.RuntimeConfig.from_environment({
        "TRANSLATOR_HOST": "0.0.0.0", "TRANSLATOR_PORT": "9123",
        "TRANSLATOR_MODEL": "medium", "TRANSLATOR_DEVICE": "Test input",
        "TRANSLATOR_BACKEND": "mlx-whisper", "TRANSLATOR_LANGUAGE": " AUTO ",
    })
    assert (config.host, config.port, config.model, config.device_name, config.backend_name) == (
        "0.0.0.0", 9123, "medium", "Test input", "mlx-whisper",
    )
    assert config.language.code is None
    module.TranslatorRuntime(config)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("variable,value", [
    ("TRANSLATOR_PORT", "invalid"), ("TRANSLATOR_PORT", "0"),
    ("TRANSLATOR_PORT", "65536"), ("TRANSLATOR_HOST", " "),
    ("TRANSLATOR_MODEL", ""), ("TRANSLATOR_DEVICE", ""),
    ("TRANSLATOR_BACKEND", "unknown"), ("TRANSLATOR_LANGUAGE", "invalid"),
])
def test_configuration_rejects_invalid_values(variable, value):
    module = importlib.import_module("translator_runtime")
    with pytest.raises(ValueError, match=variable):
        module.RuntimeConfig.from_environment({variable: value})


def test_lifespan_owns_one_runtime_around_real_request(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    module = importlib.import_module("translator_runtime")
    application = importlib.import_module("app")
    events = []

    class RecordingRuntime(module.TranslatorRuntime):
        async def start(self):
            events.append("start")
            self.caption_jobs["test-job"] = {"status": "queued", "progress": 0}

        async def stop(self):
            events.append("stop")

    def factory():
        events.append("create")
        return RecordingRuntime(module.RuntimeConfig.from_environment({}))

    app = application.create_app(runtime_factory=factory)
    assert not events
    async def exercise():
        async with app.router.lifespan_context(app):
            assert isinstance(app.state.runtime, RecordingRuntime)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test",
            ) as client:
                response = await client.get("/captions/status/test-job")
                assert response.status_code == 200
                assert response.json() == {"status": "queued", "progress": 0}
                events.append("request")
                assert events == ["create", "start", "request"]

    asyncio.run(exercise())
    assert events == ["create", "start", "request", "stop"]


def test_existing_http_routes_use_runtime_files(tmp_path):
    module = importlib.import_module("translator_runtime")
    application = importlib.import_module("app")

    class FileRuntime(module.TranslatorRuntime):  # pylint: disable=too-few-public-methods
        def __init__(self, config):
            super().__init__(config)
            self.transcripts_dir = tmp_path / "transcripts"
            self.captions_dir = tmp_path / "captions"
            self.frontend_dist = tmp_path / "frontend"
            self.templates = module.Jinja2Templates(
                directory=str(Path(__file__).resolve().parents[1] / "templates"),
            )

        async def start(self):
            self.transcripts_dir.mkdir()
            self.captions_dir.mkdir()
            (self.transcripts_dir / "session.jsonl").write_text(
                '{"text": "hola", "time": "12:00:00"}\n', encoding="utf-8",
            )
            (self.transcripts_dir / "session.wav").write_bytes(b"audio fixture")
            (self.captions_dir / "job").mkdir()
            (self.captions_dir / "job" / "video.original.srt").write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nhola\n", encoding="utf-8",
            )

    async def exercise():
        app = application.create_app(lambda: FileRuntime(module.RuntimeConfig.from_environment({})))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test",
            ) as client:
                for route in ("/", "/history", "/history/session.jsonl", "/captions"):
                    assert (await client.get(route)).status_code == 200
                assert "hola" in (await client.get("/history/session.jsonl")).text
                assert (await client.get("/audio/session.wav")).content == b"audio fixture"
                subtitle = await client.get("/captions/download/job/video.original.srt")
                assert subtitle.status_code == 200
                assert "hola" in subtitle.text
                missing = await client.get("/captions/status/missing")
                assert missing.status_code == 404
                assert missing.json() == {"error": "Job not found"}

    asyncio.run(exercise())


def test_live_audio_persists_and_queues_transcript(tmp_path):
    module = importlib.import_module("translator_runtime")

    class Backend:  # pylint: disable=too-few-public-methods
        name = "fixture backend"

        def transcribe(self, audio, *, language, beam_size):
            assert (len(audio), audio.dtype, language, beam_size) == (16000, np.float32, "es", 1)
            return [SimpleNamespace(text=" hola ")], "es"

    runtime = module.TranslatorRuntime(module.RuntimeConfig.from_environment({}))
    runtime.backend = Backend()
    runtime.transcript_file = tmp_path / "session.jsonl"
    runtime._transcribe_audio(np.zeros(48000, dtype=np.float32))  # pylint: disable=protected-access
    entry = json.loads(runtime.transcript_file.read_text(encoding="utf-8"))
    assert entry["text"] == "hola"
    assert runtime.text_queue.get_nowait() == entry


def test_shutdown_discards_an_inflight_transcription(tmp_path):
    # First-import cost is outside the worker synchronization deadline.
    importlib.import_module("scipy.signal")
    module = importlib.import_module("translator_runtime")
    entered = threading.Event()
    release = threading.Event()

    class SlowBackend:  # pylint: disable=too-few-public-methods
        name = "slow"

        def transcribe(self, *_args, **_kwargs):
            entered.set()
            release.wait(5)
            return [SimpleNamespace(text="must not persist after shutdown")], "es"

    async def exercise():
        runtime = module.TranslatorRuntime(module.RuntimeConfig.from_environment({}))
        runtime.backend = SlowBackend()
        backend_reference = weakref.ref(runtime.backend)
        runtime.transcript_file = tmp_path / "session.jsonl"
        for amplitude in (0.1, 0, 0):
            runtime.audio_chunk_queue.put(np.full(12000, amplitude, dtype="float32"))
        worker = threading.Thread(target=runtime.audio_process_loop)
        runtime.worker_threads.append(worker)
        worker.start()
        stopped = False
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            await asyncio.wait_for(runtime.stop(), timeout=3)
            stopped = True
            assert backend_reference() is not None
        finally:
            release.set()
            if not stopped:
                await runtime.stop()
            await asyncio.to_thread(worker.join, 2)
        assert not worker.is_alive()
        gc.collect()
        assert backend_reference() is None
        assert not runtime.transcript_file.exists()
        assert runtime.text_queue.empty()

    asyncio.run(exercise())


@pytest.mark.parametrize("hook_name", [
    "transcript.before_save", "transcript.after_save", "transcript.before_render",
])
def test_shutdown_blocks_output_after_an_overdue_transcript_callback(tmp_path, hook_name):
    importlib.import_module("scipy.signal")
    module = importlib.import_module("translator_runtime")
    entered = threading.Event()
    release = threading.Event()

    class Backend:  # pylint: disable=too-few-public-methods
        name = "fixture"

        def transcribe(self, *_args, **_kwargs):
            return [SimpleNamespace(text="must not be committed after stop")], "es"

    def paused_callback(event, _context):
        entered.set()
        assert release.wait(10)
        return event

    async def exercise():
        runtime = module.TranslatorRuntime(module.RuntimeConfig.from_environment({}))
        runtime.backend = Backend()
        runtime.transcript_file = tmp_path / "session.jsonl"
        for amplitude in (0.1, 0, 0):
            runtime.audio_chunk_queue.put(np.full(12000, amplitude, dtype="float32"))
        worker = threading.Thread(target=runtime.audio_process_loop)
        runtime.worker_threads.append(worker)
        worker.start()
        stopped = False
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            await asyncio.wait_for(runtime.stop(), timeout=3)
            stopped = True
            saved_at_shutdown = (
                runtime.transcript_file.read_bytes() if runtime.transcript_file.exists() else None
            )
            assert runtime.text_queue.empty()
        finally:
            release.set()
            if not stopped:
                await runtime.stop()
            await asyncio.to_thread(worker.join, 2)
        assert not worker.is_alive()
        saved_after_callback = (
            runtime.transcript_file.read_bytes() if runtime.transcript_file.exists() else None
        )
        assert saved_after_callback == saved_at_shutdown
        if hook_name == "transcript.before_save":
            assert saved_after_callback is None
        else:
            assert saved_after_callback is not None
        assert runtime.text_queue.empty()

    clear_hooks()
    register = add_action if hook_name == "transcript.after_save" else add_filter
    register(hook_name, paused_callback)
    try:
        asyncio.run(exercise())
    finally:
        clear_hooks()


def test_shutdown_waits_for_a_transcript_file_commit(tmp_path, monkeypatch):
    importlib.import_module("scipy.signal")
    module = importlib.import_module("translator_runtime")
    events_module = importlib.import_module("transcript_events")
    entered = threading.Event()
    release = threading.Event()

    class Backend:  # pylint: disable=too-few-public-methods
        name = "fixture"

        def transcribe(self, *_args, **_kwargs):
            return [SimpleNamespace(text="commit started before stop")], "es"

    class PausedFile:
        def __init__(self, path, mode, *, encoding):
            # This context manager closes the file in __exit__.
            self.file = open(path, mode, encoding=encoding)  # pylint: disable=consider-using-with

        def __enter__(self):
            entered.set()
            assert release.wait(10)
            return self.file

        def __exit__(self, *_args):
            self.file.close()

    monkeypatch.setattr(events_module, "open", PausedFile, raising=False)

    async def exercise():
        runtime = module.TranslatorRuntime(module.RuntimeConfig.from_environment({}))
        runtime.backend = Backend()
        runtime.transcript_file = tmp_path / "session.jsonl"
        for amplitude in (0.1, 0, 0):
            runtime.audio_chunk_queue.put(np.full(12000, amplitude, dtype="float32"))
        worker = threading.Thread(target=runtime.audio_process_loop)
        runtime.worker_threads.append(worker)
        worker.start()
        shutdown = None
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            shutdown = asyncio.create_task(runtime.stop())
            await asyncio.sleep(2.2)
            assert not shutdown.done()
        finally:
            release.set()
            if shutdown is None:
                shutdown = asyncio.create_task(runtime.stop())
            await asyncio.wait_for(shutdown, 3)
            saved_at_shutdown = runtime.transcript_file.read_bytes()
            await asyncio.to_thread(worker.join, 2)
        assert not worker.is_alive()
        assert runtime.transcript_file.read_bytes() == saved_at_shutdown
        assert json.loads(saved_at_shutdown)["text"] == "commit started before stop"
        assert runtime.text_queue.empty()

    asyncio.run(exercise())


def test_websocket_disconnect_after_shutdown_is_clean():
    module = importlib.import_module("translator_runtime")

    async def exercise():
        runtime = module.TranslatorRuntime(module.RuntimeConfig.from_environment({}))
        connected = asyncio.Event()
        closed = asyncio.Event()
        connection_received = False

        async def receive():
            nonlocal connection_received
            if not connection_received:
                connection_received = True
                return {"type": "websocket.connect"}
            await closed.wait()
            return {"type": "websocket.disconnect", "code": 1000}

        async def send(message):
            if message["type"] == "websocket.accept":
                connected.set()
            elif message["type"] == "websocket.close":
                closed.set()

        websocket = module.WebSocket({"type": "websocket"}, receive, send)
        endpoint = asyncio.create_task(runtime.websocket_endpoint(websocket))
        await connected.wait()
        await runtime.stop()
        await asyncio.wait_for(endpoint, 1)
        assert runtime.clients == []
        assert closed.is_set()

    asyncio.run(exercise())


def test_caption_worker_stops_after_inflight_backend_returns(tmp_path):
    module = importlib.import_module("translator_runtime")
    entered = threading.Event()
    release = threading.Event()

    class SlowBackend:  # pylint: disable=too-few-public-methods
        def transcribe(self, *_args, **_kwargs):
            entered.set()
            release.wait(5)
            return [SimpleNamespace(start=0, end=0.25, text="hola")], "es"

    class CaptionRuntime(module.TranslatorRuntime):
        def extract_audio_16k(self, _video_path, audio_path):
            with wave.Wave_write(str(audio_path)) as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(16000)
                audio.writeframes(bytes(8000))

        def ensure_argos_packages(self, _job):
            raise AssertionError("caption continued after shutdown")

    async def exercise():
        runtime = CaptionRuntime(module.RuntimeConfig.from_environment({}))
        runtime.backend = SlowBackend()
        runtime.captions_dir = tmp_path
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        runtime.caption_jobs["job"] = {"status": "queued"}
        worker = threading.Thread(
            target=runtime.caption_worker, args=("job", job_dir / "video.mp4"),
        )
        runtime.worker_threads.append(worker)
        worker.start()
        stopped = False
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            await asyncio.wait_for(runtime.stop(), 3)
            stopped = True
        finally:
            release.set()
            if not stopped:
                await runtime.stop()
            await asyncio.to_thread(worker.join, 2)
        assert not worker.is_alive()
        assert runtime.caption_jobs["job"]["status"] == "error"
        assert runtime.caption_jobs["job"]["message"] == "Runtime stopped"
        assert not list(job_dir.glob("*.srt"))

    asyncio.run(exercise())


@pytest.mark.parametrize("broadcast_failure", [False, True])
def test_runtime_shutdown_closes_owned_resources(tmp_path, monkeypatch, broadcast_failure):
    """Exercise real queues, WAV persistence, worker loops, and task cancellation."""
    monkeypatch.chdir(tmp_path)
    module = importlib.import_module("translator_runtime")
    stream_closed = threading.Event()
    reading = threading.Event()

    class InputStream:
        def __init__(self, **_kwargs):
            self.aborted = threading.Event()

        def start(self):
            pass

        def read(self, _frames):
            reading.set()
            self.aborted.wait(5)
            raise RuntimeError("stream aborted")

        def abort(self):
            self.aborted.set()

        def close(self):
            stream_closed.set()

    class Backend:  # pylint: disable=too-few-public-methods
        name = "test backend"

    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(
        query_devices=lambda: [{"name": "BlackHole 2ch", "max_input_channels": 2}],
        InputStream=InputStream,
    ))
    monkeypatch.setitem(sys.modules, "transcription", SimpleNamespace(
        get_backend=lambda *_args: Backend(),
    ))
    monkeypatch.setattr(module, "load_plugins", lambda: [])

    async def exercise():
        runtime = module.TranslatorRuntime(module.RuntimeConfig.from_environment({}))
        other = module.TranslatorRuntime(module.RuntimeConfig.from_environment({}))
        await runtime.start()
        assert await asyncio.to_thread(reading.wait, 2)
        assert runtime.text_queue is not other.text_queue
        assert runtime.audio_chunk_queue is not other.audio_chunk_queue
        assert runtime.clients is not other.clients
        assert runtime.caption_jobs is not other.caption_jobs
        runtime.caption_jobs["owned"] = {"status": "queued"}
        assert other.caption_jobs == {}
        if broadcast_failure:
            runtime.text_queue.put({"not_serializable": {1, 2}})
            await asyncio.sleep(0.2)
            assert runtime.broadcast_task.done()
        await asyncio.wait_for(runtime.stop(), timeout=3)
        assert all(not thread.is_alive() for thread in runtime.worker_threads)
        assert runtime.broadcast_task.done()
        assert stream_closed.is_set()
        assert runtime.clients == []
        assert runtime.backend is None
        with wave.open(str(runtime.audio_file), "rb") as audio:
            assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (
                1, 2, 48000,
            )
        await runtime.stop()

    asyncio.run(exercise())
