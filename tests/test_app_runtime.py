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
import queue
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


def write_wav(path, *, sample_rate=16000, channels=1, sample_width=2, frames=b"\x00\x00"):
    """Write a tiny PCM WAV with controlled metadata for runtime boundary tests."""
    with wave.Wave_write(str(path)) as wav_file:
        wav_file.setframerate(sample_rate)
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(sample_width)
        wav_file.writeframes(frames)


def require_marker_before_recorder_open(module, monkeypatch):
    """Require runtime startup to claim the recording stem before opening WAV."""
    recorder_open = module.RotatingWavRecorder.open

    def open_after_marker(recorder):
        marker = recorder.directory / f"{recorder.session_stem}.jsonl"
        assert marker.is_file()
        recorder_open(recorder)

    monkeypatch.setattr(module.RotatingWavRecorder, "open", open_after_marker)


def test_fallback_live_page_keeps_a_recording_failure_alert_visible():
    template = (
        Path(__file__).resolve().parents[1] / "templates" / "index.html"
    ).read_text(encoding="utf-8")

    assert 'id="recording-alert"' in template
    assert 'role="alert"' in template
    assert "data.type === 'status'" in template
    assert "data.status === 'recording-error'" in template
    assert "recordingAlert.textContent = data.message" in template
    assert "recordingAlert.hidden = false" in template


def test_load_audio_rejects_wrong_sample_rate(tmp_path):
    module = importlib.import_module("translator_runtime")
    path = tmp_path / "wrong-rate.wav"
    write_wav(path, sample_rate=8000)

    with pytest.raises(ValueError, match="sample rate.*8000.*16000"):
        module.TranslatorRuntime(module.RuntimeConfig.from_environment({})).load_audio_16k(path)


def test_load_audio_rejects_wrong_channel_count(tmp_path):
    module = importlib.import_module("translator_runtime")
    path = tmp_path / "stereo.wav"
    write_wav(path, channels=2, frames=b"\x00\x00\x00\x00")

    with pytest.raises(ValueError, match="channels.*2.*1"):
        module.TranslatorRuntime(module.RuntimeConfig.from_environment({})).load_audio_16k(path)


def test_load_audio_rejects_wrong_sample_width(tmp_path):
    module = importlib.import_module("translator_runtime")
    path = tmp_path / "eight-bit.wav"
    write_wav(path, sample_width=1, frames=b"\x00")

    with pytest.raises(ValueError, match="sample width.*1.*2"):
        module.TranslatorRuntime(module.RuntimeConfig.from_environment({})).load_audio_16k(path)


def test_load_audio_rejects_invalid_metadata_under_optimized_python(tmp_path):
    path = tmp_path / "optimized-invalid.wav"
    write_wav(path, sample_rate=8000, channels=2, frames=b"\x00\x00\x00\x00")
    script = """
import sys
from pathlib import Path
import translator_runtime

runtime = translator_runtime.TranslatorRuntime(
    translator_runtime.RuntimeConfig.from_environment({}),
)
try:
    runtime.load_audio_16k(Path(sys.argv[1]))
except translator_runtime.InvalidAudioMetadataError as error:
    if "sample rate" not in str(error) or "8000" not in str(error):
        raise SystemExit(f"unexpected validation error: {error}")
else:
    raise SystemExit("invalid metadata was accepted")
"""
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    result = subprocess.run(
        [sys.executable, "-O", "-c", script, str(path)],
        env=environment, capture_output=True, text=True, check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_load_audio_decodes_valid_pcm_as_float32(tmp_path):
    module = importlib.import_module("translator_runtime")
    path = tmp_path / "valid.wav"
    write_wav(path, frames=b"\x01\x00\xff\x7f\xff\x7f")

    audio = module.TranslatorRuntime(module.RuntimeConfig.from_environment({})).load_audio_16k(path)

    np.testing.assert_array_equal(audio, np.array([1 / 32767, 1, 1], dtype=np.float32))
    assert audio.dtype == np.float32


def test_load_audio_rejects_trailing_partial_pcm_frame(tmp_path):
    module = importlib.import_module("translator_runtime")
    path = tmp_path / "partial-frame.wav"
    write_wav(path, frames=b"\x01\x00\x02")

    with pytest.raises(module.InvalidAudioMetadataError, match="frame data.*3.*2"):
        module.TranslatorRuntime(module.RuntimeConfig.from_environment({})).load_audio_16k(path)


def test_load_audio_rejects_partial_pcm_frame_under_optimized_python(tmp_path):
    path = tmp_path / "optimized-partial-frame.wav"
    write_wav(path, frames=b"\x01\x00\x02")
    script = """
import sys
from pathlib import Path
import translator_runtime

runtime = translator_runtime.TranslatorRuntime(
    translator_runtime.RuntimeConfig.from_environment({}),
)
try:
    runtime.load_audio_16k(Path(sys.argv[1]))
except translator_runtime.InvalidAudioMetadataError as error:
    if "frame data" not in str(error):
        raise SystemExit(f"unexpected validation error: {error}")
else:
    raise SystemExit("partial PCM frame was accepted")
"""
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    result = subprocess.run(
        [sys.executable, "-O", "-c", script, str(path)],
        env=environment, capture_output=True, text=True, check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_caption_worker_rejects_partial_pcm_frame_before_transcription(tmp_path):
    module = importlib.import_module("translator_runtime")
    transcribed = []

    class CaptionRuntime(module.TranslatorRuntime):
        def extract_audio_16k(self, _video_path, audio_path):
            write_wav(audio_path, frames=b"\x01\x00\x02")

        def transcribe_segments(self, *_args):
            transcribed.append(True)
            raise AssertionError("transcription should not be called")

    runtime = CaptionRuntime(module.RuntimeConfig.from_environment({}))
    runtime.captions_dir = tmp_path
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    video_path = job_dir / "video.mp4"
    video_path.write_bytes(b"video")
    runtime.caption_jobs["job"] = {"status": "queued", "files": []}

    runtime.caption_worker("job", video_path)

    job = runtime.caption_jobs["job"]
    assert job["status"] == "error"
    assert "frame data" in job["message"]
    assert not transcribed


def test_caption_worker_rejects_high_expansion_audio_and_cleans_artifacts(tmp_path):
    module = importlib.import_module("translator_runtime")
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    video_path = job_dir / "video.upload"
    video_path.write_bytes(b"compressed media")

    class CaptionRuntime(module.TranslatorRuntime):
        def extract_audio_16k(self, _video_path, audio_path):
            write_wav(
                audio_path,
                frames=b"\x00\x00" * (self.decoded_audio.maximum_frames + 1),
            )

        def transcribe_segments(self, *_args):
            raise AssertionError("transcription should not be called")

    runtime = CaptionRuntime(module.RuntimeConfig.from_environment({
        "TRANSLATOR_MAX_DECODED_AUDIO_BYTES": str(16000 * 2),
    }))
    runtime.captions_dir = tmp_path
    runtime.caption_jobs["job"] = {"status": "queued", "files": []}

    runtime.caption_worker("job", video_path)

    job = runtime.caption_jobs["job"]
    assert job["status"] == "error"
    assert "decoded audio" in job["message"].lower()
    assert "32000 bytes" in job["message"]
    assert "TRANSLATOR_MAX_DECODED_AUDIO_BYTES" in job["message"]
    assert not video_path.exists()
    assert not (job_dir / "audio.wav").exists()


def test_extract_audio_preserves_supported_media(tmp_path, monkeypatch):
    module = importlib.import_module("translator_runtime")
    input_path = tmp_path / "short.flac"
    output_path = tmp_path / "short.wav"
    input_path.write_bytes(b"supported media")
    runtime = module.TranslatorRuntime(module.RuntimeConfig.from_environment({
        "TRANSLATOR_MAX_DECODED_AUDIO_BYTES": str(16000 * 2),
    }))

    def run_decoder(arguments, **_options):
        assert "aresample=16000,atrim=end_sample=16001" in arguments
        assert arguments[-2] == str(output_path)
        write_wav(output_path, frames=b"\x00\x00" * 8000)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(module.subprocess, "run", run_decoder)

    assert runtime.extract_audio_16k(input_path, output_path) is None

    with wave.open(str(output_path), "rb") as wav_file:
        assert wav_file.getframerate() == 16000
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getnframes() == 8000
    assert runtime.load_audio_16k(output_path).shape == (8000,)


@pytest.mark.parametrize("setting", ["CONCURRENCY", "QUEUE_CAPACITY", "RETENTION_SECONDS"])
@pytest.mark.parametrize("value", ["0", "-1", "1.5", "bad", ""])
def test_caption_lifecycle_configuration_rejects_invalid_values(setting, value):
    module = importlib.import_module("translator_runtime")
    with pytest.raises(ValueError, match=f"TRANSLATOR_CAPTION_{setting}"):
        module.RuntimeConfig.from_environment({f"TRANSLATOR_CAPTION_{setting}": value})


def test_caption_capacity_keeps_fixed_workers_and_fifo_without_rejected_artifacts(upload_runtime):
    runtime, entered, inputs = upload_runtime
    module = importlib.import_module("translator_runtime")

    async def exercise():
        responses = []
        for index in range(4):
            upload = module.UploadFile(BytesIO(str(index).encode()), filename="video.mp4")
            responses.append(await runtime.captions_upload(upload))
            assert upload.file.closed
        assert entered.wait(2)
        assert [response.status_code for response in responses] == [200, 200, 200, 429]
        assert int(responses[-1].headers["Retry-After"]) > 0
        assert len(runtime.caption_jobs) == 3
        assert len(list(runtime.captions_dir.iterdir())) == 3
        assert len(inputs) == 1
        assert sum(worker.name.startswith("caption-worker-") for worker in
                   runtime.worker_threads) == 1
        ids = [json.loads(response.body)["job_id"] for response in responses[:3]]
        assert [runtime.caption_jobs[job_id]["status"] for job_id in ids] == [
            "processing", "queued", "queued",
        ]
        return ids

    ids = asyncio.run(exercise())
    # Shutdown cancels queued work immediately, while the active extraction is bounded.
    asyncio.run(runtime.stop())
    assert [runtime.caption_jobs[job_id]["status"] for job_id in ids[1:]] == ["error", "error"]
    assert all("completed_at" in runtime.caption_jobs[job_id] for job_id in ids[1:])


def test_caption_terminal_retention_removes_records_downloads_and_recursive_artifacts(
    tmp_path, monkeypatch,
):
    module = importlib.import_module("translator_runtime")
    now = [1000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])

    class FailedRuntime(module.TranslatorRuntime):  # pylint: disable=too-few-public-methods
        def extract_audio_16k(self, _video_path, audio_path):
            audio_path.write_bytes(b"partial audio")
            (audio_path.parent / "leftovers").mkdir()
            (audio_path.parent / "leftovers" / "partial").write_bytes(b"partial")
            return "invalid video"

    runtime = FailedRuntime(module.RuntimeConfig.from_environment({
        "TRANSLATOR_CAPTION_RETENTION_SECONDS": "10",
    }))
    runtime.captions_dir = tmp_path / "captions"
    try:
        response = asyncio.run(runtime.captions_upload(
            module.UploadFile(BytesIO(b"video"), filename="video.mp4"),
        ))
        job_id = json.loads(response.body)["job_id"]
        wait_until(lambda: runtime.caption_jobs[job_id]["status"] == "error")
        assert runtime.caption_jobs[job_id].get("completed_at") == 1000.0
        now[0] = 1009
        assert asyncio.run(runtime.captions_status(job_id)).status_code == 200
        now[0] = 1010
        assert asyncio.run(runtime.captions_download(job_id, "audio.wav")).status_code == 404
        assert asyncio.run(runtime.captions_status(job_id)).status_code == 404
        assert not (runtime.captions_dir / job_id).exists()
    finally:
        asyncio.run(runtime.stop())


def test_caption_sweep_removes_only_old_generated_orphans(tmp_path):
    module = importlib.import_module("translator_runtime")
    runtime = module.TranslatorRuntime(module.RuntimeConfig.from_environment({
        "TRANSLATOR_CAPTION_RETENTION_SECONDS": "10",
    }))
    runtime.captions_dir = tmp_path / "captions"
    runtime.captions_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("safe")
    for name in ("0123456789ab", "abcdefabcdef", "non-job-directory", "111111111111"):
        directory = runtime.captions_dir / name
        directory.mkdir()
        (directory / "leftover").write_text("data")
        os.utime(directory, (1, 1))
    os.utime(runtime.captions_dir / "abcdefabcdef", None)
    runtime.caption_jobs["111111111111"] = {"status": "queued"}
    (runtime.captions_dir / "222222222222").symlink_to(outside, target_is_directory=True)
    asyncio.run(runtime.captions_status("missing"))
    assert not (runtime.captions_dir / "0123456789ab").exists()
    for name in ("abcdefabcdef", "non-job-directory", "111111111111", "222222222222"):
        assert (runtime.captions_dir / name).exists()
    assert (outside / "keep").read_text() == "safe"


def test_caption_and_live_backend_calls_are_serialized_including_lazy_segments(tmp_path):
    module = importlib.import_module("translator_runtime")
    importlib.import_module("scipy.signal")
    active = 0
    maximum = 0
    lock = threading.Lock()

    class Backend:  # pylint: disable=too-few-public-methods
        name = "tracked"

        def transcribe(self, *_args, **_kwargs):
            def segments():
                nonlocal active, maximum
                with lock:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.05)
                yield SimpleNamespace(start=0, end=1, text="speech")
                with lock:
                    active -= 1
            return segments(), "en"

    runtime = module.TranslatorRuntime(module.RuntimeConfig.from_environment({}))
    runtime.backend = Backend()
    runtime.transcript_file = tmp_path / "session.jsonl"
    workers = [threading.Thread(target=runtime.transcribe_segments,
                               args=(np.zeros(16000), {})) for _ in range(3)]
    workers.append(threading.Thread(target=runtime._transcribe_audio,  # pylint: disable=protected-access
                                    args=(np.zeros(48000),)))
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(2)
        assert not worker.is_alive()
    assert maximum == 1


def test_caption_upload_reservations_count_toward_capacity_and_cancel_cleanly(tmp_path):
    module = importlib.import_module("translator_runtime")
    runtime = module.TranslatorRuntime(module.RuntimeConfig.from_environment({
        "TRANSLATOR_CAPTION_CONCURRENCY": "1", "TRANSLATOR_CAPTION_QUEUE_CAPACITY": "1",
    }))
    runtime.captions_dir = tmp_path / "captions"

    async def exercise():
        entered = asyncio.Queue()
        hold = asyncio.Event()

        class SlowUpload(module.UploadFile):  # pylint: disable=too-few-public-methods
            async def read(self, size=-1):
                await entered.put(True)
                await hold.wait()
                return await super().read(size)

        uploads = [SlowUpload(BytesIO(b"video"), filename="video.mp4") for _ in range(2)]
        tasks = [asyncio.create_task(runtime.captions_upload(upload)) for upload in uploads]
        try:
            await asyncio.wait_for(entered.get(), 2)
            await asyncio.wait_for(entered.get(), 2)
            rejected = module.UploadFile(BytesIO(b"excess"), filename="video.mp4")
            response = await runtime.captions_upload(rejected)
            assert response.status_code == 429
            assert rejected.file.closed
            assert len(list(runtime.captions_dir.iterdir())) == 2
            assert runtime.caption_jobs == {}
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            assert all(upload.file.closed for upload in uploads)
            assert not list(runtime.captions_dir.iterdir())
        # Failed uploads relinquish both slots; stopping also prevents submission.
        assert runtime.caption_manager.reserve("0123456789ab")
        assert runtime.caption_manager.reserve("abcdefabcdef")
        await runtime.stop()
        with pytest.raises(RuntimeError, match="stopped"):
            runtime.caption_manager.submit("0123456789ab", tmp_path / "video", "video")

    asyncio.run(exercise())


def test_caption_workers_finish_fifo_and_expire_success_artifacts(tmp_path, monkeypatch):
    # pylint: disable=too-many-locals
    module = importlib.import_module("translator_runtime")
    release, entered = threading.Event(), threading.Event()
    order = []

    class CaptionRuntime(module.TranslatorRuntime):  # pylint: disable=too-few-public-methods
        def extract_audio_16k(self, video_path, audio_path):
            order.append(video_path.read_bytes())
            entered.set()
            assert release.wait(5)
            with wave.Wave_write(str(audio_path)) as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(16000)
                audio.writeframes(b"\0\0" * 16000)

    class Backend:
        def transcribe(self, *_args, **_kwargs):
            return [SimpleNamespace(start=0, end=1, text="Hello")], "en"

        def detect_language(self, _audio):
            return "en", 1

    packages = SimpleNamespace(get_installed_packages=lambda: [
        SimpleNamespace(from_code="en", to_code="es"),
        SimpleNamespace(from_code="es", to_code="en"),
    ])
    translation = SimpleNamespace(translate=lambda *_args: "Hola")
    argos = SimpleNamespace(package=packages, translate=translation)
    monkeypatch.setitem(sys.modules, "argostranslate", argos)
    monkeypatch.setitem(sys.modules, "argostranslate.package", packages)
    monkeypatch.setitem(sys.modules, "argostranslate.translate", translation)
    runtime = CaptionRuntime(module.RuntimeConfig.from_environment({
        "TRANSLATOR_CAPTION_RETENTION_SECONDS": "10",
    }))
    runtime.backend = Backend()
    runtime.captions_dir = tmp_path / "captions"
    ids = []
    try:
        for index in range(3):
            response = asyncio.run(runtime.captions_upload(module.UploadFile(
                BytesIO(str(index).encode()), filename="video.mp4",
            )))
            ids.append(json.loads(response.body)["job_id"])
        assert entered.wait(2)
        release.set()
        wait_until(lambda: all("completed_at" in runtime.caption_jobs[job_id] for job_id in ids))
        assert order == [b"0", b"1", b"2"]
        assert all(runtime.caption_jobs[job_id]["status"] == "done" for job_id in ids)
        job_id = ids[0]
        filename = runtime.caption_jobs[job_id]["files"][0]
        assert asyncio.run(request_caption_download(runtime, job_id, filename)).status_code == 200
        assert "Hello" in (runtime.captions_dir / job_id / filename).read_text()
        finished = max(job["completed_at"] for job in runtime.caption_jobs.values())
        monkeypatch.setattr(time, "time", lambda: finished + 10)
        assert asyncio.run(runtime.captions_status(job_id)).status_code == 404
        assert not list(runtime.captions_dir.iterdir())
    finally:
        release.set()
        asyncio.run(runtime.stop())


def test_caption_translation_installation_and_translation_do_not_overlap(monkeypatch):
    module = importlib.import_module("translator_runtime")
    entered, release = threading.Event(), threading.Event()
    overlap = []

    def installed():
        entered.set()
        assert release.wait(3)
        return [SimpleNamespace(from_code="en", to_code="es"),
                SimpleNamespace(from_code="es", to_code="en")]

    def translate(*_args):
        overlap.append(not release.is_set())
        return "Hola"

    packages = SimpleNamespace(get_installed_packages=installed)
    translation = SimpleNamespace(translate=translate)
    monkeypatch.setitem(sys.modules, "argostranslate", SimpleNamespace(
        package=packages, translate=translation,
    ))
    monkeypatch.setitem(sys.modules, "argostranslate.package", packages)
    monkeypatch.setitem(sys.modules, "argostranslate.translate", translation)
    runtime = module.TranslatorRuntime(module.RuntimeConfig.from_environment({}))
    installer = threading.Thread(target=runtime.ensure_argos_packages, args=({},))
    translator = threading.Thread(target=runtime.build_srt_entries, args=([
        {"start": 0, "end": 1, "text": "Hello", "language": "en"},
    ], {}))
    try:
        installer.start()
        assert entered.wait(2)
        translator.start()
        time.sleep(0.05)
    finally:
        release.set()
        installer.join(2)
        translator.join(2)
    assert overlap == [False]


def test_caption_cleanup_does_not_follow_root_symlink(tmp_path):
    module = importlib.import_module("translator_runtime")
    runtime = module.TranslatorRuntime(module.RuntimeConfig.from_environment({}))
    outside = tmp_path / "outside"
    job_dir = outside / "0123456789ab"
    job_dir.mkdir(parents=True)
    (job_dir / "keep").write_text("safe")
    runtime.captions_dir = tmp_path / "captions"
    runtime.captions_dir.symlink_to(outside, target_is_directory=True)
    runtime.caption_jobs["0123456789ab"] = {"status": "done", "completed_at": 1}
    asyncio.run(runtime.captions_status("0123456789ab"))
    assert (job_dir / "keep").read_text() == "safe"


def test_caption_configured_concurrency_and_http_capacity(tmp_path, monkeypatch):
    module = importlib.import_module("translator_runtime")
    application = importlib.import_module("app")
    release = threading.Event()
    entered = queue.Queue()

    class Runtime(module.TranslatorRuntime):  # pylint: disable=too-few-public-methods
        async def start(self):
            pass

        def extract_audio_16k(self, video_path, _audio_path):
            entered.put(video_path)
            assert release.wait(5)
            return "fixture finished"

    runtime = Runtime(module.RuntimeConfig.from_environment({
        "TRANSLATOR_CAPTION_CONCURRENCY": "2", "TRANSLATOR_CAPTION_QUEUE_CAPACITY": "1",
    }))
    runtime.captions_dir = tmp_path / "captions"
    monkeypatch.chdir(tmp_path)

    async def exercise():
        app = application.create_app(lambda: runtime)
        async with app.router.lifespan_context(app):
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                             base_url="http://test") as client:
                    responses = [await client.post("/captions/upload", files={
                        "file": ("video.mp4", b"video"),
                    }) for _ in range(4)]
                    assert [response.status_code for response in responses] == [200, 200, 200, 429]
                    assert "capacity" in responses[-1].json()["error"]
                    entered.get(timeout=2)
                    entered.get(timeout=2)
                    assert entered.empty()
                    assert len(list(runtime.captions_dir.iterdir())) == 3
                    assert sum(worker.name.startswith("caption-worker-") for worker in
                               runtime.worker_threads) == 2
            finally:
                release.set()

    asyncio.run(exercise())


def test_caption_periodic_sweep_expires_failed_job_without_requests(tmp_path):
    module = importlib.import_module("translator_runtime")

    class Runtime(module.TranslatorRuntime):  # pylint: disable=too-few-public-methods
        def extract_audio_16k(self, _video_path, _audio_path):
            raise OSError("broken media")

    runtime = Runtime(module.RuntimeConfig.from_environment({
        "TRANSLATOR_CAPTION_RETENTION_SECONDS": "1",
    }))
    runtime.captions_dir = tmp_path / "captions"
    try:
        response = asyncio.run(runtime.captions_upload(module.UploadFile(
            BytesIO(b"video"), filename="video.mp4",
        )))
        job_id = json.loads(response.body)["job_id"]
        wait_until(lambda: "completed_at" in runtime.caption_jobs[job_id])
        assert runtime.caption_jobs[job_id]["message"] == "broken media"
        wait_until(lambda: job_id not in runtime.caption_jobs)
        wait_until(lambda: not (runtime.captions_dir / job_id).exists())
    finally:
        asyncio.run(runtime.stop())


def test_caption_shutdown_aborts_upload_at_next_chunk_without_storing_it(tmp_path):
    module = importlib.import_module("translator_runtime")
    runtime = module.TranslatorRuntime(module.RuntimeConfig.from_environment({}))
    runtime.captions_dir = tmp_path / "captions"
    reads = []

    class InterruptedUpload(module.UploadFile):  # pylint: disable=too-few-public-methods
        async def read(self, size=-1):
            reads.append(size)
            if len(reads) == 1:
                await runtime.stop()
            return await super().read(size)

    upload = InterruptedUpload(BytesIO(b"v" * 2097152), filename="video.mp4")
    with pytest.raises(RuntimeError, match="Runtime stopped"):
        asyncio.run(runtime.captions_upload(upload))
    assert len(reads) == 1
    assert upload.file.closed
    assert not list(runtime.captions_dir.iterdir())
    assert not runtime.caption_jobs


def test_caption_expiry_hides_jobs_when_artifact_removal_needs_retry(tmp_path, monkeypatch):
    module = importlib.import_module("translator_runtime")
    runtime = module.TranslatorRuntime(module.RuntimeConfig.from_environment({}))
    runtime.captions_dir = tmp_path / "captions"
    job_id = "0123456789ab"
    job_dir = runtime.captions_dir / job_id
    job_dir.mkdir(parents=True)
    (job_dir / "video.original.srt").write_text("private captions")
    os.utime(job_dir, (1, 1))
    runtime.caption_jobs[job_id] = {"status": "done", "completed_at": 1,
                                    "files": ["video.original.srt"]}
    manager_module = importlib.import_module("caption_jobs")

    def cannot_remove(_path):
        raise PermissionError("busy filesystem")

    with monkeypatch.context() as context:
        context.setattr(manager_module.shutil, "rmtree", cannot_remove)
        assert asyncio.run(runtime.captions_status(job_id)).status_code == 404
        download = asyncio.run(runtime.captions_download(job_id, "video.original.srt"))
        assert download.status_code == 404
        assert job_dir.exists()
    asyncio.run(runtime.captions_status(job_id))
    assert not job_dir.exists()


def test_caption_slow_cleanup_keeps_event_loop_stop_and_reservations_responsive(
    tmp_path, monkeypatch,
):
    module = importlib.import_module("translator_runtime")
    manager_module = importlib.import_module("caption_jobs")
    runtime = module.TranslatorRuntime(module.RuntimeConfig.from_environment({}))
    runtime.captions_dir = tmp_path / "captions"
    job_id = "0123456789ab"
    directory = runtime.captions_dir / job_id
    directory.mkdir(parents=True)
    runtime.caption_jobs[job_id] = {"status": "error", "completed_at": 1}
    entered, release = threading.Event(), threading.Event()
    original_remove = manager_module.shutil.rmtree

    def slow_remove(path):
        entered.set()
        assert release.wait(5)
        original_remove(path)

    monkeypatch.setattr(manager_module.shutil, "rmtree", slow_remove)

    async def exercise():
        began = time.monotonic()
        response_task = asyncio.create_task(runtime.captions_status(job_id))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            await asyncio.sleep(0.01)
            assert time.monotonic() - began < 0.5
            assert job_id not in runtime.caption_jobs
            concurrent_status = await runtime.captions_status(job_id)
            assert concurrent_status.status_code == 404
            # Reusing a selected cleanup ID must fail until its directory is gone.
            assert not runtime.caption_manager.reserve(job_id)
            assert runtime.caption_manager.reserve("abcdefabcdef")
            await runtime.stop()
            assert time.monotonic() - began < 0.5
        finally:
            release.set()
            await response_task
        assert not directory.exists()

    # Release eventually even if the old synchronous sweep blocks the event loop.
    timer = threading.Timer(1, release.set)
    timer.start()
    try:
        asyncio.run(exercise())
    finally:
        release.set()
        timer.cancel()


@pytest.mark.parametrize("failure", [PermissionError("temporary access loss"),
                                    RuntimeError("unexpected filesystem adapter failure")])
def test_caption_periodic_sweeper_recovers_after_root_listing_failure(
    tmp_path, monkeypatch, caplog, failure,
):
    module = importlib.import_module("translator_runtime")
    runtime = module.TranslatorRuntime(module.RuntimeConfig.from_environment({
        "TRANSLATOR_CAPTION_RETENTION_SECONDS": "1",
    }))
    runtime.captions_dir = tmp_path / "captions"
    orphan = runtime.captions_dir / "0123456789ab"
    orphan.mkdir(parents=True)
    (orphan / "old-upload").write_text("data")
    os.utime(orphan, (1, 1))
    failed = threading.Event()
    original_iterdir = Path.iterdir

    def flaky_iterdir(path):
        if path == runtime.captions_dir and not failed.is_set():
            failed.set()
            raise failure
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", flaky_iterdir)
    # Exercise the real background loop directly, including its recovery boundary.
    sweeper = threading.Thread(target=runtime.caption_manager._sweep_loop,  # pylint: disable=protected-access
                               name="caption-retention")
    runtime.worker_threads.append(sweeper)
    sweeper.start()
    try:
        assert failed.wait(2)
        wait_until(lambda: not orphan.exists())
        assert sweeper.is_alive()
        assert str(failure) in caplog.text
    finally:
        asyncio.run(runtime.stop())


def test_caption_orphan_selection_cannot_delete_a_new_reservation(tmp_path, monkeypatch):
    module = importlib.import_module("translator_runtime")
    runtime = module.TranslatorRuntime(module.RuntimeConfig.from_environment({}))
    runtime.captions_dir = tmp_path / "captions"
    job_id = "0123456789ab"
    directory = runtime.captions_dir / job_id
    directory.mkdir(parents=True)
    os.utime(directory, (1, 1))
    inspected, release = threading.Event(), threading.Event()
    original_clean_unowned = (
        runtime.caption_manager._clean_unowned  # pylint: disable=protected-access
    )

    def paused_clean_unowned(selected_job_id):
        if selected_job_id == job_id and threading.current_thread().name == "orphan-sweep":
            inspected.set()
            assert release.wait(3)
        original_clean_unowned(selected_job_id)

    monkeypatch.setattr(runtime.caption_manager, "_clean_unowned", paused_clean_unowned)
    sweeper = threading.Thread(target=runtime.caption_manager.sweep, name="orphan-sweep")
    sweeper.start()
    try:
        assert inspected.wait(2)
        assert runtime.caption_manager.reserve(job_id)
        # The uploader owns the ID before the old selection is converted into a claim.
        directory.rmdir()
        directory.mkdir()
        (directory / "new-upload").write_bytes(b"new data")
    finally:
        release.set()
        sweeper.join(3)
    assert not sweeper.is_alive()
    assert (directory / "new-upload").read_bytes() == b"new data"
    runtime.caption_manager.cancel_upload(job_id)


@pytest.fixture(name="download_runtime")
def fixture_download_runtime(tmp_path):
    module = importlib.import_module("translator_runtime")

    class Runtime(module.TranslatorRuntime):  # pylint: disable=too-few-public-methods
        async def start(self):
            pass

    runtime = Runtime(module.RuntimeConfig.from_environment({
        "TRANSLATOR_CAPTION_RETENTION_SECONDS": "1",
    }))
    runtime.captions_dir = tmp_path / "captions"
    job_id = "0123456789ab"
    path = runtime.captions_dir / job_id / "video.original.srt"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"1\n00:00:00,000 --> 00:00:01,000\nHello\n")
    runtime.caption_jobs[job_id] = {"status": "done", "completed_at": 1000,
                                    "files": [path.name]}
    return runtime, job_id, path


def test_caption_download_body_survives_expiry_after_response_headers(
    download_runtime, monkeypatch,
):
    runtime, job_id, path = download_runtime
    application = importlib.import_module("app")
    now = [1000.9]
    monkeypatch.setattr(time, "time", lambda: now[0])
    messages = []
    content = path.read_bytes()

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)
        if message["type"] == "http.response.start":
            assert message["status"] == 200
            now[0] = 1001
            await asyncio.to_thread(runtime.caption_manager.sweep)
            assert job_id not in runtime.caption_jobs
            assert path.exists()  # An accepted stream owns the file through its final body.

    async def exercise():
        app = application.create_app(lambda: runtime)
        async with app.router.lifespan_context(app):
            await app({"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
                       "http_version": "1.1",
                       "method": "GET", "scheme": "http", "headers": [], "query_string": b"",
                       "path": f"/captions/download/{job_id}/{path.name}", "root_path": "",
                       "server": ("test", 80), "client": ("test", 1234)}, receive, send)

    asyncio.run(exercise())
    assert b"".join(message.get("body", b"") for message in messages) == content
    headers = dict(messages[0]["headers"])
    assert headers[b"content-type"] == b"application/x-subrip"
    assert headers[b"content-length"] == str(len(content)).encode()
    assert headers[b"content-disposition"] == b'attachment; filename="video.original.srt"'
    runtime.caption_manager.sweep()
    assert not path.exists()


async def request_caption_download(runtime, job_id, filename):
    """Send the real ASGI route without acquiring unrelated audio hardware."""
    app = importlib.import_module("app").create_app(lambda: runtime)
    app.state.runtime = runtime
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        return await client.get(f"/captions/download/{job_id}/{filename}")


@pytest.mark.parametrize("status, filename, published", [
    ("processing", "audio.wav", []), ("error", "audio.wav", []),
    ("done", "audio.wav", ["video.original.srt"]),
    ("done", "original.upload", ["video.original.srt"]),
    ("processing", "video.original.srt", ["video.original.srt"]),
])
def test_caption_download_rejects_unpublished_working_files(
    download_runtime, monkeypatch, status, filename, published,
):
    runtime, job_id, path = download_runtime
    monkeypatch.setattr(time, "time", lambda: 1000.9)
    (path.parent / filename).write_bytes(b"private working data")
    runtime.caption_jobs[job_id].update(status=status, files=published)
    response = asyncio.run(request_caption_download(runtime, job_id, filename))
    assert response.status_code == 404


def test_caption_download_streams_large_published_subtitle_in_bounded_frames(
    download_runtime, monkeypatch,
):
    runtime, job_id, path = download_runtime
    monkeypatch.setattr(time, "time", lambda: 1000.9)
    content = b"subtitle data\n" * 100000
    path.write_bytes(content)
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    async def exercise():
        app = importlib.import_module("app").create_app(lambda: runtime)
        app.state.runtime = runtime
        await app({"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
                   "http_version": "1.1", "method": "GET", "scheme": "http", "headers": [],
                   "query_string": b"", "path": f"/captions/download/{job_id}/{path.name}",
                   "root_path": "", "server": ("test", 80), "client": ("test", 1234)},
                  receive, send)

    asyncio.run(exercise())
    bodies = [message["body"] for message in messages if message["type"] == "http.response.body"]
    assert b"".join(bodies) == content
    assert max(map(len, bodies)) <= 65536
    assert dict(messages[0]["headers"])[b"content-length"] == str(len(content)).encode()


def test_caption_download_claim_protects_open_and_rejects_requests_after_retirement(
    download_runtime, monkeypatch,
):
    runtime, job_id, path = download_runtime
    application = importlib.import_module("app")
    now = [1000.9]
    monkeypatch.setattr(time, "time", lambda: now[0])
    reading, release = threading.Event(), threading.Event()
    original_open = Path.open
    content = path.read_bytes()

    def slow_open(candidate, *args, **kwargs):
        if candidate == path:
            reading.set()
            assert release.wait(3)
        return original_open(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "open", slow_open)

    async def exercise():
        app = application.create_app(lambda: runtime)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                request = asyncio.create_task(client.get(
                    f"/captions/download/{job_id}/{path.name}",
                ))
                try:
                    assert await asyncio.to_thread(reading.wait, 1)
                    now[0] = 1001
                    await asyncio.wait_for(asyncio.to_thread(runtime.caption_manager.sweep), 0.5)
                    assert job_id not in runtime.caption_jobs
                    assert path.exists()
                    rejected = await client.get(f"/captions/download/{job_id}/{path.name}")
                    assert rejected.status_code == 404
                finally:
                    release.set()
                    response = await request
                assert response.status_code == 200
                assert response.content == content
                await asyncio.to_thread(runtime.caption_manager.sweep)
                assert not path.exists()

    asyncio.run(exercise())


def test_caption_download_open_failure_releases_cleanup_claim(download_runtime, monkeypatch):
    runtime, job_id, path = download_runtime
    now = [1000.9]
    monkeypatch.setattr(time, "time", lambda: now[0])

    def unreadable(_path, *_args, **_kwargs):
        raise PermissionError("temporarily unreadable")

    monkeypatch.setattr(Path, "open", unreadable)
    response = asyncio.run(runtime.captions_download(job_id, path.name))
    assert response.status_code == 404
    now[0] = 1001
    runtime.caption_manager.sweep()
    assert not path.exists()


def test_caption_download_started_after_cleanup_claim_is_404(download_runtime, monkeypatch):
    runtime, job_id, path = download_runtime
    application = importlib.import_module("app")
    manager_module = importlib.import_module("caption_jobs")
    monkeypatch.setattr(time, "time", lambda: 1001)
    cleaning, release = threading.Event(), threading.Event()
    original_remove = manager_module.shutil.rmtree

    def slow_remove(directory):
        cleaning.set()
        assert release.wait(3)
        original_remove(directory)

    monkeypatch.setattr(manager_module.shutil, "rmtree", slow_remove)
    sweeper = threading.Thread(target=runtime.caption_manager.sweep)
    sweeper.start()

    async def exercise():
        app = application.create_app(lambda: runtime)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                response = await asyncio.wait_for(client.get(
                    f"/captions/download/{job_id}/{path.name}",
                ), 0.5)
                assert response.status_code == 404

    try:
        assert cleaning.wait(2)
        asyncio.run(exercise())
    finally:
        release.set()
        sweeper.join(3)
    assert not path.exists()


def test_caption_canceled_download_keeps_lease_until_background_open_finishes(
    download_runtime, monkeypatch,
):
    runtime, job_id, path = download_runtime
    now = [1000.9]
    monkeypatch.setattr(time, "time", lambda: now[0])
    reading, release, finished = threading.Event(), threading.Event(), threading.Event()
    original_open = Path.open

    def slow_open(candidate, *args, **kwargs):
        reading.set()
        assert release.wait(3)
        try:
            return original_open(candidate, *args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(Path, "open", slow_open)

    async def exercise():
        request = asyncio.create_task(runtime.captions_download(job_id, path.name))
        try:
            assert await asyncio.to_thread(reading.wait, 1)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            now[0] = 1001
            await asyncio.to_thread(runtime.caption_manager.sweep)
            assert job_id not in runtime.caption_jobs
            assert path.exists()
        finally:
            release.set()
            assert await asyncio.to_thread(finished.wait, 1)
        # Abandoned acquisition closes its result after the background open returns.
        for _ in range(100):
            await asyncio.to_thread(runtime.caption_manager.sweep)
            if not path.exists():
                break
            await asyncio.sleep(0.005)
        assert not path.exists()

    asyncio.run(exercise())


def test_caption_download_encodes_attachment_filename(download_runtime, monkeypatch):
    runtime, job_id, path = download_runtime
    monkeypatch.setattr(time, "time", lambda: 1000.9)
    renamed = path.with_name('résumé "original".srt')
    path.rename(renamed)
    runtime.caption_jobs[job_id]["files"] = [renamed.name]
    response = asyncio.run(request_caption_download(runtime, job_id, renamed.name))
    assert response.status_code == 200
    assert response.headers["Content-Disposition"] == (
        "attachment; filename*=utf-8''r%C3%A9sum%C3%A9%20%22original%22.srt"
    )


@pytest.mark.parametrize("ending", ["complete", "send-error", "read-error", "cancel", "disconnect"])
def test_caption_stream_finalization_closes_once_and_releases_retention(
    download_runtime, monkeypatch, ending,
):
    # pylint: disable=too-many-locals,too-many-statements
    runtime, job_id, path = download_runtime
    path.write_bytes(b"subtitles\n" * 20000)
    now = [1000.9]
    monkeypatch.setattr(time, "time", lambda: now[0])
    original_open = Path.open
    handles = []

    class ObservedFile:
        def __init__(self, stream):
            self.stream = stream
            self.reads = []
            self.closes = 0

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            self.reads.append(size)
            if ending == "read-error" and len(self.reads) == 2:
                raise OSError("disk read failed")
            return self.stream.read(size)

        def close(self):
            self.closes += 1
            self.stream.close()

    def observed_open(candidate, *args, **kwargs):
        stream = original_open(candidate, *args, **kwargs)  # pylint: disable=consider-using-with
        if candidate != path:
            return stream
        handle = ObservedFile(stream)
        handles.append(handle)
        return handle

    monkeypatch.setattr(Path, "open", observed_open)

    async def exercise():
        app = importlib.import_module("app").create_app(lambda: runtime)
        app.state.runtime = runtime
        disconnect = asyncio.Event()

        async def receive():
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                assert message["status"] == 200
                now[0] = 1001
                await asyncio.to_thread(runtime.caption_manager.sweep)
                assert path.exists()
            elif message.get("body"):
                assert len(message["body"]) <= 65536
                if ending == "send-error":
                    raise ConnectionError("peer disconnected")
                if ending == "cancel":
                    raise asyncio.CancelledError
                if ending == "disconnect":
                    disconnect.set()
                    await asyncio.sleep(0)

        call = app({"type": "http", "asgi": {"version": "3.0", "spec_version":
                    "2.0" if ending == "disconnect" else "2.4"}, "http_version": "1.1",
                    "method": "GET", "scheme": "http", "headers": [], "query_string": b"",
                    "path": f"/captions/download/{job_id}/{path.name}", "root_path": "",
                    "server": ("test", 80), "client": ("test", 1234)}, receive, send)
        if ending == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await call
        elif ending in ("send-error", "read-error"):
            with pytest.raises(importlib.import_module("starlette.requests").ClientDisconnect):
                await call
        else:
            await call
        await asyncio.to_thread(runtime.caption_manager.sweep)

    asyncio.run(exercise())
    assert len(handles) == 1
    assert handles[0].closes == 1
    assert handles[0].stream.closed
    assert all(size <= 65536 for size in handles[0].reads)
    assert not path.exists()


@pytest.mark.parametrize("second_ending", ["complete", "cancel"])
def test_caption_multiple_streams_keep_independent_retention_leases(
    download_runtime, monkeypatch, second_ending,
):
    runtime, job_id, path = download_runtime
    now = [1000.9]
    monkeypatch.setattr(time, "time", lambda: now[0])

    async def exercise():
        app = importlib.import_module("app").create_app(lambda: runtime)
        app.state.runtime = runtime
        entered = [asyncio.Event(), asyncio.Event()]
        release = [asyncio.Event(), asyncio.Event()]

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def stream(index):
            async def send(message):
                if message["type"] == "http.response.start":
                    assert message["status"] == 200
                    entered[index].set()
                    await release[index].wait()

            await app({"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
                       "http_version": "1.1", "method": "GET", "scheme": "http", "headers": [],
                       "query_string": b"", "path": f"/captions/download/{job_id}/{path.name}",
                       "root_path": "", "server": ("test", 80), "client": ("test", 1234)},
                      receive, send)

        tasks = [asyncio.create_task(stream(index)) for index in range(2)]
        try:
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered)), 1)
            now[0] = 1001
            await asyncio.to_thread(runtime.caption_manager.sweep)
            assert job_id not in runtime.caption_jobs
            assert path.exists()
            release[0].set()
            await tasks[0]
            await asyncio.to_thread(runtime.caption_manager.sweep)
            assert path.exists()
            if second_ending == "cancel":
                tasks[1].cancel()
                with pytest.raises(asyncio.CancelledError):
                    await tasks[1]
            else:
                release[1].set()
                await tasks[1]
            await asyncio.to_thread(runtime.caption_manager.sweep)
            assert not path.exists()
        finally:
            for event in release:
                event.set()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(exercise())


def test_caption_cancel_during_inflight_read_closes_after_read_returns(
    download_runtime, monkeypatch,
):
    # pylint: disable=too-many-locals
    runtime, job_id, path = download_runtime
    now = [1000.9]
    monkeypatch.setattr(time, "time", lambda: now[0])
    reading, release = threading.Event(), threading.Event()
    original_open = Path.open
    handles = []

    class SlowFile:
        def __init__(self, stream):
            self.stream = stream
            self.closes = 0

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            reading.set()
            assert release.wait(3)
            return self.stream.read(size)

        def close(self):
            self.closes += 1
            self.stream.close()

    def slow_open(candidate, *args, **kwargs):
        stream = original_open(candidate, *args, **kwargs)  # pylint: disable=consider-using-with
        if candidate != path:
            return stream
        handle = SlowFile(stream)
        handles.append(handle)
        return handle

    monkeypatch.setattr(Path, "open", slow_open)

    async def exercise():
        request = asyncio.create_task(request_caption_download(runtime, job_id, path.name))
        try:
            assert await asyncio.to_thread(reading.wait, 1)
            request.cancel()
            now[0] = 1001
            await asyncio.wait_for(asyncio.to_thread(runtime.caption_manager.sweep), 0.5)
            assert path.exists()
            assert not handles[0].stream.closed
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await request
        await asyncio.to_thread(wait_until, lambda: handles[0].stream.closed)
        await asyncio.to_thread(runtime.caption_manager.sweep)
        assert not path.exists()

    asyncio.run(exercise())
    assert handles[0].closes == 1
    assert handles[0].stream.closed


@pytest.mark.parametrize("shutdown", ["asyncio-run", "closed-loop", "executor-unavailable"])
def test_caption_abandoned_open_closes_without_event_loop_or_executor(
    download_runtime, monkeypatch, shutdown,
):
    # pylint: disable=too-many-locals,too-many-statements
    runtime, job_id, path = download_runtime
    now = [1000.9]
    monkeypatch.setattr(time, "time", lambda: now[0])
    entered, release = threading.Event(), threading.Event()
    original_open = Path.open
    handles = []
    loop_errors = []

    class ObservedFile:
        def __init__(self, stream):
            self.stream = stream
            self.closes = 0

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            return self.stream.read(size)

        def close(self):
            self.closes += 1
            self.stream.close()

    def blocked_open(candidate, *args, **kwargs):
        if candidate == path:
            entered.set()
            assert release.wait(5)
        stream = original_open(candidate, *args, **kwargs)  # pylint: disable=consider-using-with
        if candidate != path:
            return stream
        handle = ObservedFile(stream)
        handles.append(handle)
        return handle

    monkeypatch.setattr(Path, "open", blocked_open)

    async def await_open():
        deadline = time.monotonic() + 2
        while not entered.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        assert entered.is_set()

    async def exercise():
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
        request = asyncio.create_task(runtime.captions_download(job_id, path.name))
        await await_open()
        if shutdown == "asyncio-run":
            # Returning triggers cancellation of every task, including the opening operation.
            timer = threading.Timer(0.1, release.set)
            timer.start()
            return
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        stop_executor = asyncio.create_task(loop.shutdown_default_executor())
        await asyncio.sleep(0)
        release.set()
        await stop_executor

    if shutdown == "closed-loop":
        loop = asyncio.new_event_loop()
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
        loop.create_task(runtime.captions_download(job_id, path.name))
        try:
            loop.run_until_complete(await_open())
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        finally:
            loop.close()
            release.set()
    else:
        asyncio.run(exercise())
    wait_until(lambda: bool(handles))
    wait_until(lambda: handles[0].closes == 1)
    assert handles[0].stream.closed
    assert runtime.caption_manager._downloads == {}  # pylint: disable=protected-access
    assert not loop_errors
    now[0] = 1001
    runtime.caption_manager.sweep()
    assert not path.exists()


def test_caption_acquisition_handoff_and_abandon_race_has_exactly_one_owner(
    download_runtime, monkeypatch,
):
    # pylint: disable=too-many-locals
    runtime, job_id, path = download_runtime
    module = importlib.import_module("caption_jobs")
    monkeypatch.setattr(time, "time", lambda: 1000.9)
    original_open = Path.open
    handles = []

    class ObservedFile:
        def __init__(self, stream):
            self.stream = stream
            self.closes = 0

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            return self.stream.read(size)

        def close(self):
            self.closes += 1
            self.stream.close()

    def observed_open(candidate, *args, **kwargs):
        stream = original_open(candidate, *args, **kwargs)  # pylint: disable=consider-using-with
        if candidate != path:
            return stream
        handle = ObservedFile(stream)
        handles.append(handle)
        return handle

    monkeypatch.setattr(Path, "open", observed_open)
    for _ in range(50):
        acquisition = module.CaptionDownloadAcquisition(runtime.caption_manager, job_id, path.name)
        worker = threading.Thread(target=acquisition.run)
        worker.start()
        assert acquisition.ready.wait(1)
        barrier = threading.Barrier(3)
        accepted = []

        def accept(barrier=barrier, accepted=accepted, acquisition=acquisition):
            # pylint: disable=dangerous-default-value
            barrier.wait()
            try:
                accepted.append(acquisition.accept())
            except RuntimeError:
                pass  # Abandonment won the same ownership lock.

        def abandon(barrier=barrier, acquisition=acquisition):
            barrier.wait()
            acquisition.abandon()

        contenders = [threading.Thread(target=accept), threading.Thread(target=abandon)]
        for contender in contenders:
            contender.start()
        barrier.wait()
        for contender in contenders:
            contender.join(1)
            assert not contender.is_alive()
        worker.join(1)
        assert not worker.is_alive()
        if accepted:
            assert len(accepted) == 1
            assert not handles[-1].stream.closed
            accepted[0].close()
        assert handles[-1].closes == 1
        assert handles[-1].stream.closed
        assert runtime.caption_manager._downloads == {}  # pylint: disable=protected-access
    assert len(handles) == 50


def test_caption_idle_stream_cancel_closes_with_executor_unavailable_without_new_thread(
    download_runtime, monkeypatch,
):
    runtime, job_id, path = download_runtime
    now = [1000.9]
    monkeypatch.setattr(time, "time", lambda: now[0])

    async def exercise():
        response = await runtime.captions_download(job_id, path.name)
        headers_sent = asyncio.Event()

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(_message):
            headers_sent.set()
            await asyncio.Event().wait()

        request = asyncio.create_task(response(
            {"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send,
        ))
        await headers_sent.wait()
        await asyncio.get_running_loop().shutdown_default_executor()

        def cannot_start_thread(_thread):
            raise AssertionError("Finalization must not create a thread")

        with monkeypatch.context() as context:
            context.setattr(threading.Thread, "start", cannot_start_thread)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
        assert response.lease.stream.closed
        assert runtime.caption_manager._downloads == {}  # pylint: disable=protected-access

    asyncio.run(exercise())
    now[0] = 1001
    runtime.caption_manager.sweep()
    assert not path.exists()


def test_caption_lease_read_and_close_races_finalize_once(download_runtime, monkeypatch):
    # pylint: disable=too-many-locals
    runtime, job_id, path = download_runtime
    monkeypatch.setattr(time, "time", lambda: 1000.9)
    original_open = Path.open
    handles = []

    class ObservedFile:
        def __init__(self, stream):
            self.stream = stream
            self.closes = 0

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            time.sleep(0.001)
            return self.stream.read(size)

        def close(self):
            self.closes += 1
            self.stream.close()

    def observed_open(candidate, *args, **kwargs):
        stream = original_open(candidate, *args, **kwargs)  # pylint: disable=consider-using-with
        if candidate != path:
            return stream
        handle = ObservedFile(stream)
        handles.append(handle)
        return handle

    monkeypatch.setattr(Path, "open", observed_open)
    for _ in range(50):
        lease = runtime.caption_manager.open_download(job_id, path.name)
        barrier = threading.Barrier(3)
        results = queue.Queue()

        def read(lease=lease, barrier=barrier, results=results):
            barrier.wait()
            try:
                results.put(lease.read())
            except BaseException as error:  # pylint: disable=broad-exception-caught
                results.put(error)

        def close(lease=lease, barrier=barrier):
            barrier.wait()
            lease.close()

        contenders = [threading.Thread(target=read), threading.Thread(target=close)]
        for contender in contenders:
            contender.start()
        barrier.wait()
        for contender in contenders:
            contender.join(1)
            assert not contender.is_alive()
        assert isinstance(results.get_nowait(), bytes)
        lease.close()
        assert handles[-1].closes == 1
        assert handles[-1].stream.closed
        assert runtime.caption_manager._downloads == {}  # pylint: disable=protected-access
    assert len(handles) == 50


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


def test_audio_capture_read_failure_is_terminal_without_output(tmp_path, capsys):
    module = importlib.import_module("translator_runtime")
    runtime = module.TranslatorRuntime(module.RuntimeConfig.from_environment({}))

    class FailedStream:  # pylint: disable=too-few-public-methods
        reads = 0

        def read(self, _frames):
            self.reads += 1
            if self.reads > 1:
                runtime._stopping.set()  # pylint: disable=protected-access
            raise OSError("input disconnected")

    runtime.audio_stream = FailedStream()
    audio_file = tmp_path / "capture.wav"
    runtime.audio_recorder = module.RotatingWavRecorder(
        tmp_path, "capture", storage_budget_bytes=1024,
    )
    runtime.audio_recorder.open()
    runtime.audio_capture_loop()
    runtime.audio_recorder.close()

    assert runtime.audio_stream.reads == 1
    assert not runtime._stopping.is_set()  # pylint: disable=protected-access
    assert runtime.audio_chunk_queue.empty()
    assert capsys.readouterr().out.count("Audio capture error: input disconnected") == 1
    with wave.open(str(audio_file), "rb") as recording:
        assert recording.getnframes() == 0


def test_audio_capture_recording_failure_reports_once_and_keeps_queueing(tmp_path, capsys):
    module = importlib.import_module("translator_runtime")
    runtime = module.TranslatorRuntime(module.RuntimeConfig.from_environment({}))

    class Stream:  # pylint: disable=too-few-public-methods
        reads = 0

        def read(self, frames):
            self.reads += 1
            if self.reads > 2:
                raise OSError("input disconnected")
            return np.zeros((frames, 1), dtype="float32"), False

    runtime.audio_stream = Stream()
    runtime.audio_recorder = module.RotatingWavRecorder(
        tmp_path, "2026-09-09_120000", storage_budget_bytes=1,
    )
    runtime.audio_capture_loop()

    assert runtime.audio_stream.reads == 3
    assert not runtime._stopping.is_set()  # pylint: disable=protected-access
    assert runtime.audio_chunk_queue.qsize() == 2
    status = runtime.text_queue.get_nowait()
    assert status == {
        "type": "status",
        "status": "recording-error",
        "message": "Recording stopped; live transcription continues.",
    }
    assert runtime.text_queue.empty()
    assert runtime.audio_recorder.enabled is False
    output = capsys.readouterr().out
    assert output.count("Recording disabled: Recording storage budget exhausted") == 1
    assert output.count("Audio capture error: input disconnected") == 1


def test_audio_capture_wave_write_failure_keeps_the_chunk_for_transcription(
    tmp_path, monkeypatch,
):
    module = importlib.import_module("translator_runtime")
    runtime = module.TranslatorRuntime(module.RuntimeConfig.from_environment({}))

    class Stream:  # pylint: disable=too-few-public-methods
        reads = 0

        def read(self, frames):
            self.reads += 1
            if self.reads > 1:
                raise OSError("input disconnected")
            return np.full((frames, 1), 0.25, dtype="float32"), False

    runtime.audio_stream = Stream()
    runtime.audio_recorder = module.RotatingWavRecorder(
        tmp_path, "2026-09-09_120000", storage_budget_bytes=1_000_000,
    )
    runtime.audio_recorder.open()
    writer = runtime.audio_recorder._writer  # pylint: disable=protected-access

    def fail_write(_pcm):
        raise OSError("disk write failed")

    monkeypatch.setattr(writer, "writeframes", fail_write)

    runtime.audio_capture_loop()

    captured = runtime.audio_chunk_queue.get_nowait()
    assert np.all(captured == 0.25)
    assert runtime.audio_chunk_queue.empty()
    assert runtime.text_queue.get_nowait()["status"] == "recording-error"
    assert runtime.audio_recorder.enabled is False


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
    runtime.audio_recorder = module.RotatingWavRecorder(
        tmp_path, "capture", storage_budget_bytes=10_000_000,
    )
    runtime.audio_recorder.open()
    runtime.audio_capture_loop()
    runtime.audio_recorder.close()
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


def test_audio_silent_tail_never_reaches_inference_after_combined_boundary(tmp_path):
    importlib.import_module("scipy.signal")
    module = importlib.import_module("translator_runtime")
    received = []

    class Backend:  # pylint: disable=too-few-public-methods
        name = "fixture"

        def transcribe(self, audio, **_kwargs):
            received.append(audio.copy())
            return [SimpleNamespace(text="speech")], "es"

    runtime = module.TranslatorRuntime(module.RuntimeConfig.from_environment({}))
    runtime.backend = Backend()
    runtime.transcript_file = tmp_path / "session.jsonl"
    first = np.full(240000, 0.1, dtype="float32")
    first[218400:220800] = 0.001
    chunks = [first[start:start + 12000] for start in range(0, len(first), 12000)]
    chunks += [np.full(12000, 0.1, dtype="float32") for _ in range(17)]
    chunks += [np.zeros(12000, dtype="float32") for _ in range(20)]
    start_audio_workers(runtime)
    try:
        for chunk in chunks:
            runtime.audio_chunk_queue.put(chunk)
            wait_until(lambda: runtime.audio_chunk_queue.unfinished_tasks == 0)
            wait_until(lambda: runtime.utterance_queue.unfinished_tasks == 0)
        assert len(received) == 2
        assert [len(audio) for audio in received] == [73600, 80000]
        assert all(np.any(audio) for audio in received)
        assert runtime.audio_chunker.buffered_samples <= 24000
    finally:
        asyncio.run(runtime.stop())


def test_audio_mixed_carried_tail_keeps_its_existing_silence_count():
    chunker = importlib.import_module("audio_pipeline").UtteranceChunker()
    audio = np.full(240000, 0.1, dtype="float32")
    audio[192000:194400] = 0
    audio[228000:] = 0
    first = None
    for start in range(0, len(audio), 12000):
        first = chunker.add(audio[start:start + 12000])
    assert len(first) == 194400  # 4.05s cut; tail includes speech and one silent read.
    second = chunker.add(np.zeros(12000, dtype="float32"))
    assert second is not None  # The next silent read completes the natural pause.
    assert len(second) == 57600
    np.testing.assert_array_equal(np.concatenate([first, second]), np.pad(audio, (0, 12000)))
    assert chunker.buffered_samples == 0


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


@pytest.mark.parametrize("environment, expected", [
    ({}, 256 * 1024 * 1024),
    ({"TRANSLATOR_MAX_DECODED_AUDIO_BYTES": "65536"}, 65536),
])
def test_decoded_audio_configuration_has_a_positive_pcm_byte_limit(environment, expected):
    module = importlib.import_module("translator_runtime")

    config = module.RuntimeConfig.from_environment(environment)

    assert config.max_decoded_audio_bytes == expected


@pytest.mark.parametrize("value", ["", " ", "invalid", "1.5", "0", "-1"])
def test_decoded_audio_configuration_rejects_invalid_limits(value):
    module = importlib.import_module("translator_runtime")

    with pytest.raises(ValueError, match="TRANSLATOR_MAX_DECODED_AUDIO_BYTES"):
        module.RuntimeConfig.from_environment({"TRANSLATOR_MAX_DECODED_AUDIO_BYTES": value})


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


def test_caption_admission_precedes_concurrent_multipart_parsing(  # pylint: disable=too-many-statements
    tmp_path, monkeypatch,
):
    """A full request queue rejects the next body before Starlette reads it."""
    module = importlib.import_module("translator_runtime")
    application = importlib.import_module("app").create_app()

    class AdmissionRuntime(module.TranslatorRuntime):  # pylint: disable=too-few-public-methods
        def caption_worker(self, job_id, video_path):
            video_path.unlink(missing_ok=True)
            self.caption_jobs[job_id].update(status="error", message="Fixture stopped")
            self.caption_manager.complete(job_id)

    runtime = AdmissionRuntime(module.RuntimeConfig.from_environment({
        "TRANSLATOR_MAX_UPLOAD_BYTES": "1048579",
        "TRANSLATOR_CAPTION_CONCURRENCY": "1",
        "TRANSLATOR_CAPTION_QUEUE_CAPACITY": "1",
    }))
    runtime.captions_dir = tmp_path / "captions"
    application.state.runtime = runtime
    entered = asyncio.Queue()
    hold = asyncio.Event()
    consumed = {"first": 0, "second": 0, "excess": 0}
    canceled = []
    cancel_upload = runtime.caption_manager.cancel_upload

    def observe_cancel(job_id):
        canceled.append(job_id)
        cancel_upload(job_id)

    monkeypatch.setattr(runtime.caption_manager, "cancel_upload", observe_cancel)

    async def held_multipart(label):
        header = (b'--boundary\r\nContent-Disposition: form-data; name="file"; '
                  b'filename="video.mp4"\r\nContent-Type: video/mp4\r\n\r\n')
        consumed[label] += len(header)
        await entered.put(label)
        yield header
        await hold.wait()
        ending = b"video\r\n--boundary--\r\n"
        consumed[label] += len(ending)
        yield ending

    async def excess_multipart():
        body = (b'--boundary\r\nContent-Disposition: form-data; name="file"; '
                b'filename="excess.mp4"\r\nContent-Type: video/mp4\r\n\r\n'
                b"excess\r\n--boundary--\r\n")
        consumed["excess"] += len(body)
        yield body

    async def exercise():
        headers = {"content-type": "multipart/form-data; boundary=boundary"}
        tasks = []
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application), base_url="http://test",
            ) as client:
                tasks = [
                    asyncio.create_task(client.post(
                        "/captions/upload", headers=headers, content=held_multipart(label),
                    ))
                    for label in ("first", "second")
                ]
                assert {await asyncio.wait_for(entered.get(), 2) for _ in tasks} == {
                    "first", "second",
                }
                response = await asyncio.wait_for(client.post(
                    "/captions/upload", headers=headers, content=excess_multipart(),
                ), 2)
                assert response.status_code == 429
                assert response.json() == {
                    "error": "Caption capacity is full; retry after current jobs finish",
                }
                assert response.headers["retry-after"] == "5"
                assert consumed["excess"] == 0
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            hold.set()

        assert len(canceled) == 2
        assert len(set(canceled)) == 2
        assert runtime.caption_manager.reserve("0123456789ab")
        assert runtime.caption_manager.reserve("abcdefabcdef")
        assert not runtime.caption_manager.reserve("111111111111")
        runtime.caption_manager.cancel_upload("0123456789ab")
        runtime.caption_manager.cancel_upload("abcdefabcdef")
        await runtime.stop()

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
            if thread.name.startswith("caption-worker-"):
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
            self.caption_jobs["job"] = {"status": "done", "completed_at": time.time(),
                                        "files": ["video.original.srt"]}

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


def test_websocket_client_connecting_after_recording_failure_receives_status():
    module = importlib.import_module("translator_runtime")

    async def exercise():
        runtime = module.TranslatorRuntime(module.RuntimeConfig.from_environment({}))
        runtime._publish_recording_error(OSError("disk unavailable"))  # pylint: disable=protected-access
        broadcaster = asyncio.create_task(runtime.broadcast_loop())
        await asyncio.wait_for(asyncio.to_thread(runtime.text_queue.join), 1)
        broadcaster.cancel()
        with suppress(asyncio.CancelledError):
            await broadcaster
        messages = []
        delivered = asyncio.Event()
        connection_received = False

        async def receive():
            nonlocal connection_received
            if not connection_received:
                connection_received = True
                return {"type": "websocket.connect"}
            await delivered.wait()
            return {"type": "websocket.disconnect", "code": 1000}

        async def send(message):
            messages.append(message)
            if message["type"] == "websocket.send":
                delivered.set()

        websocket = module.WebSocket({"type": "websocket"}, receive, send)
        await asyncio.wait_for(runtime.websocket_endpoint(websocket), 1)
        assert json.loads(messages[1]["text"]) == {
            "type": "status", "status": "recording-error",
            "message": "Recording stopped; live transcription continues.",
        }
        await runtime.stop()

    asyncio.run(exercise())


def test_recording_status_is_not_duplicated_when_client_connects_before_broadcast():
    module = importlib.import_module("translator_runtime")

    async def exercise():
        runtime = module.TranslatorRuntime(module.RuntimeConfig.from_environment({}))
        runtime._publish_recording_error(OSError("disk unavailable"))  # pylint: disable=protected-access
        delivered = []
        accepted = asyncio.Event()
        release = asyncio.Event()
        connection_received = False

        async def receive():
            nonlocal connection_received
            if not connection_received:
                connection_received = True
                return {"type": "websocket.connect"}
            await release.wait()
            return {"type": "websocket.disconnect", "code": 1000}

        async def send(message):
            if message["type"] == "websocket.accept":
                accepted.set()
            elif message["type"] == "websocket.send":
                delivered.append(json.loads(message["text"]))

        websocket = module.WebSocket({"type": "websocket"}, receive, send)
        endpoint = asyncio.create_task(runtime.websocket_endpoint(websocket))
        await accepted.wait()
        broadcaster = asyncio.create_task(runtime.broadcast_loop())
        await asyncio.wait_for(asyncio.to_thread(runtime.text_queue.join), 1)
        await asyncio.sleep(0.05)
        assert delivered == [{
            "type": "status", "status": "recording-error",
            "message": "Recording stopped; live transcription continues.",
        }]
        release.set()
        await endpoint
        broadcaster.cancel()
        with suppress(asyncio.CancelledError):
            await broadcaster
        await runtime.stop()

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


def test_caption_worker_rejects_unsupported_language_before_argos_or_output(tmp_path):
    module = importlib.import_module("translator_runtime")
    reached = []

    class CaptionRuntime(module.TranslatorRuntime):
        def extract_audio_16k(self, _video_path, _audio_path):
            return None

        def load_audio_16k(self, _audio_path):
            return np.zeros(4000, dtype=np.float32)

        def transcribe_segments(self, _audio_array, job):
            job.update(progress=45, message="Transcribed 1 segments...")
            return [{"start": 0, "end": 0.25, "text": "bonjour"}], "fr"

        def ensure_argos_packages(self, _job):
            reached.append("Argos")

        def write_caption_files(self, *_args):
            reached.append("subtitle output")
            return ["video.translated.srt"]

    runtime = CaptionRuntime(module.RuntimeConfig.from_environment({}))
    runtime.captions_dir = tmp_path
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    video_path = job_dir / "video.upload"
    video_path.write_bytes(b"video")
    runtime.caption_jobs["job"] = {"status": "queued", "files": []}

    runtime.caption_worker("job", video_path)

    job = runtime.caption_jobs["job"]
    assert job["status"] == "error"
    assert "fr" in job["message"]
    assert "en, es" in job["message"]
    assert job["files"] == []
    assert "detected_language" not in job
    assert not list(job_dir.glob("*.srt"))
    assert not reached


@pytest.mark.parametrize("device_setting", [None, "default", "MacBook Pro Microphone"])
@pytest.mark.parametrize("broadcast_failure", [False, True])
def test_runtime_shutdown_closes_owned_resources(
    tmp_path, monkeypatch, capsys, broadcast_failure, device_setting,
):
    """Exercise real queues, WAV persistence, worker loops, and task cancellation."""
    monkeypatch.chdir(tmp_path)
    module = importlib.import_module("translator_runtime")
    stream_closed = threading.Event()
    reading = threading.Event()

    class InputStream:
        def __init__(self, **kwargs):
            assert kwargs == {
                "samplerate": 48000, "channels": 1, "dtype": "float32", "device": 1,
            }
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
        query_devices=lambda: [
            {"name": "Speakers", "max_input_channels": 0},
            {"name": "MacBook Pro Microphone", "max_input_channels": 1},
        ],
        default=SimpleNamespace(device=(1, 0)),
        InputStream=InputStream,
    ))
    monkeypatch.setitem(sys.modules, "transcription", SimpleNamespace(
        get_backend=lambda *_args: Backend(),
    ))
    monkeypatch.setattr(module, "load_plugins", lambda: [])
    require_marker_before_recorder_open(module, monkeypatch)

    async def exercise():
        environment = {} if device_setting is None else {"TRANSLATOR_DEVICE": device_setting}
        runtime = module.TranslatorRuntime(module.RuntimeConfig.from_environment(environment))
        other = module.TranslatorRuntime(module.RuntimeConfig.from_environment({}))
        await runtime.start()
        assert "MacBook Pro Microphone (index 1)" in capsys.readouterr().out
        assert await asyncio.to_thread(reading.wait, 2)
        assert runtime.text_queue is not other.text_queue
        assert runtime.audio_chunk_queue is not other.audio_chunk_queue
        assert runtime.client_deliveries is not other.client_deliveries
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
        assert (runtime.clients, runtime.backend) == ([], None)
        assert runtime.transcript_file.read_bytes() == b""
        with wave.open(str(runtime.audio_file), "rb") as audio:
            assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (
                1, 2, 48000,
            )
        assert "Audio capture error:" not in capsys.readouterr().out
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
def test_cleanup_continues_after_a_resource_failure(  # pylint: disable=too-many-statements
    tmp_path, monkeypatch, failure,
):
    module = importlib.import_module("translator_runtime")
    first_error = OSError(
        "abort failed" if failure == "abort_and_wav_close" else "WAV close failed",
    )

    async def exercise():
        runtime = module.TranslatorRuntime(module.RuntimeConfig.from_environment({}))
        runtime.backend = object()
        runtime.captions_dir = tmp_path / "captions"
        runtime.audio_file = tmp_path / "session.wav"
        runtime.audio_recorder = module.RotatingWavRecorder(
            tmp_path, "session", storage_budget_bytes=1024,
        )
        runtime.audio_recorder.open()
        stream = FailingAudioStream(first_error if failure == "abort_and_wav_close" else None)
        runtime.audio_stream = stream
        runtime.broadcast_task = asyncio.create_task(runtime.broadcast_loop())
        delivered = []

        async def receive():
            return {"type": "websocket.connect"}

        async def send(message):
            delivered.append(message)

        client = module.WebSocket({"type": "websocket"}, receive, send)
        await client.accept()
        runtime.client_deliveries.register(client)
        if failure == "caption_thread_start":
            original_start = threading.Thread.start

            def fail_caption_start(thread):
                if thread.name.startswith("caption-worker-"):
                    raise RuntimeError("caption thread could not start")
                original_start(thread)

            monkeypatch.setattr(threading.Thread, "start", fail_caption_start)
            upload = module.UploadFile(BytesIO(b"video"), filename="video.mp4")
            with pytest.raises(RuntimeError, match="caption thread could not start"):
                await runtime.captions_upload(upload)
            await runtime.stop()
            assert not runtime.worker_threads
        else:
            writer = runtime.audio_recorder._writer  # pylint: disable=protected-access
            original_close = writer.close

            def fail_wav_close():
                original_close()
                if failure == "wav_close":
                    raise first_error
                raise OSError("later WAV close failure")

            monkeypatch.setattr(writer, "close", fail_wav_close)
            with pytest.raises(OSError) as caught:
                await runtime.stop()
            assert caught.value is first_error

        assert stream.aborted and stream.closed
        assert runtime.audio_stream is None
        assert runtime.audio_recorder is None
        assert runtime.backend is None
        assert runtime.broadcast_task.done()
        assert not runtime.clients
        assert client.application_state is WebSocketState.DISCONNECTED
        with wave.open(str(runtime.audio_file), "rb") as audio:
            assert audio.getframerate() == 48000
        if failure != "caption_thread_start":
            status_messages = [
                json.loads(message["text"]) for message in delivered
                if message["type"] == "websocket.send"
            ]
            assert status_messages == [{
                "type": "status", "status": "recording-error",
                "message": "Recording stopped; live transcription continues.",
            }]

    asyncio.run(exercise())
