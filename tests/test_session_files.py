"""Atomic reservation and presentation of live-session files."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import datetime
import errno
import importlib
import os
import threading
import wave
from zoneinfo import ZoneInfo

import pytest


def session_files_module():
    """Resolve the production API inside a test so missing behavior is a failure."""
    runtime = importlib.import_module("translator_runtime")
    return runtime.LiveSessionFiles


def test_same_instant_reservations_get_distinct_paired_paths(tmp_path):
    reservation = session_files_module()
    instant = datetime(2026, 9, 9, 19, 20, 21, 123456, tzinfo=ZoneInfo("UTC"))
    tokens = iter(("0123456789abcdef", "fedcba9876543210"))

    first = reservation.reserve(tmp_path, lambda: instant, lambda: next(tokens))
    second = reservation.reserve(tmp_path, lambda: instant, lambda: next(tokens))

    assert first.stem == (
        "2026-09-09_192021_123456Z--2026-09-09_192021_123456+0000-UTC"
        "--0123456789abcdef"
    )
    assert second.stem == (
        "2026-09-09_192021_123456Z--2026-09-09_192021_123456+0000-UTC"
        "--fedcba9876543210"
    )
    try:
        for files in (first, second):
            assert files.transcript_path == tmp_path / f"{files.stem}.jsonl"
            assert files.audio_path == tmp_path / f"{files.stem}.wav"
            assert files.transcript_path.read_bytes() == b""
            assert files.audio_path.read_bytes() == b""
    finally:
        first.close()
        second.close()


def test_concurrent_reservations_atomically_contend_for_one_candidate(tmp_path):
    reservation = session_files_module()
    contenders = 8
    barrier = threading.Barrier(contenders)
    local = threading.local()
    instant = datetime(2026, 9, 9, 19, 20, 21, tzinfo=ZoneInfo("UTC"))

    def token():
        if not hasattr(local, "retried"):
            local.retried = False
            barrier.wait(timeout=2)
            return "0000000000000000"
        local.retried = True
        return f"{threading.get_ident():016x}"[-16:]

    with ThreadPoolExecutor(max_workers=contenders) as workers:
        sessions = list(workers.map(
            lambda _index: reservation.reserve(tmp_path, lambda: instant, token),
            range(contenders),
        ))

    assert len({session.stem for session in sessions}) == contenders
    assert len(list(tmp_path.glob("*.jsonl"))) == contenders
    assert all(session.transcript_path.is_file() for session in sessions)
    assert len(list(tmp_path.glob("*.wav"))) == contenders
    for session in sessions:
        session.close()


def test_utc_names_keep_dst_fold_sessions_in_chronological_history_order(tmp_path):
    reservation = session_files_module()
    history = importlib.import_module("translator_runtime").TranscriptHistoryReader
    pacific = ZoneInfo("America/Los_Angeles")
    first_fold = datetime(2026, 11, 1, 1, 30, fold=0, tzinfo=pacific)
    second_fold = datetime(2026, 11, 1, 1, 30, fold=1, tzinfo=pacific)

    first = reservation.reserve(
        tmp_path, lambda: first_fold, lambda: "1111111111111111",
    )
    second = reservation.reserve(
        tmp_path, lambda: second_fold, lambda: "2222222222222222",
    )

    assert first.stem.startswith(
        "2026-11-01_083000_000000Z--2026-11-01_013000_000000-0700-PDT--"
    )
    assert second.stem.startswith(
        "2026-11-01_093000_000000Z--2026-11-01_013000_000000-0800-PST--"
    )
    try:
        listing = history(tmp_path, session_limit=2, entry_limit=2).list_sessions()
        assert [item["filename"] for item in listing["transcripts"]] == [
            second.transcript_path.name, first.transcript_path.name,
        ]
        assert [item["label"] for item in listing["transcripts"]] == [
            "November 01, 2026 at 01:30 AM PST (UTC-08:00)",
            "November 01, 2026 at 01:30 AM PDT (UTC-07:00)",
        ]
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("recording_name", ["{stem}.wav", "{stem}.part002.wav"])
def test_reservation_skips_an_orphan_recording_without_changing_it(
    tmp_path, recording_name,
):
    reservation = session_files_module()
    instant = datetime(2026, 9, 9, 19, 20, 21, tzinfo=ZoneInfo("UTC"))
    first_stem = (
        "2026-09-09_192021_000000Z--2026-09-09_192021_000000+0000-UTC"
        "--0000000000000000"
    )
    orphan = tmp_path / recording_name.format(stem=first_stem)
    orphan.write_bytes(b"existing recording")
    tokens = iter(("0000000000000000", "1111111111111111"))

    files = reservation.reserve(tmp_path, lambda: instant, lambda: next(tokens))

    assert files.stem == (
        "2026-09-09_192021_000000Z--2026-09-09_192021_000000+0000-UTC"
        "--1111111111111111"
    )
    assert orphan.read_bytes() == b"existing recording"
    assert not (tmp_path / f"{first_stem}.jsonl").exists()
    files.close()


def test_reservation_skips_an_orphan_recording_symlink(tmp_path):
    reservation = session_files_module()
    instant = datetime(2026, 9, 9, 19, 20, 21, tzinfo=ZoneInfo("UTC"))
    first_stem = (
        "2026-09-09_192021_000000Z--2026-09-09_192021_000000+0000-UTC"
        "--0000000000000000"
    )
    outside = tmp_path.parent / "outside-recording.wav"
    outside.write_bytes(b"outside recording")
    (tmp_path / f"{first_stem}.wav").symlink_to(outside)
    tokens = iter(("0000000000000000", "1111111111111111"))

    files = reservation.reserve(tmp_path, lambda: instant, lambda: next(tokens))

    assert files.stem.endswith("-1111111111111111")
    assert outside.read_bytes() == b"outside recording"
    assert (tmp_path / f"{first_stem}.wav").is_symlink()
    files.close()


def test_reservation_rejects_a_symlinked_transcript_directory(tmp_path):
    reservation = session_files_module()
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)

    with pytest.raises(OSError, match="transcript directory"):
        reservation.reserve(linked)

    assert not list(actual.iterdir())


def test_reserved_audio_descriptor_is_handed_directly_to_the_recorder(tmp_path):
    reservation = session_files_module()
    files = reservation.reserve(tmp_path)
    recorder = files.create_audio_recorder(storage_budget_bytes=1024)

    recorder.open()
    recorder.write(b"\x01\x00")
    recorder.close()
    files.close()

    with wave.open(str(files.audio_path), "rb") as recording:
        assert recording.readframes(recording.getnframes()) == b"\x01\x00"


def test_collision_cleanup_never_deletes_a_replaced_marker(tmp_path, monkeypatch):
    module = importlib.import_module("session_files")
    reservation = session_files_module()
    real_open = module.os.open
    instant = datetime(2026, 9, 9, 19, 20, 21, tzinfo=ZoneInfo("UTC"))
    stem = (
        "2026-09-09_192021_000000Z--2026-09-09_192021_000000+0000-UTC"
        "--0000000000000000"
    )
    marker = tmp_path / f"{stem}.jsonl"

    def replace_marker_before_audio_claim(path, flags, mode=0o777, *, dir_fd=None):
        if path == f"{stem}.wav":
            marker.unlink()
            marker.write_bytes(b"replacement marker")
            raise FileExistsError(path)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(module.os, "open", replace_marker_before_audio_claim)

    with pytest.raises(module.SessionReservationError, match="release"):
        reservation.reserve(
            tmp_path, lambda: instant, lambda: "0000000000000000",
        )

    assert marker.read_bytes() == b"replacement marker"


def test_collision_cleanup_failure_leaves_the_claimed_marker_safe(tmp_path, monkeypatch):
    module = importlib.import_module("session_files")
    reservation = session_files_module()
    real_open = module.os.open
    real_unlink = module.os.unlink
    instant = datetime(2026, 9, 9, 19, 20, 21, tzinfo=ZoneInfo("UTC"))
    stem = (
        "2026-09-09_192021_000000Z--2026-09-09_192021_000000+0000-UTC"
        "--0000000000000000"
    )
    marker_name = f"{stem}.jsonl"

    def collide_on_audio(path, flags, mode=0o777, *, dir_fd=None):
        if path == f"{stem}.wav":
            raise FileExistsError(path)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    def reject_marker_cleanup(path, *, dir_fd=None):
        if path == marker_name:
            raise PermissionError("cleanup denied")
        return real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(module.os, "open", collide_on_audio)
    monkeypatch.setattr(module.os, "unlink", reject_marker_cleanup)

    with pytest.raises(module.SessionReservationError, match="release"):
        reservation.reserve(
            tmp_path, lambda: instant, lambda: "0000000000000000",
        )

    assert (tmp_path / marker_name).read_bytes() == b""


def test_audio_claim_error_closes_and_removes_only_the_owned_marker(tmp_path, monkeypatch):
    module = importlib.import_module("session_files")
    reservation = session_files_module()
    real_open = module.os.open
    instant = datetime(2026, 9, 9, 19, 20, 21, tzinfo=ZoneInfo("UTC"))
    stem = (
        "2026-09-09_192021_000000Z--2026-09-09_192021_000000+0000-UTC"
        "--0000000000000000"
    )
    marker = tmp_path / f"{stem}.jsonl"
    marker_descriptors = []

    def fail_audio_claim(path, flags, mode=0o777, *, dir_fd=None):
        if path == f"{stem}.wav":
            raise OSError(errno.ENOSPC, "disk full")
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if path == marker.name:
            marker_descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr(module.os, "open", fail_audio_claim)
    try:
        with pytest.raises(OSError) as caught:
            reservation.reserve(
                tmp_path, lambda: instant, lambda: "0000000000000000",
            )

        assert caught.value.errno == errno.ENOSPC
        assert not marker.exists()
        assert not (tmp_path / f"{stem}.wav").exists()
        assert len(marker_descriptors) == 1
        with pytest.raises(OSError) as closed:
            os.fstat(marker_descriptors[0])
        assert closed.value.errno == errno.EBADF
    finally:
        for descriptor in marker_descriptors:
            with suppress(OSError):
                os.close(descriptor)
