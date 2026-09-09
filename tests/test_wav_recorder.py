"""Rotating WAV recording and retained-storage boundaries."""

import importlib
import os
from pathlib import Path
import wave

import pytest


def read_wav(path):
    """Return format metadata and frame bytes from one finalized WAV fixture."""
    with wave.open(str(path), "rb") as recording:
        metadata = (
            recording.getnchannels(), recording.getsampwidth(), recording.getframerate(),
        )
        return metadata, recording.readframes(recording.getnframes())


def write_wav(path, frames=b"\x00\x00"):
    """Create one valid retained recording part."""
    with wave.Wave_write(str(path)) as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(48000)
        recording.writeframes(frames)


def partial_write_failure(real_open):
    """Build a wave opener whose first frame write and later close both fail."""

    class FailedFirstWrite:
        def __init__(self, filename, mode):
            self.writer = real_open(filename, mode)

        def __getattr__(self, name):
            return getattr(self.writer, name)

        def writeframes(self, pcm):
            self.writer.writeframesraw(pcm[:2])
            raise OSError("partial frame write")

        def close(self):
            self.writer.close()
            raise OSError("close after write failed")

    return FailedFirstWrite


def test_recorder_rotates_a_boundary_chunk_without_loss_or_duplication(tmp_path):
    module = importlib.import_module("wav_recorder")
    recorder = module.RotatingWavRecorder(
        tmp_path, "2026-09-09_120000", storage_budget_bytes=1024,
        part_data_limit=6,
    )
    first_chunk = b"\x01\x00\x02\x00"
    crossing_chunk = b"\x03\x00\x04\x00"

    recorder.write(first_chunk)
    recorder.write(crossing_chunk)
    recorder.close()
    recorder.close()

    parts = sorted(tmp_path.glob("*.wav"))
    assert [part.name for part in parts] == [
        "2026-09-09_120000.part002.wav", "2026-09-09_120000.wav",
    ]
    recordings = {part.name: read_wav(part) for part in parts}
    assert recordings["2026-09-09_120000.wav"] == (
        (1, 2, 48000), first_chunk,
    )
    assert recordings["2026-09-09_120000.part002.wav"] == (
        (1, 2, 48000), crossing_chunk,
    )
    assert b"".join(recordings[name][1] for name in (
        "2026-09-09_120000.wav", "2026-09-09_120000.part002.wav",
    )) == first_chunk + crossing_chunk


def test_recorder_evicts_the_oldest_inactive_audio_group_before_writing(tmp_path):
    module = importlib.import_module("wav_recorder")
    old_transcript = tmp_path / "2026-09-07_120000.jsonl"
    old_transcript.write_text("transcript remains\n", encoding="utf-8")
    old_parts = [
        tmp_path / "2026-09-07_120000.wav",
        tmp_path / "2026-09-07_120000.part002.wav",
    ]
    newer_part = tmp_path / "2026-09-08_120000.wav"
    for path in (*old_parts, newer_part):
        write_wav(path)
    for path in old_parts:
        os.utime(path, (1, 1))
    os.utime(newer_part, (2, 2))
    recorder = module.RotatingWavRecorder(
        tmp_path, "2026-09-09_120000", storage_budget_bytes=92,
    )

    recorder.write(b"\x01\x00")
    recorder.close()

    assert all(not path.exists() for path in old_parts)
    assert newer_part.exists()
    assert old_transcript.read_text(encoding="utf-8") == "transcript remains\n"
    assert (tmp_path / "2026-09-09_120000.wav").stat().st_size == 46
    assert sum(path.stat().st_size for path in tmp_path.glob("*.wav")) == 92


def test_recorder_disables_when_the_active_group_exhausts_storage(tmp_path):
    module = importlib.import_module("wav_recorder")
    recorder = module.RotatingWavRecorder(
        tmp_path, "2026-09-09_120000", storage_budget_bytes=47,
    )
    recorder.write(b"\x01\x00")

    with pytest.raises(module.RecordingStorageError, match="storage budget"):
        recorder.write(b"\x02\x00")

    assert recorder.enabled is False
    recorder.close()
    assert read_wav(tmp_path / "2026-09-09_120000.wav")[1] == b"\x01\x00"


def test_recorder_retention_ignores_audio_symlinks_outside_its_root(tmp_path):
    module = importlib.import_module("wav_recorder")
    root = tmp_path / "transcripts"
    root.mkdir()
    outside = tmp_path / "private.wav"
    outside.write_bytes(b"private audio")
    (root / "2026-09-07_120000.wav").symlink_to(outside)
    inactive = root / "2026-09-08_120000.wav"
    write_wav(inactive)
    inactive.with_suffix(".jsonl").write_text("transcript\n", encoding="utf-8")
    recorder = module.RotatingWavRecorder(
        root, "2026-09-09_120000", storage_budget_bytes=46,
    )

    recorder.write(b"\x01\x00")
    recorder.close()

    assert outside.read_bytes() == b"private audio"
    assert (root / "2026-09-07_120000.wav").is_symlink()
    assert not inactive.exists()
    assert (root / "2026-09-09_120000.wav").stat().st_size == 46


@pytest.mark.parametrize("environment, expected", [
    ({}, 8 * 1024 * 1024 * 1024),
    ({"TRANSLATOR_RECORDING_STORAGE_BYTES": "4096"}, 4096),
])
def test_recording_storage_configuration_is_a_positive_byte_budget(environment, expected):
    module = importlib.import_module("runtime_config")

    assert module.RuntimeConfig.from_environment(environment).recording_storage_bytes == expected


@pytest.mark.parametrize("value", ["", " ", "invalid", "1.5", "0", "-1"])
def test_recording_storage_configuration_rejects_invalid_limits(value):
    module = importlib.import_module("runtime_config")

    with pytest.raises(ValueError, match="TRANSLATOR_RECORDING_STORAGE_BYTES"):
        module.RuntimeConfig.from_environment({"TRANSLATOR_RECORDING_STORAGE_BYTES": value})


def test_recorder_removes_an_incomplete_file_when_open_fails(tmp_path, monkeypatch):
    module = importlib.import_module("wav_recorder")
    part = tmp_path / "2026-09-09_120000.wav"

    def fail_after_creating(stream, _mode):
        part.write_bytes(b"incomplete header")
        assert Path(stream.name) == part
        raise PermissionError("storage unavailable")

    monkeypatch.setattr(module.wave, "open", fail_after_creating)
    recorder = module.RotatingWavRecorder(
        tmp_path, "2026-09-09_120000", storage_budget_bytes=1024,
    )

    with pytest.raises(module.RecordingError, match="open failed"):
        recorder.open()

    assert recorder.enabled is False
    assert not part.exists()
    recorder.close()


def test_recorder_removes_a_partial_file_when_wave_setup_and_close_fail(
    tmp_path, monkeypatch,
):
    module = importlib.import_module("wav_recorder")
    part = tmp_path / "2026-09-09_120000.wav"

    class FailedWave:
        def setnchannels(self, _channels):
            raise OSError("header setup failed")

        def close(self):
            raise OSError("partial close failed")

    def open_partial(stream, _mode):
        assert Path(stream.name) == part
        part.write_bytes(b"partial header")
        return FailedWave()

    monkeypatch.setattr(module.wave, "open", open_partial)
    recorder = module.RotatingWavRecorder(
        tmp_path, "2026-09-09_120000", storage_budget_bytes=1024,
    )

    with pytest.raises(module.RecordingError, match="open failed"):
        recorder.open()

    assert recorder.enabled is False
    assert not part.exists()


def test_recorder_removes_a_new_part_when_its_first_frame_write_is_unconfirmed(
    tmp_path, monkeypatch,
):
    module = importlib.import_module("wav_recorder")
    part = tmp_path / "2026-09-09_120000.wav"
    real_open = module.wave.open

    monkeypatch.setattr(module.wave, "open", partial_write_failure(real_open))
    recorder = module.RotatingWavRecorder(
        tmp_path, "2026-09-09_120000", storage_budget_bytes=1024,
    )

    with pytest.raises(module.RecordingError, match="write failed"):
        recorder.write(b"\x01\x00\x02\x00")

    assert recorder.enabled is False
    assert not part.exists()


def test_recorder_discards_a_failed_new_part_but_keeps_finalized_earlier_parts(
    tmp_path, monkeypatch,
):
    module = importlib.import_module("wav_recorder")
    stem = "2026-09-09_120000"
    first_part = tmp_path / f"{stem}.wav"
    failed_part = tmp_path / f"{stem}.part002.wav"
    recorder = module.RotatingWavRecorder(
        tmp_path, stem, storage_budget_bytes=1024, part_data_limit=4,
    )
    first_chunk = b"\x01\x00\x02\x00"
    recorder.write(first_chunk)
    real_open = module.wave.open

    with monkeypatch.context() as patcher:
        patcher.setattr(module.wave, "open", partial_write_failure(real_open))
        with pytest.raises(module.RecordingError, match="write failed"):
            recorder.write(b"\x03\x00\x04\x00")

    assert recorder.enabled is False
    assert read_wav(first_part)[1] == first_chunk
    assert not failed_part.exists()


def test_recorder_rejects_a_part_limit_that_splits_pcm_frames(tmp_path):
    module = importlib.import_module("wav_recorder")

    with pytest.raises(ValueError, match="whole PCM frames"):
        module.RotatingWavRecorder(
            tmp_path, "2026-09-09_120000", storage_budget_bytes=1024,
            part_data_limit=5,
        )


def test_recorder_rejects_a_partial_pcm_frame_before_opening_a_file(tmp_path):
    module = importlib.import_module("wav_recorder")
    recorder = module.RotatingWavRecorder(
        tmp_path, "2026-09-09_120000", storage_budget_bytes=1024,
    )

    with pytest.raises(module.RecordingError, match="whole PCM frames"):
        recorder.write(b"\x01")

    assert recorder.enabled is False
    assert not list(tmp_path.iterdir())


def test_default_part_limit_reserves_the_final_riff_header():
    module = importlib.import_module("wav_recorder")

    data_bytes = module.RotatingWavRecorder._DEFAULT_PART_DATA_LIMIT  # pylint: disable=protected-access

    assert data_bytes == 4_294_967_258
    assert data_bytes % 2 == 0
    assert data_bytes + 36 <= 0xFFFFFFFF


def test_recorder_rejects_a_part_limit_beyond_the_riff_field(tmp_path):
    module = importlib.import_module("wav_recorder")

    with pytest.raises(ValueError, match="RIFF"):
        module.RotatingWavRecorder(
            tmp_path, "2026-09-09_120000", storage_budget_bytes=10_000_000_000,
            part_data_limit=4_294_967_260,
        )


def test_retention_leaves_unmanaged_and_malformed_wav_names_unchanged(tmp_path):
    module = importlib.import_module("wav_recorder")
    managed_stem = "2026-09-08_120000"
    managed = tmp_path / f"{managed_stem}.wav"
    write_wav(managed)
    (tmp_path / f"{managed_stem}.jsonl").write_text("transcript\n", encoding="utf-8")
    unmanaged = tmp_path / "manually-imported.wav"
    malformed = [
        tmp_path / f"{managed_stem}.part02.wav",
        tmp_path / f"{managed_stem}.part0002.wav",
        tmp_path / f"{managed_stem}.part00002.wav",
    ]
    unmanaged.write_bytes(b"manual audio")
    for path in malformed:
        path.write_bytes(b"malformed part")
    os.utime(unmanaged, (0, 0))
    for path in malformed:
        os.utime(path, (0, 0))
    os.utime(managed, (1, 1))
    recorder = module.RotatingWavRecorder(
        tmp_path, "2026-09-09_120000", storage_budget_bytes=46,
    )

    recorder.write(b"\x01\x00")
    recorder.close()

    assert not managed.exists()
    assert unmanaged.read_bytes() == b"manual audio"
    assert all(path.read_bytes() == b"malformed part" for path in malformed)


def test_retention_accounts_for_part_1000_as_a_canonical_group_member(tmp_path):
    module = importlib.import_module("wav_recorder")
    old_stem = "2026-09-08_120000"
    (tmp_path / f"{old_stem}.jsonl").touch()
    old_parts = [
        tmp_path / f"{old_stem}.wav",
        tmp_path / f"{old_stem}.part1000.wav",
    ]
    for part in old_parts:
        write_wav(part)
    recorder = module.RotatingWavRecorder(
        tmp_path, "2026-09-09_120000", storage_budget_bytes=92,
        part_data_limit=2,
    )

    recorder.write(b"\x01\x00")
    recorder.close()

    assert all(not part.exists() for part in old_parts)
    assert (tmp_path / "2026-09-09_120000.wav").stat().st_size == 46


def test_restart_retention_evicts_audio_owned_by_an_empty_crash_marker(tmp_path):
    module = importlib.import_module("wav_recorder")
    crashed_stem = "2026-09-08_120000"
    marker = tmp_path / f"{crashed_stem}.jsonl"
    crashed_audio = tmp_path / f"{crashed_stem}.wav"
    marker.touch()
    write_wav(crashed_audio)
    recorder = module.RotatingWavRecorder(
        tmp_path, "2026-09-09_120000", storage_budget_bytes=44,
    )

    recorder.open()
    recorder.close()

    assert not crashed_audio.exists()
    assert marker.read_bytes() == b""
    assert (tmp_path / "2026-09-09_120000.wav").stat().st_size == 44


def test_recorder_never_opens_an_active_part_through_a_symlink(tmp_path):
    module = importlib.import_module("wav_recorder")
    root = tmp_path / "transcripts"
    root.mkdir()
    outside = tmp_path / "outside.wav"
    outside.write_bytes(b"private audio")
    active = root / "2026-09-09_120000.wav"
    active.symlink_to(outside)
    recorder = module.RotatingWavRecorder(
        root, "2026-09-09_120000", storage_budget_bytes=1024,
    )

    with pytest.raises(module.RecordingError, match="unsafe recording path"):
        recorder.open()

    assert recorder.enabled is False
    assert active.is_symlink()
    assert outside.read_bytes() == b"private audio"


def test_recorder_never_truncates_an_existing_first_part(tmp_path):
    module = importlib.import_module("wav_recorder")
    existing = tmp_path / "2026-09-09_120000.wav"
    existing.write_bytes(b"existing recording")
    recorder = module.RotatingWavRecorder(
        tmp_path, "2026-09-09_120000", storage_budget_bytes=1024,
    )

    with pytest.raises(module.RecordingError, match="open failed"):
        recorder.open()

    assert recorder.enabled is False
    assert existing.read_bytes() == b"existing recording"


def test_recorder_never_truncates_an_existing_rotated_part(tmp_path):
    module = importlib.import_module("wav_recorder")
    stem = "2026-09-09_120000"
    first = tmp_path / f"{stem}.wav"
    existing = tmp_path / f"{stem}.part002.wav"
    existing.write_bytes(b"existing second part")
    recorder = module.RotatingWavRecorder(
        tmp_path, stem, storage_budget_bytes=1024, part_data_limit=2,
    )
    recorder.write(b"\x01\x00")

    with pytest.raises(module.RecordingError, match="write failed"):
        recorder.write(b"\x02\x00")

    recorder.close()
    assert read_wav(first)[1] == b"\x01\x00"
    assert existing.read_bytes() == b"existing second part"


def test_recorder_rejects_a_replacement_for_its_reserved_first_part(tmp_path):
    session_module = importlib.import_module("session_files")
    recorder_module = importlib.import_module("wav_recorder")
    files = session_module.LiveSessionFiles.reserve(tmp_path)
    files.audio_path.unlink()
    files.audio_path.write_bytes(b"replacement recording")
    recorder = files.create_audio_recorder(storage_budget_bytes=1024)

    with pytest.raises(recorder_module.RecordingError, match="reserved audio"):
        recorder.open()

    recorder.close()
    assert files.audio_path.read_bytes() == b"replacement recording"


def test_reserved_empty_audio_is_removed_when_recording_cannot_start(tmp_path):
    session_module = importlib.import_module("session_files")
    recorder_module = importlib.import_module("wav_recorder")
    files = session_module.LiveSessionFiles.reserve(tmp_path)
    recorder = files.create_audio_recorder(storage_budget_bytes=1)

    with pytest.raises(recorder_module.RecordingStorageError, match="storage budget"):
        recorder.open()

    assert files.transcript_path.is_file()
    assert not files.audio_path.exists()
