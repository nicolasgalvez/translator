"""Bounded transcript history storage and route behavior."""

import asyncio
from io import BytesIO
import importlib
import json
from pathlib import Path
import threading

import httpx
import pytest


def write_session(path, texts):
    """Write valid production-shaped JSONL entries for a history session."""
    path.write_text("".join(
        json.dumps({"time": f"12:00:0{index}", "text": text}) + "\n"
        for index, text in enumerate(texts)
    ), encoding="utf-8")


def test_readme_documents_the_live_transcript_retention_contract():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    live_section = readme.split("### Live Transcriber (`/`)", maxsplit=1)[1].split(
        "### Transcript History (`/history`)", maxsplit=1,
    )[0]

    assert "retains the 500 newest transcript events in arrival order" in live_section
    assert "evicts the oldest event" in live_section
    assert "plugin rendering" in live_section


@pytest.mark.parametrize("setting", ["SESSION_LIMIT", "ENTRY_LIMIT"])
@pytest.mark.parametrize("value", ["0", "-1", "1.5", "bad", ""])
def test_history_configuration_rejects_invalid_limits(setting, value):
    module = importlib.import_module("translator_runtime")

    with pytest.raises(ValueError, match=f"TRANSLATOR_HISTORY_{setting}"):
        module.RuntimeConfig.from_environment({f"TRANSLATOR_HISTORY_{setting}": value})


def test_history_configuration_has_bounded_defaults():
    module = importlib.import_module("translator_runtime")

    config = module.RuntimeConfig.from_environment({})

    assert config.history_session_limit == 50
    assert config.history_entry_limit == 500


def test_history_reader_prefers_newest_sessions_and_discloses_partial_counts(tmp_path):
    module = importlib.import_module("translator_runtime")
    write_session(tmp_path / "2026-09-01_120000.jsonl", ["old"])
    write_session(tmp_path / "2026-09-02_120000.jsonl", ["middle"])
    write_session(tmp_path / "2026-09-03_120000.jsonl", ["one", "two", "three"])

    listing = module.TranscriptHistoryReader(
        tmp_path, session_limit=2, entry_limit=2,
    ).list_sessions()

    assert [session["filename"] for session in listing["transcripts"]] == [
        "2026-09-03_120000.jsonl", "2026-09-02_120000.jsonl",
    ]
    assert listing["sessions_truncated"] is True
    assert listing["session_limit"] == 2
    assert listing["transcripts"][0]["count"] == 2
    assert listing["transcripts"][0]["count_truncated"] is True
    assert listing["transcripts"][1]["count"] == 1
    assert listing["transcripts"][1]["count_truncated"] is False
    assert listing["transcripts"][0]["label"] == "September 03, 2026 at 12:00 PM"


def test_history_listing_skips_malformed_records_and_counts_valid_entries(tmp_path):
    module = importlib.import_module("translator_runtime")
    session = tmp_path / "2026-09-03_120000.jsonl"
    session.write_bytes(
        b'{"time":"12:00:00","text":"first"}\n'
        b'{not valid json}\n'
        b'{"time":"12:00:02","text":"last"}\n'
    )

    listing = module.TranscriptHistoryReader(
        tmp_path, session_limit=2, entry_limit=5,
    ).list_sessions()

    assert listing["transcripts"][0]["count"] == 2
    assert listing["transcripts"][0]["count_truncated"] is False
    assert listing["transcripts"][0]["skipped_records"] == 1


def test_history_listing_skips_nonstandard_json_constants(tmp_path):
    module = importlib.import_module("translator_runtime")
    session = tmp_path / "2026-09-03_120000.jsonl"
    session.write_bytes(b'{"time":"12:00:00","text":NaN}\n')

    listing = module.TranscriptHistoryReader(
        tmp_path, session_limit=2, entry_limit=5,
    ).list_sessions()

    assert listing["transcripts"][0]["count"] == 0
    assert listing["transcripts"][0]["skipped_records"] == 1


def test_history_listing_rejects_every_non_object_json_value(tmp_path):
    module = importlib.import_module("translator_runtime")
    session = tmp_path / "2026-09-03_120000.jsonl"
    session.write_bytes(b'[]\n"text"\n42\ntrue\nnull\n')

    listing = module.TranscriptHistoryReader(
        tmp_path, session_limit=2, entry_limit=5,
    ).list_sessions()

    assert listing["transcripts"][0]["count"] == 0
    assert listing["transcripts"][0]["skipped_records"] == 5


def test_history_listing_skips_records_the_json_decoder_cannot_nest(tmp_path):
    module = importlib.import_module("translator_runtime")
    session = tmp_path / "2026-09-03_120000.jsonl"
    session.write_bytes((b"[" * 30000) + (b"]" * 30000) + b"\n")

    listing = module.TranscriptHistoryReader(
        tmp_path, session_limit=2, entry_limit=5,
    ).list_sessions()

    assert listing["transcripts"][0]["count"] == 0
    assert listing["transcripts"][0]["skipped_records"] == 1


def test_history_listing_skips_invalid_utf8_records(tmp_path):
    module = importlib.import_module("translator_runtime")
    session = tmp_path / "2026-09-03_120000.jsonl"
    session.write_bytes(
        b'{"time":"12:00:00","text":"valid"}\n'
        b'{"time":"12:00:01","text":"\xff"}\n'
    )

    listing = module.TranscriptHistoryReader(
        tmp_path, session_limit=2, entry_limit=5,
    ).list_sessions()

    assert listing["transcripts"][0]["count"] == 1
    assert listing["transcripts"][0]["skipped_records"] == 1


def test_history_reader_rejects_surrogates_in_nested_values_and_object_keys(tmp_path):
    module = importlib.import_module("translator_runtime")
    session = tmp_path / "2026-09-03_120000.jsonl"
    session.write_bytes(
        b'{"time":"12:00:00","text":"first"}\n'
        b'{"\\ud800":"unsafe key"}\n'
        b'{"meta":{"items":["\\udfff"]}}\n'
        b'{"time":"12:00:03","text":"last"}\n'
    )
    reader = module.TranscriptHistoryReader(tmp_path, session_limit=2, entry_limit=5)

    listing = reader.list_sessions()
    detail = reader.read_session(session.name)

    assert listing["transcripts"][0]["count"] == 2
    assert listing["transcripts"][0]["skipped_records"] == 2
    assert [entry["text"] for entry in detail["entries"]] == ["first", "last"]
    assert detail["skipped_records"] == 2


def test_history_listing_treats_a_valid_unterminated_final_record_as_corrupt(tmp_path):
    module = importlib.import_module("translator_runtime")
    session = tmp_path / "2026-09-03_120000.jsonl"
    session.write_bytes(
        b'{"time":"12:00:00","text":"complete"}\n'
        b'{"time":"12:00:01","text":"crash-written"}'
    )

    listing = module.TranscriptHistoryReader(
        tmp_path, session_limit=2, entry_limit=5,
    ).list_sessions()

    assert listing["transcripts"][0]["count"] == 1
    assert listing["transcripts"][0]["skipped_records"] == 1


def test_history_listing_reports_only_corruption_seen_before_the_valid_limit(tmp_path):
    module = importlib.import_module("translator_runtime")
    session = tmp_path / "2026-09-03_120000.jsonl"
    session.write_bytes(
        b'{"time":"12:00:00","text":"first"}\n'
        b'{first damaged record}\n'
        b'{"time":"12:00:01","text":"second"}\n'
        b'{unscanned damaged record}\n'
    )

    listing = module.TranscriptHistoryReader(
        tmp_path, session_limit=2, entry_limit=1,
    ).list_sessions()

    assert listing["transcripts"][0]["count"] == 1
    assert listing["transcripts"][0]["count_truncated"] is True
    assert listing["transcripts"][0]["scan_truncated"] is True
    assert listing["transcripts"][0]["skipped_records"] == 1


def test_history_listing_stops_after_proving_the_valid_count_is_truncated(
    tmp_path, monkeypatch,
):
    module = importlib.import_module("translator_runtime")
    session = tmp_path / "session.jsonl"
    session.touch()
    lines = [
        b'{"time":"12:00:00","text":"first"}\n',
        b'{"time":"12:00:01","text":"second"}\n',
    ]
    reads = 0

    class BoundedListing:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def readline(self, _size=-1):
            nonlocal reads
            reads += 1
            if reads > len(lines):
                raise AssertionError("listing read beyond entry_limit + 1 valid records")
            return lines[reads - 1]

    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: BoundedListing())

    listing = module.TranscriptHistoryReader(
        tmp_path, session_limit=1, entry_limit=1,
    ).list_sessions()

    assert reads == 2
    assert listing["transcripts"][0]["count"] == 1
    assert listing["transcripts"][0]["count_truncated"] is True
    assert listing["transcripts"][0]["scan_truncated"] is True
    assert listing["transcripts"][0]["skipped_records"] == 0


def test_history_listing_bounds_an_endless_stream_of_damaged_records(
    tmp_path, monkeypatch,
):
    module = importlib.import_module("translator_runtime")
    session = tmp_path / "session.jsonl"
    session.touch()
    reads = 0

    class EndlessDamage:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def readline(self, _size=-1):
            nonlocal reads
            reads += 1
            if reads > 3:
                raise AssertionError("listing exceeded its byte budget")
            return b"{bad}\n"

    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: EndlessDamage())
    monkeypatch.setattr(
        module.TranscriptHistoryReader, "_MAX_LIST_BYTES", 18, raising=False,
    )

    listing = module.TranscriptHistoryReader(
        tmp_path, session_limit=1, entry_limit=1,
    ).list_sessions()

    assert reads == 3
    assert listing["transcripts"][0]["count"] == 0
    assert listing["transcripts"][0]["count_truncated"] is False
    assert listing["transcripts"][0]["scan_truncated"] is True
    assert listing["transcripts"][0]["skipped_records"] == 3


def test_history_listing_bounds_one_endless_oversized_record(tmp_path, monkeypatch):
    module = importlib.import_module("translator_runtime")
    session = tmp_path / "session.jsonl"
    session.touch()
    reads = 0

    class EndlessOversizedRecord:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def readline(self, size=-1):
            nonlocal reads
            reads += 1
            if reads > 2:
                raise AssertionError("oversized record drain exceeded the byte budget")
            return b"x" * size

    monkeypatch.setattr(
        Path, "open", lambda *_args, **_kwargs: EndlessOversizedRecord(),
    )
    monkeypatch.setattr(module.TranscriptHistoryReader, "_MAX_ENTRY_BYTES", 4)
    monkeypatch.setattr(
        module.TranscriptHistoryReader, "_MAX_LIST_BYTES", 10, raising=False,
    )

    listing = module.TranscriptHistoryReader(
        tmp_path, session_limit=1, entry_limit=1,
    ).list_sessions()

    assert reads == 2
    assert listing["transcripts"][0]["count"] == 0
    assert listing["transcripts"][0]["count_truncated"] is False
    assert listing["transcripts"][0]["scan_truncated"] is True
    assert listing["transcripts"][0]["skipped_records"] == 1


def test_history_listing_treats_an_exact_budget_unterminated_record_as_eof(
    tmp_path, monkeypatch,
):
    module = importlib.import_module("translator_runtime")
    session = tmp_path / "session.jsonl"
    session.write_bytes(b"{}")
    monkeypatch.setattr(module.TranscriptHistoryReader, "_MAX_LIST_BYTES", 2)

    listing = module.TranscriptHistoryReader(
        tmp_path, session_limit=1, entry_limit=1,
    ).list_sessions()

    assert listing["transcripts"][0]["count"] == 0
    assert listing["transcripts"][0]["count_truncated"] is False
    assert listing["transcripts"][0]["scan_truncated"] is False
    assert listing["transcripts"][0]["skipped_records"] == 1


def test_history_listing_treats_an_exact_budget_terminated_record_as_eof(
    tmp_path, monkeypatch,
):
    module = importlib.import_module("translator_runtime")
    session = tmp_path / "session.jsonl"
    session.write_bytes(b"{}\n")
    monkeypatch.setattr(module.TranscriptHistoryReader, "_MAX_LIST_BYTES", 3)

    listing = module.TranscriptHistoryReader(
        tmp_path, session_limit=1, entry_limit=1,
    ).list_sessions()

    assert listing["transcripts"][0]["count"] == 1
    assert listing["transcripts"][0]["count_truncated"] is False
    assert listing["transcripts"][0]["scan_truncated"] is False
    assert listing["transcripts"][0]["skipped_records"] == 0


def test_history_listing_counts_an_exact_budget_oversized_record_at_eof(
    tmp_path, monkeypatch,
):
    module = importlib.import_module("translator_runtime")
    session = tmp_path / "session.jsonl"
    session.write_bytes(b"x" * 5)
    monkeypatch.setattr(module.TranscriptHistoryReader, "_MAX_ENTRY_BYTES", 4)
    monkeypatch.setattr(module.TranscriptHistoryReader, "_MAX_LIST_BYTES", 5)

    listing = module.TranscriptHistoryReader(
        tmp_path, session_limit=1, entry_limit=1,
    ).list_sessions()

    assert listing["transcripts"][0]["count"] == 0
    assert listing["transcripts"][0]["count_truncated"] is False
    assert listing["transcripts"][0]["scan_truncated"] is False
    assert listing["transcripts"][0]["skipped_records"] == 1


def test_history_listing_does_not_read_growth_after_its_open_file_snapshot(
    tmp_path, monkeypatch,
):
    module = importlib.import_module("translator_runtime")
    session = tmp_path / "session.jsonl"
    session.write_bytes(b"{}")
    original_open = Path.open

    class GrowingFile:
        def __init__(self):
            self.stream = None

        def __enter__(self):
            self.stream = original_open(session, "rb")
            return self

        def __exit__(self, *_args):
            self.stream.close()
            return False

        def fileno(self):
            return self.stream.fileno()

        def readline(self, size=-1):
            with original_open(session, "ab") as appended:
                appended.write(b"\n{}\n")
            return self.stream.readline(size)

    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: GrowingFile())

    listing = module.TranscriptHistoryReader(
        tmp_path, session_limit=1, entry_limit=1,
    ).list_sessions()

    assert listing["transcripts"][0]["count"] == 0
    assert listing["transcripts"][0]["scan_truncated"] is False
    assert listing["transcripts"][0]["skipped_records"] == 1
    with original_open(session, "rb") as saved:
        assert saved.read() == b"{}\n{}\n"


def test_history_reader_streams_and_retains_only_recent_entries(tmp_path, monkeypatch):
    module = importlib.import_module("translator_runtime")
    session = tmp_path / "2026-09-03_120000.jsonl"
    write_session(session, ["one", "two", "three", "four"])
    (tmp_path / "2026-09-03_120000.wav").write_bytes(b"audio")

    def reject_whole_file_read(*_args, **_kwargs):
        raise AssertionError("history must stream transcript lines")

    monkeypatch.setattr(Path, "read_text", reject_whole_file_read)
    detail = module.TranscriptHistoryReader(
        tmp_path, session_limit=2, entry_limit=2,
    ).read_session(session.name)

    assert [entry["text"] for entry in detail["entries"]] == ["three", "four"]
    assert detail["entries_truncated"] is True
    assert detail["entry_limit"] == 2
    assert detail["label"] == "September 03, 2026 at 12:00 PM"
    assert detail["has_audio"] is True
    assert detail["audio_url"] == "/audio/2026-09-03_120000.wav"


def test_history_detail_skips_corruption_and_preserves_valid_entry_order(tmp_path):
    module = importlib.import_module("translator_runtime")
    session = tmp_path / "2026-09-03_120000.jsonl"
    session.write_bytes(
        b'{"time":"12:00:00","text":"first"}\n'
        b'{not valid json}\n'
        b'{"time":"12:00:02","text":"last"}\n'
        b'{"time":"12:00:03","text":"crash-written"}'
    )

    detail = module.TranscriptHistoryReader(
        tmp_path, session_limit=2, entry_limit=5,
    ).read_session(session.name)

    assert [entry["text"] for entry in detail["entries"]] == ["first", "last"]
    assert detail["entries_truncated"] is False
    assert detail["skipped_records"] == 2


def test_history_reader_logs_one_content_safe_warning_per_affected_operation(
    tmp_path, caplog,
):
    module = importlib.import_module("translator_runtime")
    session = tmp_path / "2026-09-03_120000.jsonl"
    session.write_bytes(b'{"private transcript contents": not-json}\n')
    reader = module.TranscriptHistoryReader(tmp_path, session_limit=2, entry_limit=5)

    reader.list_sessions()
    reader.read_session(session.name)

    warnings = [record.getMessage() for record in caplog.records]
    assert len(warnings) == 2
    assert all(session.name in warning for warning in warnings)
    assert all("1 corrupt record" in warning for warning in warnings)
    assert any("listing" in warning for warning in warnings)
    assert any("reading" in warning for warning in warnings)
    assert all("private transcript contents" not in warning for warning in warnings)


def test_history_reader_escapes_control_characters_in_logged_filenames(
    tmp_path, caplog,
):
    module = importlib.import_module("translator_runtime")
    unsafe_name = "evil\n\x1b[31mforged.jsonl"
    (tmp_path / unsafe_name).write_bytes(b"{damaged}\n")

    module.TranscriptHistoryReader(
        tmp_path, session_limit=2, entry_limit=5,
    ).list_sessions()

    warning = caplog.records[0].getMessage()
    assert "\n" not in warning
    assert "\x1b" not in warning
    assert r"evil\n\x1b[31mforged.jsonl" in warning


def test_history_detail_reads_only_a_bounded_recent_tail(tmp_path, monkeypatch):
    module = importlib.import_module("translator_runtime")
    session = tmp_path / "session.jsonl"
    session.touch()
    payload = b"".join(
        json.dumps({"time": "12:00:00", "text": f"entry {index}"}).encode() + b"\n"
        for index in range(20000)
    )

    class ReadBudget:
        def __init__(self, binary):
            self.stream = BytesIO(payload)
            self.binary = binary
            self.bytes_read = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            return self

        def __next__(self):
            line = self.stream.readline()
            if not line:
                raise StopIteration
            self._record(line)
            return line if self.binary else line.decode()

        def read(self, size=-1):
            data = self.stream.read(size)
            self._record(data)
            return data

        def seek(self, offset, whence=0):
            return self.stream.seek(offset, whence)

        def tell(self):
            return self.stream.tell()

        def _record(self, data):
            self.bytes_read += len(data)
            if self.bytes_read > 70000:
                raise AssertionError("detail exceeded its bounded tail-read budget")

    def guarded_open(_path, *args, **_kwargs):
        return ReadBudget(bool(args and "b" in args[0]))

    monkeypatch.setattr(Path, "open", guarded_open)
    detail = module.TranscriptHistoryReader(
        tmp_path, session_limit=2, entry_limit=2,
    ).read_session(session.name)

    assert [entry["text"] for entry in detail["entries"]] == [
        "entry 19998", "entry 19999",
    ]
    assert detail["entries_truncated"] is True


def test_history_detail_does_not_call_older_blank_lines_truncated_entries(tmp_path):
    module = importlib.import_module("translator_runtime")
    session = tmp_path / "session.jsonl"
    session.write_bytes(
        (b"\n" * 70000) + b'{"time":"12:00:00","text":"only valid entry"}\n'
    )

    detail = module.TranscriptHistoryReader(
        tmp_path, session_limit=1, entry_limit=1,
    ).read_session(session.name)

    assert [entry["text"] for entry in detail["entries"]] == ["only valid entry"]
    assert detail["entries_truncated"] is False
    assert detail["skipped_records"] == 0


def test_history_detail_counts_an_unterminated_final_record_beyond_its_byte_budget(
    tmp_path, monkeypatch,
):
    module = importlib.import_module("translator_runtime")
    session = tmp_path / "session.jsonl"
    session.write_bytes(b"x" * 64)
    monkeypatch.setattr(module.TranscriptHistoryReader, "_MAX_DETAIL_BYTES", 32)

    detail = module.TranscriptHistoryReader(
        tmp_path, session_limit=1, entry_limit=1,
    ).read_session(session.name)

    assert detail["entries"] == []
    assert detail["entries_truncated"] is False
    assert detail["scan_truncated"] is True
    assert detail["skipped_records"] == 1


def test_history_detail_counts_terminated_oversized_records_across_chunk_boundaries(
    tmp_path,
):
    module = importlib.import_module("translator_runtime")
    valid = b'{"time":"12:00:00","text":"valid"}\n'
    skipped_counts = []

    for index, record_size in enumerate((65537, 131071, 131072, 131073)):
        session = tmp_path / f"session-{index}.jsonl"
        session.write_bytes(valid + (b"x" * record_size) + b"\n")
        detail = module.TranscriptHistoryReader(
            tmp_path, session_limit=5, entry_limit=5,
        ).read_session(session.name)
        skipped_counts.append(detail["skipped_records"])
        assert [entry["text"] for entry in detail["entries"]] == ["valid"]

    assert skipped_counts == [1, 1, 1, 1]


def test_history_detail_counts_a_terminated_oversized_record_past_its_byte_budget(
    tmp_path,
):
    module = importlib.import_module("translator_runtime")
    session = tmp_path / "session.jsonl"
    session.write_bytes((b"x" * (4 * 1024 * 1024 + 1)) + b"\n")

    detail = module.TranscriptHistoryReader(
        tmp_path, session_limit=1, entry_limit=1,
    ).read_session(session.name)

    assert detail["entries"] == []
    assert detail["entries_truncated"] is False
    assert detail["scan_truncated"] is True
    assert detail["skipped_records"] == 1


def test_history_count_skips_an_oversized_entry_with_bounded_reads(tmp_path, monkeypatch):
    module = importlib.import_module("translator_runtime")
    session = tmp_path / "session.jsonl"
    session.touch()
    requested_sizes = []
    payload = (
        (b"x" * 65537) + b"\n"
        b'{"time":"12:00:01","text":"valid"}\n'
    )

    class OversizedEntry:
        def __init__(self):
            self.stream = BytesIO(payload)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            raise AssertionError("counting must not iterate an unbounded line")

        def readline(self, size=-1):
            requested_sizes.append(size)
            return self.stream.readline(size)

    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: OversizedEntry())
    reader = module.TranscriptHistoryReader(tmp_path, session_limit=1, entry_limit=1)

    listing = reader.list_sessions()

    assert listing["transcripts"][0]["count"] == 1
    assert listing["transcripts"][0]["skipped_records"] == 1
    assert requested_sizes
    assert max(requested_sizes) <= 65538


def test_history_accepts_an_exactly_64_kib_entry_with_a_line_delimiter(tmp_path):
    module = importlib.import_module("translator_runtime")
    prefix = b'{"time":"12:00:00","text":"'
    suffix = b'"}'
    record = prefix + (b"a" * (65536 - len(prefix) - len(suffix))) + suffix
    session = tmp_path / "2026-09-03_120000.jsonl"
    session.write_bytes(record + b"\n")
    reader = module.TranscriptHistoryReader(tmp_path, session_limit=1, entry_limit=1)

    listing = reader.list_sessions()
    detail = reader.read_session(session.name)

    assert listing["transcripts"][0]["count"] == 1
    assert listing["transcripts"][0]["count_truncated"] is False
    assert len(detail["entries"][0]["text"]) == 65536 - len(prefix) - len(suffix)
    assert detail["entries_truncated"] is False


def test_history_reader_skips_symlinks_that_escape_the_history_root(tmp_path):
    module = importlib.import_module("translator_runtime")
    history = tmp_path / "transcripts"
    history.mkdir()
    outside = tmp_path / "outside.jsonl"
    write_session(outside, ["outside root"])
    write_session(history / "2026-09-02_120000.jsonl", ["inside root"])
    escaped_name = "2026-09-03_120000.jsonl"
    (history / escaped_name).symlink_to(outside)
    reader = module.TranscriptHistoryReader(history, session_limit=5, entry_limit=5)

    listing = reader.list_sessions()

    assert [item["filename"] for item in listing["transcripts"]] == [
        "2026-09-02_120000.jsonl",
    ]
    assert listing["sessions_truncated"] is False
    assert reader.read_session(escaped_name) is None


def test_history_detail_does_not_advertise_an_escaping_audio_symlink(tmp_path):
    module = importlib.import_module("translator_runtime")
    history = tmp_path / "transcripts"
    history.mkdir()
    session = history / "2026-09-03_120000.jsonl"
    write_session(session, ["inside root"])
    outside_audio = tmp_path / "outside.wav"
    outside_audio.write_bytes(b"outside audio")
    (history / "2026-09-03_120000.wav").symlink_to(outside_audio)

    detail = module.TranscriptHistoryReader(
        history, session_limit=5, entry_limit=5,
    ).read_session(session.name)

    assert detail["has_audio"] is False
    assert detail["audio_url"] is None


def test_audio_route_does_not_serve_an_escaping_symlink(tmp_path):
    module = importlib.import_module("translator_runtime")
    application = importlib.import_module("app")
    history = tmp_path / "transcripts"
    history.mkdir()
    outside_audio = tmp_path / "outside.wav"
    outside_audio.write_bytes(b"outside audio")
    (history / "2026-09-03_120000.wav").symlink_to(outside_audio)

    class Runtime(module.TranslatorRuntime):  # pylint: disable=too-few-public-methods
        async def start(self):
            return None

    runtime = Runtime(module.RuntimeConfig.from_environment({}))
    runtime.transcripts_dir = history

    async def exercise():
        app = application.create_app(lambda: runtime)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test",
            ) as client:
                return await client.get("/audio/2026-09-03_120000.wav")

    response = asyncio.run(exercise())
    assert response.status_code == 404
    assert response.content != b"outside audio"


def test_history_pages_make_session_count_and_detail_truncation_visible(tmp_path):
    module = importlib.import_module("translator_runtime")
    application = importlib.import_module("app")
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    write_session(transcripts / "2026-09-01_120000.jsonl", ["excluded session"])
    write_session(transcripts / "2026-09-02_120000.jsonl", ["middle session"])
    write_session(transcripts / "2026-09-03_120000.jsonl", [
        "excluded entry one", "excluded entry two", "recent entry three", "recent entry four",
    ])

    class Runtime(module.TranslatorRuntime):  # pylint: disable=too-few-public-methods
        async def start(self):
            return None

    runtime = Runtime(module.RuntimeConfig.from_environment({
        "TRANSLATOR_HISTORY_SESSION_LIMIT": "2",
        "TRANSLATOR_HISTORY_ENTRY_LIMIT": "2",
    }))
    runtime.transcripts_dir = transcripts
    runtime.templates = module.Jinja2Templates(
        directory=str(Path(__file__).resolve().parents[1] / "templates"),
    )

    async def exercise():
        app = application.create_app(lambda: runtime)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test",
            ) as client:
                listing = await client.get("/history")
                detail = await client.get("/history/2026-09-03_120000.jsonl")
                missing = await client.get("/history/missing.jsonl")
                return listing, detail, missing

    listing, detail, missing = asyncio.run(exercise())
    assert listing.status_code == 200
    assert "Showing the 2 newest sessions" in listing.text
    assert "2026-09-01_120000.jsonl" not in listing.text
    assert "2+ segments" in listing.text
    assert detail.status_code == 200
    assert "Showing the 2 most recent entries" in detail.text
    assert "recent entry three" in detail.text
    assert "recent entry four" in detail.text
    assert "excluded entry one" not in detail.text
    assert missing.status_code == 404


def test_history_pages_disclose_skipped_records_without_hiding_other_sessions(tmp_path):
    module = importlib.import_module("translator_runtime")
    application = importlib.import_module("app")
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    damaged = transcripts / "2026-09-03_120000.jsonl"
    damaged.write_bytes(
        b'{"time":"12:00:00","text":"visible entry"}\n'
        b'{damaged}\n'
    )
    write_session(transcripts / "2026-09-02_120000.jsonl", ["unaffected session"])

    class Runtime(module.TranslatorRuntime):  # pylint: disable=too-few-public-methods
        async def start(self):
            return None

    runtime = Runtime(module.RuntimeConfig.from_environment({}))
    runtime.transcripts_dir = transcripts
    runtime.templates = module.Jinja2Templates(
        directory=str(Path(__file__).resolve().parents[1] / "templates"),
    )

    async def exercise():
        app = application.create_app(lambda: runtime)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test",
            ) as client:
                return (
                    await client.get("/history"),
                    await client.get(f"/history/{damaged.name}"),
                )

    listing, detail = asyncio.run(exercise())
    assert listing.status_code == 200
    assert "September 02, 2026" in listing.text
    assert "1 damaged record skipped" in listing.text
    assert detail.status_code == 200
    assert "visible entry" in detail.text
    assert "1 damaged record skipped" in detail.text


def test_history_listing_discloses_when_its_byte_budget_ends(tmp_path, monkeypatch):
    module = importlib.import_module("translator_runtime")
    application = importlib.import_module("app")
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    (transcripts / "session.jsonl").write_bytes(b"\n" * 64)
    monkeypatch.setattr(module.TranscriptHistoryReader, "_MAX_LIST_BYTES", 32)

    class Runtime(module.TranslatorRuntime):  # pylint: disable=too-few-public-methods
        async def start(self):
            return None

    runtime = Runtime(module.RuntimeConfig.from_environment({}))
    runtime.transcripts_dir = transcripts
    runtime.templates = module.Jinja2Templates(
        directory=str(Path(__file__).resolve().parents[1] / "templates"),
    )

    async def exercise():
        app = application.create_app(lambda: runtime)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test",
            ) as client:
                return await client.get("/history")

    response = asyncio.run(exercise())
    assert response.status_code == 200
    assert "History inspection stopped at its read limit" in response.text


def test_history_detail_route_skips_an_escaped_lone_surrogate(tmp_path):
    module = importlib.import_module("translator_runtime")
    application = importlib.import_module("app")
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    session = transcripts / "2026-09-03_120000.jsonl"
    session.write_bytes(
        b'{"time":"12:00:00","text":"visible entry"}\n'
        b'{"time":"12:00:01","text":"\\ud800"}\n'
    )

    class Runtime(module.TranslatorRuntime):  # pylint: disable=too-few-public-methods
        async def start(self):
            return None

    runtime = Runtime(module.RuntimeConfig.from_environment({}))
    runtime.transcripts_dir = transcripts
    runtime.templates = module.Jinja2Templates(
        directory=str(Path(__file__).resolve().parents[1] / "templates"),
    )

    async def exercise():
        app = application.create_app(lambda: runtime)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test",
            ) as client:
                return await client.get(f"/history/{session.name}")

    response = asyncio.run(exercise())
    assert response.status_code == 200
    assert "visible entry" in response.text
    assert "1 damaged record skipped" in response.text


def test_history_detail_route_discloses_when_the_byte_scan_ends(tmp_path, monkeypatch):
    module = importlib.import_module("translator_runtime")
    application = importlib.import_module("app")
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    session = transcripts / "session.jsonl"
    session.write_bytes(b"x" * 64)
    monkeypatch.setattr(module.TranscriptHistoryReader, "_MAX_DETAIL_BYTES", 32)

    class Runtime(module.TranslatorRuntime):  # pylint: disable=too-few-public-methods
        async def start(self):
            return None

    runtime = Runtime(module.RuntimeConfig.from_environment({}))
    runtime.transcripts_dir = transcripts
    runtime.templates = module.Jinja2Templates(
        directory=str(Path(__file__).resolve().parents[1] / "templates"),
    )

    async def exercise():
        app = application.create_app(lambda: runtime)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test",
            ) as client:
                return await client.get(f"/history/{session.name}")

    response = asyncio.run(exercise())
    assert response.status_code == 200
    assert "Older history was not inspected because the read limit was reached" in response.text


@pytest.mark.parametrize("route, reader_method", [
    ("/history", "list_sessions"),
    ("/history/2026-09-03_120000.jsonl", "read_session"),
])
def test_history_file_work_does_not_block_unrelated_requests(
    tmp_path, monkeypatch, route, reader_method,
):
    module = importlib.import_module("translator_runtime")
    application = importlib.import_module("app")
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    write_session(transcripts / "2026-09-03_120000.jsonl", ["entry"])
    entered = threading.Event()
    release = threading.Event()
    worker_thread_ids = []
    original = getattr(module.TranscriptHistoryReader, reader_method)

    def paused(reader, *args):
        worker_thread_ids.append(threading.get_ident())
        entered.set()
        assert release.wait(3)
        return original(reader, *args)

    monkeypatch.setattr(module.TranscriptHistoryReader, reader_method, paused)

    class Runtime(module.TranslatorRuntime):  # pylint: disable=too-few-public-methods
        async def start(self):
            return None

    runtime = Runtime(module.RuntimeConfig.from_environment({}))
    runtime.transcripts_dir = transcripts
    runtime.frontend_dist = tmp_path / "missing-frontend"
    runtime.templates = module.Jinja2Templates(
        directory=str(Path(__file__).resolve().parents[1] / "templates"),
    )

    async def exercise():
        app = application.create_app(lambda: runtime)
        event_loop_thread_id = threading.get_ident()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test",
            ) as client:
                history_request = asyncio.create_task(client.get(route))
                try:
                    assert await asyncio.to_thread(entered.wait, 1)
                    unrelated = await asyncio.wait_for(client.get("/"), 0.5)
                    assert unrelated.status_code == 200
                finally:
                    release.set()
                history_response = await history_request
                assert history_response.status_code == 200
        assert worker_thread_ids != [event_loop_thread_id]

    asyncio.run(exercise())
