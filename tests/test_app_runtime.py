"""Runtime boundaries: import, configuration, application lifecycle, and cleanup."""

# Endpoint regressions stay beside the resource and lifecycle boundary tests.
# pylint: disable=too-many-lines

import asyncio
import gc
import importlib
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import wave
import weakref
from contextlib import suppress

import pytest
import httpx
import numpy as np
from starlette.websockets import WebSocketState

from hooks import add_action, add_filter, clear_hooks


def start_audio_workers(runtime):
    """Start the production processing workers without acquiring hardware."""
    for target in (runtime.audio_process_loop, runtime.audio_transcription_loop):
        worker = threading.Thread(target=target)
        runtime.worker_threads.append(worker)
        worker.start()
    return worker


def wait_until(predicate):
    deadline = time.monotonic() + 2
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert predicate()


def test_audio_silence_retains_only_preroll_without_repeated_copying(tmp_path, monkeypatch):
    importlib.import_module("scipy.signal")
    module = importlib.import_module("translator_runtime")
    received = []
    concatenations = []
    concatenate = np.concatenate

    def tracked_concatenate(arrays, *args, **kwargs):
        result = concatenate(arrays, *args, **kwargs)
        concatenations.append(len(result))
        return result

    class Backend:  # pylint: disable=too-few-public-methods
        name = "fixture"

        def transcribe(self, audio, **_kwargs):
            received.append(audio)
            return [SimpleNamespace(text="after silence")], "es"

    runtime = module.TranslatorRuntime(module.RuntimeConfig.from_environment({}))
    runtime.backend = Backend()
    runtime.transcript_file = tmp_path / "session.jsonl"
    monkeypatch.setattr(np, "concatenate", tracked_concatenate)
    start_audio_workers(runtime)
    try:
        for amplitude in [0] * 100 + [0.1, 0, 0]:
            runtime.audio_chunk_queue.put(np.full(12000, amplitude, dtype="float32"))
            wait_until(lambda: runtime.audio_chunk_queue.unfinished_tasks == 0)
        wait_until(lambda: not runtime.text_queue.empty())
        assert len(received) == 1
        assert len(received[0]) == 20000  # 0.5s pre-roll + 0.25s speech + 0.5s pause.
        assert len(concatenations) == 1
        assert max(concatenations) == 60000
    finally:
        asyncio.run(runtime.stop())


def test_audio_capture_overload_keeps_recent_chunks_and_reports_drops(tmp_path, caplog):
    module = importlib.import_module("translator_runtime")
    runtime = module.TranslatorRuntime(module.RuntimeConfig.from_environment({}))

    class Stream:  # pylint: disable=too-few-public-methods
        reads = 0

        def read(self, frames):
            self.reads += 1
            if self.reads == 101:
                runtime._stopping.set()  # pylint: disable=protected-access
            return np.full((frames, 1), self.reads / 100, dtype="float32"), False

    runtime.audio_stream = Stream()
    runtime.audio_file = tmp_path / "capture.wav"
    with wave.Wave_write(str(runtime.audio_file)) as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(48000)
        runtime.wav_writer = writer
        runtime.audio_capture_loop()
    assert runtime.audio_chunk_queue.qsize() <= 8
    chunks = []
    while not runtime.audio_chunk_queue.empty():
        chunks.append(runtime.audio_chunk_queue.get_nowait())
    assert [round(float(chunk[0]) * 100) for chunk in chunks] == list(range(93, 101))
    assert runtime.audio_chunk_queue.dropped_count == 92
    assert "capture" in caplog.text and "dropped" in caplog.text
    with wave.open(str(runtime.audio_file), "rb") as recording:
        assert recording.getnframes() == 1200000


def test_audio_chunking_continues_during_slow_inference(tmp_path):
    importlib.import_module("scipy.signal")
    module = importlib.import_module("translator_runtime")
    entered, release = threading.Event(), threading.Event()

    class Backend:  # pylint: disable=too-few-public-methods
        name = "slow"

        def transcribe(self, *_args, **_kwargs):
            entered.set()
            assert release.wait(10)
            return [SimpleNamespace(text="recovered")], "es"

    runtime = module.TranslatorRuntime(module.RuntimeConfig.from_environment({}))
    runtime.backend = Backend()
    runtime.transcript_file = tmp_path / "session.jsonl"
    start_audio_workers(runtime)
    try:
        for amplitude in (0.1, 0, 0):
            runtime.audio_chunk_queue.put(np.full(12000, amplitude, dtype="float32"))
        assert entered.wait(2)
        for amplitude in (0.2, 0, 0):
            runtime.audio_chunk_queue.put(np.full(12000, amplitude, dtype="float32"))
        wait_until(lambda: runtime.audio_chunk_queue.unfinished_tasks == 0)
        assert runtime.utterance_queue.qsize() == 1
    finally:
        release.set()
        asyncio.run(runtime.stop())


def test_audio_chunker_bounds_silence_and_preserves_quiet_cuts():
    chunker = importlib.import_module("audio_pipeline").UtteranceChunker()
    for _ in range(4000):
        assert chunker.add(np.zeros(12000, dtype="float32")) is None
        assert chunker.buffered_samples <= 24000
    # Start a fresh speech sequence; the quietest window ends at 4.6 seconds.
    chunker.reset()
    audio = np.full(480000, 0.1, dtype="float32")
    audio[218400:220800] = 0.001
    audio[240000:] = np.linspace(0.01, 0.005, 240000, dtype="float32")
    outputs = []
    for start in range(0, len(audio), 12000):
        result = chunker.add(audio[start:start + 12000])
        if result is not None:
            outputs.append(result)
            assert 24000 <= len(result) <= 240000
        assert chunker.buffered_samples < 240000
    for _ in range(2):
        result = chunker.add(np.zeros(12000, dtype="float32"))
        if result is not None:
            outputs.append(result)
    assert len(outputs[0]) == 220800
    assert len(outputs[1]) == 240000  # Carried tail must not allow a 5.1-second emission.
    np.testing.assert_array_equal(np.concatenate(outputs), np.pad(audio, (0, 24000)))
    assert chunker.buffered_samples == 0


def test_audio_chunker_waits_for_minimum_and_two_silent_chunks():
    chunker = importlib.import_module("audio_pipeline").UtteranceChunker()
    assert chunker.add(np.full(6000, 0.1, dtype="float32")) is None
    assert chunker.add(np.zeros(6000, dtype="float32")) is None
    assert chunker.add(np.zeros(6000, dtype="float32")) is None
    output = chunker.add(np.zeros(6000, dtype="float32"))
    assert len(output) == 24000
    np.testing.assert_array_equal(output[:6000], np.full(6000, 0.1, dtype="float32"))
    assert not np.any(output[6000:])


def test_audio_pending_overload_recovers_with_recent_utterances(tmp_path, caplog):
    importlib.import_module("scipy.signal")
    module = importlib.import_module("translator_runtime")
    entered, release = threading.Event(), threading.Event()
    received = []

    class Backend:  # pylint: disable=too-few-public-methods
        name = "slow"

        def transcribe(self, audio, **_kwargs):
            value = round(float(audio[1000]) * 100)
            received.append(value)
            entered.set()
            assert release.wait(10)
            return [SimpleNamespace(text=f"utterance {value}")], "es"

    runtime = module.TranslatorRuntime(module.RuntimeConfig.from_environment({}))
    runtime.backend = Backend()
    runtime.transcript_file = tmp_path / "session.jsonl"
    start_audio_workers(runtime)
    try:
        for number in range(1, 12):
            for amplitude in (number / 100, 0, 0):
                runtime.audio_chunk_queue.put(np.full(12000, amplitude, dtype="float32"))
                wait_until(lambda: runtime.audio_chunk_queue.unfinished_tasks == 0)
            if number == 1:
                assert entered.wait(2)
        assert runtime.utterance_queue.qsize() == 2
        assert runtime.utterance_queue.dropped_count == 8
        assert runtime.audio_chunk_queue.dropped_count == 0
        assert "utterance" in caplog.text and "dropped" in caplog.text
        release.set()
        wait_until(lambda: runtime.text_queue.qsize() == 3)
        assert received == [1, 10, 11]
        saved_texts = [json.loads(line)["text"]
                       for line in runtime.transcript_file.read_text().splitlines()]
        assert saved_texts == [
            "utterance 1", "utterance 10", "utterance 11",
        ]
    finally:
        release.set()
        asyncio.run(runtime.stop())


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


@pytest.mark.parametrize("environment,expected", [
    ({}, 1073741824), ({"TRANSLATOR_MAX_UPLOAD_BYTES": "17"}, 17),
])
def test_max_upload_configuration(environment, expected):
    module = importlib.import_module("translator_runtime")
    assert module.RuntimeConfig.from_environment(environment).max_upload_bytes == expected


@pytest.mark.parametrize("value", ["", " ", "invalid", "1.5", "0", "-1"])
def test_max_upload_configuration_rejects_invalid_limit(value):
    module = importlib.import_module("translator_runtime")
    with pytest.raises(ValueError, match="TRANSLATOR_MAX_UPLOAD_BYTES"):
        module.RuntimeConfig.from_environment({"TRANSLATOR_MAX_UPLOAD_BYTES": value})


@pytest.fixture(name="upload_runtime")
def fixture_upload_runtime(tmp_path):
    """Keep storage and worker threads real; pause only external audio extraction."""
    module = importlib.import_module("translator_runtime")
    release = threading.Event()
    entered = threading.Event()
    inputs = []

    class UploadRuntime(module.TranslatorRuntime):  # pylint: disable=too-few-public-methods
        def extract_audio_16k(self, video_path, _audio_path):
            inputs.append((video_path, video_path.read_bytes()))
            entered.set()
            release.wait(10)
            return "Fixture extraction finished"

    runtime = UploadRuntime(module.RuntimeConfig.from_environment({
        "TRANSLATOR_MAX_UPLOAD_BYTES": "1048579",
    }))
    runtime.captions_dir = tmp_path / "captions"
    yield runtime, entered, inputs
    release.set()
    asyncio.run(runtime.stop())


@pytest.mark.parametrize("filename", [
    "video.mp4", "../../escaped.mp4", "nested/video.mp4", "absolute", "audio.wav",
])
def test_upload_uses_server_filename_and_preserves_metadata(upload_runtime, tmp_path, filename):
    runtime, entered, inputs = upload_runtime
    if filename == "absolute":
        filename = str(tmp_path / "escaped.mp4")
    upload_content = b"v" * 1048579 if filename == "video.mp4" else b"video content"
    application = importlib.import_module("app").create_app()
    application.state.runtime = runtime

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test",
        ) as client:
            response = await client.post(
                "/captions/upload", files={"file": (filename, upload_content)},
            )
            assert response.status_code == 200
            payload = response.json()
            assert set(payload) == {"job_id"}
            assert len(payload["job_id"]) == 12
            assert await asyncio.to_thread(entered.wait, 2)
            job_dir = runtime.captions_dir / payload["job_id"]
            path, content = inputs[0]
            assert path.parent == job_dir
            assert path.name not in (filename, "audio.wav")
            assert content == upload_content
            assert list(job_dir.iterdir()) == [path]
            assert sorted(tmp_path.iterdir()) == [runtime.captions_dir]
            status = await client.get(f"/captions/status/{payload['job_id']}")
            assert status.json()["original_filename"] == filename

    asyncio.run(exercise())


@pytest.mark.parametrize("size", [0, 1048579])
def test_upload_accepts_exact_limit_and_closes_upload(upload_runtime, size):
    runtime, entered, inputs = upload_runtime
    module = importlib.import_module("translator_runtime")
    upload = module.UploadFile(BytesIO(b"v" * size), filename="video.mp4")

    async def exercise():
        response = await runtime.captions_upload(upload)
        assert response.status_code == 200
        assert await asyncio.to_thread(entered.wait, 2)
        assert inputs[0][1] == b"v" * size
        assert upload.file.closed

    asyncio.run(exercise())


def test_upload_rejects_one_byte_over_http_limit_and_removes_job(upload_runtime):
    runtime, entered, _inputs = upload_runtime
    application = importlib.import_module("app").create_app()
    application.state.runtime = runtime

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test",
        ) as client:
            response = await client.post(
                "/captions/upload", files={"file": ("video.mp4", b"v" * 1048580)},
            )
        assert response.status_code == 413
        assert response.json() == {"error": "Upload exceeds maximum size of 1048579 bytes"}
        assert not list(runtime.captions_dir.iterdir())
        assert not runtime.caption_jobs
        assert not runtime.worker_threads
        assert not entered.is_set()

    asyncio.run(exercise())


def test_upload_checks_size_before_writing_and_closes_rejection(upload_runtime, monkeypatch):
    runtime, entered, _inputs = upload_runtime
    module = importlib.import_module("translator_runtime")
    observed_sizes = []

    class ObservedUpload(module.UploadFile):  # pylint: disable=too-few-public-methods
        async def read(self, size=-1):
            assert 0 < size <= 1048576
            return await super().read(size)

    class ObservedWriter:
        def __init__(self, path, mode):
            self.file = open(path, mode)  # pylint: disable=consider-using-with

        def __enter__(self):
            return self.file

        def __exit__(self, *_args):
            observed_sizes.append(self.file.tell())
            self.file.close()

    monkeypatch.setattr(module, "open", ObservedWriter, raising=False)
    upload = ObservedUpload(BytesIO(b"v" * 2097153), filename="video.mp4")
    response = asyncio.run(runtime.captions_upload(upload))
    assert response.status_code == 413
    assert max(observed_sizes) <= 1048579
    assert upload.file.closed
    assert not list(runtime.captions_dir.iterdir())
    assert not runtime.caption_jobs
    assert not entered.is_set()


@pytest.mark.parametrize("content_length", [None, "1", "1000000000"])
@pytest.mark.parametrize("deployment_prefix", ["", "/api"], ids=["root", "prefixed"])
@pytest.mark.parametrize("encoded_suffix", ["", "%0A"], ids=["canonical", "encoded-newline"])
def test_upload_request_limit_stops_multipart_spooling(
    upload_runtime, monkeypatch, content_length, deployment_prefix, encoded_suffix,
):
    runtime, entered, _inputs = upload_runtime
    application = importlib.import_module("app").create_app()
    application.state.runtime = runtime
    parser_module = importlib.import_module("starlette.formparsers")
    observed = SimpleNamespace(
        spools=[], consumed=0, handler_entered=False, original_upload=runtime.captions_upload,
    )

    class ObservedSpool(parser_module.SpooledTemporaryFile):
        # pylint: disable=too-few-public-methods
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.saved_size = 0
            observed.spools.append(self)

        def close(self):
            if not self.closed:
                self.saved_size = self.tell()
            super().close()

    monkeypatch.setattr(parser_module, "SpooledTemporaryFile", ObservedSpool)
    async def observe_handler(file):
        observed.handler_entered = True
        return await observed.original_upload(file)

    monkeypatch.setattr(runtime, "captions_upload", observe_handler)

    async def multipart_body():
        yield (b'--boundary\r\nContent-Disposition: form-data; name="file"; '
               b'filename="video.mp4"\r\nContent-Type: video/mp4\r\n\r\n')
        for _ in range(64):
            observed.consumed += 65536
            yield b"v" * 65536
        yield b"\r\n--boundary--\r\n"

    async def exercise():
        headers = {"content-type": "multipart/form-data; boundary=boundary"}
        if content_length is not None:
            headers["content-length"] = content_length
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application, root_path=deployment_prefix),
            base_url="http://test",
        ) as client:
            response = await client.post(
                f"{deployment_prefix}/captions/upload{encoded_suffix}",
                headers=headers, content=multipart_body(),
            )
        assert response.status_code == 413
        assert response.json() == {"error": "Upload exceeds maximum size of 1048579 bytes"}
        assert not observed.handler_entered
        if content_length == "1000000000":
            assert observed.consumed == 0
            assert not observed.spools
        else:
            assert 0 < observed.consumed <= 1048579 + 65536 + 65536
            assert len(observed.spools) == 1
            assert observed.spools[0].closed
            assert 0 < observed.spools[0].saved_size <= 1048579 + 65536
        assert not runtime.captions_dir.exists()
        assert not runtime.caption_jobs
        assert not runtime.worker_threads
        assert not entered.is_set()

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["write", "thread_start", "mkdir"])
def test_upload_failure_removes_files_and_job_and_closes_upload(
    upload_runtime, monkeypatch, failure,
):
    runtime, entered, _inputs = upload_runtime
    module = importlib.import_module("translator_runtime")
    failure_error = OSError(f"{failure} failed")

    class FailingWriter:
        def __init__(self, path, mode):
            self.file = open(path, mode)  # pylint: disable=consider-using-with

        def __enter__(self):
            return self

        def write(self, data):
            self.file.write(data[:2])
            self.file.flush()
            raise failure_error

        def __exit__(self, *_args):
            self.file.close()

    if failure == "write":
        monkeypatch.setattr(module, "open", FailingWriter, raising=False)
    elif failure == "thread_start":
        original_start = threading.Thread.start

        def fail_start(thread):
            if thread._target == runtime.caption_worker:  # pylint: disable=protected-access
                raise failure_error
            original_start(thread)
        monkeypatch.setattr(threading.Thread, "start", fail_start)
    else:
        runtime.captions_dir.write_bytes(b"existing file")

    upload = module.UploadFile(BytesIO(b"video content"), filename="video.mp4")

    async def exercise():
        with pytest.raises(OSError) as caught:
            await runtime.captions_upload(upload)
        if failure != "mkdir":
            assert caught.value is failure_error
            assert not list(runtime.captions_dir.iterdir())
        else:
            assert runtime.captions_dir.read_bytes() == b"existing file"
        assert upload.file.closed
        assert not runtime.caption_jobs
        assert not runtime.worker_threads
        assert not entered.is_set()

    asyncio.run(exercise())


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
        worker = start_audio_workers(runtime)
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
        assert runtime.audio_chunk_queue.empty()
        assert runtime.utterance_queue.empty()
        runtime.audio_chunk_queue.put(np.ones(12000, dtype="float32"))
        runtime.utterance_queue.put(np.ones(24000, dtype="float32"))
        assert runtime.audio_chunk_queue.empty()
        assert runtime.utterance_queue.empty()

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
        worker = start_audio_workers(runtime)
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
        worker = start_audio_workers(runtime)
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


def websocket_scope(headers, root_path="", scheme="ws"):
    """A request with server-provided connection metadata and raw client headers."""
    return {
        "type": "websocket", "asgi": {"version": "3.0"}, "scheme": scheme,
        "path": root_path + "/ws", "raw_path": (root_path + "/ws").encode(),
        "root_path": root_path, "query_string": b"", "headers": headers,
        "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 8765),
        "subprotocols": [],
    }


@pytest.mark.parametrize("scheme,origin,host,allowed", [
    ("ws", "http://localhost:8765", "localhost:8765", True),
    ("ws", "HTTP://LOCALHOST:8765", "LocalHost:8765", True),
    ("ws", "http://localhost", "localhost:80", True),
    ("ws", "http://localhost:80", "localhost", True),
    ("wss", "https://transcripts.example", "transcripts.example:443", True),
    ("wss", "https://transcripts.example:443", "transcripts.example", True),
    ("wss", "https://transcripts.example:0443", "transcripts.example", True),
    ("ws", "http://127.0.0.1:5173", "127.0.0.1:5173", True),
    ("ws", "http://[::1]:8765", "[0:0:0:0:0:0:0:1]:8765", True),
    ("wss", "https://[2001:DB8::1]", "[2001:db8:0:0:0:0:0:1]:443", True),
    ("ws", "http://localhost:8765", "localhost:8766", False),
    ("ws", "http://127.0.0.1:8765", "localhost:8765", False),
    ("wss", "https://transcripts.example", "transcripts.example:80", False),
    ("ws", "http://transcripts.example", "transcripts.example:443", False),
    ("ws", "http://[::1]:8765", "[::2]:8765", False),
    ("ws", "http://localhost:8765", "evil.example:8765", False),
    ("ws", "http://localhost:8765", "localhost.:8765", False),
    ("wss", "http://transcripts.example", "transcripts.example", False),
    ("wss", "http://transcripts.example", "transcripts.example:443", False),
    ("wss", "http://transcripts.example:443", "transcripts.example:443", False),
    ("ws", "https://transcripts.example", "transcripts.example", False),
    ("ws", "https://transcripts.example", "transcripts.example:80", False),
    ("ws", "https://transcripts.example:80", "transcripts.example:80", False),
])
def test_websocket_origin_policy_matches_normalized_request_host(scheme, origin, host, allowed):
    policy = importlib.import_module("websocket_security").WebSocketOriginPolicy()
    scope = websocket_scope([(b"origin", origin.encode()), (b"host", host.encode())], scheme=scheme)
    assert policy.allows(scope) is allowed


INVALID_ORIGINS = [
    "", "null", "*", "localhost:8765", "//localhost:8765", "ws://localhost:8765",
    "file://localhost:8765", "http://", "http://localhost:8765/",
    "http://localhost:8765/path", "http://localhost:8765?query",
    "http://localhost:8765?", "http://localhost:8765#fragment", "http://localhost:8765#",
    "http://user@localhost:8765", "http://user:pass@localhost:8765",
    "http://localhost:8765,http://evil.example", "http://localhost:8765 http://evil.example",
    " http://localhost:8765", "http://localhost:8765 ", "http://local\thost:8765",
    "http://localhost:8765\r\n", "http://localhost:8765\\evil", "http://localhost:",
    "http://localhost:-1", "http://localhost:65536", "http://localhost:abc",
    "http://[::1", "http://::1:8765", "http://[::1]garbage:8765",
    "http://[fe80::1%25en0]:8765", "http://local%68ost:8765", "http://local\x00host:8765",
]


@pytest.mark.parametrize("origin", INVALID_ORIGINS)
def test_websocket_origin_policy_rejects_malformed_browser_origins(origin):
    policy = importlib.import_module("websocket_security").WebSocketOriginPolicy()
    assert not policy.allows(websocket_scope([
        (b"origin", origin.encode()), (b"host", b"localhost:8765"),
    ]))


@pytest.mark.parametrize("headers", [
    [(b"host", b"localhost:8765")], [],
])
def test_websocket_origin_policy_allows_non_browser_clients(headers):
    policy = importlib.import_module("websocket_security").WebSocketOriginPolicy()
    assert policy.allows(websocket_scope(headers))


@pytest.mark.parametrize("headers", [
    [(b"origin", b"http://localhost:8765"), (b"origin", b"http://localhost:8765"),
     (b"host", b"localhost:8765")],
    [(b"origin", b"https://trusted.example"), (b"origin", b"https://evil.example"),
     (b"host", b"localhost:8765")],
    [(b"origin", b"http://localhost:8765"), (b"host", b"localhost:8765"),
     (b"host", b"evil.example")],
    [(b"origin", b"https://trusted.example"), (b"host", b"localhost:8765"),
     (b"host", b"localhost:8765")],
    [(b"origin", b"http://localhost:8765")],
    [(b"origin", b"http://localhost:8765"), (b"host", b"localhost:8765/path")],
    [(b"origin", b"http://localhost:8765"), (b"host", b"localhost:8765,evil.example")],
    [(b"origin", b"http://localhost:8765"), (b"host", b"user@localhost:8765")],
    [(b"origin", b"http://[::1]:8765"), (b"host", b"[::1]garbage:8765")],
    [(b"origin", b"http://localhost:8765"), (b"host", b"localhost:8765 ")],
    [(b"origin", b"http://local\xffhost:8765"), (b"host", b"local\xffhost:8765")],
])
def test_websocket_origin_policy_rejects_ambiguous_headers(headers):
    policy = importlib.import_module("websocket_security").WebSocketOriginPolicy(
        frozenset({"https://trusted.example"}),
    )
    assert not policy.allows(websocket_scope(headers))


@pytest.mark.parametrize("origin,allowed", [
    ("https://TRUSTED.example", True), ("https://trusted.example:443", True),
    ("http://trusted.example", False), ("https://trusted.example:444", False),
    ("https://trusted.example.evil.example", False), ("https://evil.example", False),
    ("http://[::1]:5173", True),
])
def test_websocket_origin_policy_additional_origins_are_exact(origin, allowed):
    policy = importlib.import_module("websocket_security").WebSocketOriginPolicy(
        frozenset({"https://trusted.example:443", "http://[0:0:0:0:0:0:0:1]:5173"}),
    )
    scope = websocket_scope([(b"origin", origin.encode()), (b"host", b"internal:8765")])
    assert policy.allows(scope) is allowed


@pytest.mark.parametrize("value,expected", [
    ("", frozenset()), ("  ", frozenset()),
    (" HTTPS://Trusted.example,https://trusted.example:443 , http://[::1]:5173 ",
     frozenset({"https://trusted.example:443", "http://[::1]:5173"})),
])
def test_allowed_origin_configuration_is_validated_without_resources(tmp_path, monkeypatch,
                                                                   value, expected):
    monkeypatch.chdir(tmp_path)
    module = importlib.import_module("translator_runtime")
    config = module.RuntimeConfig.from_environment({"TRANSLATOR_ALLOWED_ORIGINS": value})
    assert config.allowed_origins == expected
    module.TranslatorRuntime(config)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("value", [
    "null", "*", "https://trusted.example/", "https://trusted.example?query",
    "https://user@trusted.example", "https://trusted.example:65536",
    "https://trusted.example,", ",https://trusted.example",
    "https://trusted.example,,https://other.example", "https://trusted.example,not-an-origin",
])
def test_allowed_origin_configuration_rejects_invalid_entries(value):
    module = importlib.import_module("translator_runtime")
    with pytest.raises(ValueError, match="TRANSLATOR_ALLOWED_ORIGINS"):
        module.RuntimeConfig.from_environment({"TRANSLATOR_ALLOWED_ORIGINS": value})


@pytest.mark.parametrize("root_path", ["", "/translator"])
@pytest.mark.parametrize("headers,extra_origins,allowed", [
    ([(b"host", b"localhost:8765")], "", True),
    ([(b"origin", b"http://localhost:8765"), (b"host", b"localhost:8765")], "", True),
    ([(b"origin", b"http://127.0.0.1:5173"), (b"host", b"127.0.0.1:5173")], "", True),
    ([(b"origin", b"https://public.example"), (b"host", b"public.example")], "", False),
    ([(b"origin", b"https://public.example"), (b"host", b"public.example")],
     "https://PUBLIC.example:443", True),
    ([(b"origin", b"https://public.example"), (b"host", b"internal:8765")],
     "https://PUBLIC.example:443", True),
    ([(b"origin", b"https://evil.example"), (b"host", b"localhost:8765")], "", False),
    ([(b"origin", b"null"), (b"host", b"localhost:8765")], "", False),
    ([(b"origin", b"http://localhost:8765/"), (b"host", b"localhost:8765")], "", False),
    ([(b"origin", b"http://localhost:8765"), (b"origin", b"http://localhost:8765"),
      (b"host", b"localhost:8765")], "", False),
    ([(b"origin", b"http://localhost:8765"), (b"host", b"localhost:8765"),
      (b"host", b"evil.example")], "", False),
    ([(b"origin", b"https://public.example"), (b"host", b"internal:8765"),
      (b"x-forwarded-host", b"public.example"), (b"x-forwarded-proto", b"https"),
      (b"forwarded", b"host=public.example;proto=https")], "", False),
])
def test_websocket_endpoint_enforces_origin_before_transcript_access(headers, extra_origins,
                                                                   allowed, root_path):
    assert_websocket_access(websocket_scope(headers, root_path), extra_origins, allowed)


@pytest.mark.parametrize("root_path", ["", "/translator"])
@pytest.mark.parametrize("scheme,origin,host,allowed", [
    ("wss", "http://transcripts.example", "transcripts.example", False),
    ("wss", "http://transcripts.example", "transcripts.example:443", False),
    ("wss", "http://transcripts.example:443", "transcripts.example", False),
    ("wss", "http://transcripts.example:443", "transcripts.example:443", False),
    ("wss", "http://transcripts.example:8765", "transcripts.example:8765", False),
    ("ws", "https://transcripts.example", "transcripts.example", False),
    ("ws", "https://transcripts.example", "transcripts.example:80", False),
    ("ws", "https://transcripts.example:80", "transcripts.example", False),
    ("ws", "https://transcripts.example:80", "transcripts.example:80", False),
    ("ws", "https://transcripts.example:8765", "transcripts.example:8765", False),
    ("wss", "https://transcripts.example", "transcripts.example", True),
    ("wss", "https://transcripts.example", "transcripts.example:443", True),
    ("ws", "http://transcripts.example", "transcripts.example", True),
    ("ws", "http://transcripts.example", "transcripts.example:80", True),
])
def test_websocket_endpoint_uses_trusted_scheme(scheme, origin, host, allowed, root_path):
    headers = [(b"origin", origin.encode()), (b"host", host.encode()),
               (b"x-forwarded-proto", b"https" if scheme == "ws" else b"http")]
    assert_websocket_access(websocket_scope(headers, root_path, scheme), "", allowed)


def assert_websocket_access(scope, extra_origins, allowed):
    """Exercise the real route and broadcast loop, including registration and cleanup."""
    module = importlib.import_module("translator_runtime")
    runtime = module.TranslatorRuntime(module.RuntimeConfig.from_environment({
        "TRANSLATOR_ALLOWED_ORIGINS": extra_origins,
    }))
    application = importlib.import_module("app").create_app()
    application.state.runtime = runtime
    messages = []
    client_counts = []

    async def exercise():
        connected = False
        delivered = asyncio.Event()

        async def receive():
            nonlocal connected
            if not connected:
                connected = True
                return {"type": "websocket.connect"}
            client_counts.append(len(runtime.clients))
            runtime.text_queue.put({"id": "event-1", "text": "private transcript"})
            broadcaster = asyncio.create_task(runtime.broadcast_loop())
            try:
                await asyncio.wait_for(delivered.wait(), 1)
            finally:
                broadcaster.cancel()
                with suppress(asyncio.CancelledError):
                    await broadcaster
            return {"type": "websocket.disconnect", "code": 1000}

        async def send(message):
            messages.append(message)
            client_counts.append(len(runtime.clients))
            if message["type"] == "websocket.send":
                delivered.set()

        await asyncio.wait_for(application(scope, receive, send), 2)

    asyncio.run(exercise())
    assert runtime.clients == []
    if allowed:
        assert [message["type"] for message in messages] == ["websocket.accept", "websocket.send"]
        assert json.loads(messages[1]["text"]) == {
            "type": "transcript", "event": {"id": "event-1", "text": "private transcript"},
        }
        assert 1 in client_counts
    else:
        assert [message["type"] for message in messages] == ["websocket.close"]
        assert messages[0]["code"] == 1008
        assert all(count == 0 for count in client_counts)


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
        if broadcast_failure:
            with pytest.raises(TypeError, match="not JSON serializable"):
                await asyncio.wait_for(runtime.stop(), timeout=3)
        else:
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
        if not broadcast_failure:
            await runtime.stop()

    asyncio.run(exercise())


class FailingAudioStream:
    def __init__(self, abort_error=None):
        self.aborted = False
        self.closed = False
        self.abort_error = abort_error

    def abort(self):
        self.aborted = True
        if self.abort_error is not None:
            raise self.abort_error

    def close(self):
        self.closed = True


@pytest.mark.parametrize("failure", ["caption_thread_start", "wav_close", "abort_and_wav_close"])
def test_cleanup_continues_after_a_resource_failure(tmp_path, monkeypatch, failure):
    module = importlib.import_module("translator_runtime")
    first_error = OSError(
        "abort failed" if failure == "abort_and_wav_close" else "WAV close failed",
    )

    async def exercise():
        runtime = module.TranslatorRuntime(module.RuntimeConfig.from_environment({}))
        runtime.backend = object()
        runtime.captions_dir = tmp_path / "captions"
        runtime.audio_file = tmp_path / "session.wav"
        runtime.wav_writer = wave.Wave_write(str(runtime.audio_file))
        runtime.wav_writer.setnchannels(1)
        runtime.wav_writer.setsampwidth(2)
        runtime.wav_writer.setframerate(48000)
        stream = FailingAudioStream(first_error if failure == "abort_and_wav_close" else None)
        runtime.audio_stream = stream
        runtime.broadcast_task = asyncio.create_task(runtime.broadcast_loop())

        async def receive():
            return {"type": "websocket.connect"}

        async def send(_message):
            pass

        client = module.WebSocket({"type": "websocket"}, receive, send)
        await client.accept()
        runtime.clients.append(client)
        if failure == "caption_thread_start":
            original_start = threading.Thread.start

            def fail_caption_start(thread):
                if thread._target == runtime.caption_worker:  # pylint: disable=protected-access
                    raise RuntimeError("caption thread could not start")
                original_start(thread)

            monkeypatch.setattr(threading.Thread, "start", fail_caption_start)
            upload = module.UploadFile(BytesIO(b"video"), filename="video.mp4")
            with pytest.raises(RuntimeError, match="caption thread could not start"):
                await runtime.captions_upload(upload)
            await runtime.stop()
            assert not runtime.worker_threads
        else:
            original_close = runtime.wav_writer.close

            def fail_wav_close():
                original_close()
                if failure == "wav_close":
                    raise first_error
                raise OSError("later WAV close failure")

            monkeypatch.setattr(runtime.wav_writer, "close", fail_wav_close)
            with pytest.raises(OSError) as caught:
                await runtime.stop()
            assert caught.value is first_error

        assert stream.aborted and stream.closed
        assert runtime.audio_stream is None
        assert runtime.wav_writer is None
        assert runtime.backend is None
        assert runtime.broadcast_task.done()
        assert not runtime.clients
        assert client.application_state is WebSocketState.DISCONNECTED
        with wave.open(str(runtime.audio_file), "rb") as audio:
            assert audio.getframerate() == 48000

    asyncio.run(exercise())
