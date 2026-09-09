"""Bounded, presentation-ready access to saved transcript sessions."""

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import heapq
import json
import logging
import os
from pathlib import Path
import re

LOGGER = logging.getLogger(__name__)


@dataclass
class _ReverseScanState:
    """Bounded state retained while transcript records are read newest-first."""

    recent: deque[dict]
    right_boundary_complete: bool
    record: bytes = b""
    record_oversized: bool = False
    skipped_records: int = 0
    entries_truncated: bool = False


class TranscriptHistoryReader:
    """Read recent transcript sessions without retaining unbounded file data."""

    _READ_CHUNK_BYTES = 65536
    _MAX_ENTRY_BYTES = 65536
    _MAX_DETAIL_BYTES = 4 * 1024 * 1024
    # Covers more than 1,000 typical 64 KiB records and twice the worst-case
    # bytes needed to count the default 500-entry view plus one.
    _MAX_LIST_BYTES = 64 * 1024 * 1024
    _AUDIO_PART_NUMBER = re.compile(r"[0-9]{3,}")
    _UTC_SESSION_STEM = re.compile(
        r"(?P<utc>[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{6}_[0-9]{6})Z--"
        r"(?P<local>[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{6}_[0-9]{6})"
        r"(?P<offset>[+-][0-9]{4})-(?P<zone>[A-Za-z0-9+-]{1,16})--[0-9a-f]{16}"
    )

    def __init__(self, directory: Path, session_limit: int, entry_limit: int):
        self.directory = directory
        self.session_limit = session_limit
        self.entry_limit = entry_limit

    def list_sessions(self) -> dict:
        """Return bounded newest-session metadata for the history page."""
        if not self.directory.exists():
            candidates = []
        else:
            root = self.directory.resolve()
            candidates = heapq.nlargest(
                self.session_limit + 1,
                self._session_files(root),
                key=lambda candidate: self._session_order(candidate[0]),
            )
        sessions_truncated = len(candidates) > self.session_limit
        transcripts = []
        for display_path, resolved_path in candidates[:self.session_limit]:
            count, count_truncated, scan_truncated, skipped_records = (
                self._count_entries(resolved_path)
            )
            self._warn_corruption(display_path.name, skipped_records, "listing history")
            transcripts.append({
                "filename": display_path.name,
                "label": self._label(display_path.stem),
                "count": count,
                "count_truncated": count_truncated,
                "scan_truncated": scan_truncated,
                "skipped_records": skipped_records,
            })
        return {
            "transcripts": transcripts,
            "sessions_truncated": sessions_truncated,
            "session_limit": self.session_limit,
        }

    def read_session(self, filename: str) -> dict | None:
        """Return recent entries and media metadata for one valid session."""
        requested_path = self.directory / filename
        if Path(filename).name != filename or requested_path.suffix != ".jsonl":
            return None
        path = self._resolved_file(self.directory.resolve(), requested_path)
        if path is None:
            return None
        entries, entries_truncated, scan_truncated, skipped_records = (
            self._read_recent_entries(path)
        )
        self._warn_corruption(requested_path.name, skipped_records, "reading history")
        stem = requested_path.stem
        audio_urls = self._audio_urls(stem)
        return {
            "label": self._label(stem),
            "entries": list(entries),
            "entries_truncated": entries_truncated,
            "scan_truncated": scan_truncated,
            "skipped_records": skipped_records,
            "entry_limit": self.entry_limit,
            "has_audio": bool(audio_urls),
            "audio_url": audio_urls[0] if len(audio_urls) == 1 else None,
            "audio_urls": audio_urls,
        }

    def audio_file(self, filename: str) -> Path | None:
        """Return a contained regular WAV path, excluding symlinks."""
        requested_path = self.directory / filename
        if (Path(filename).name != filename or requested_path.suffix != ".wav"
                or requested_path.is_symlink()):
            return None
        return self._resolved_file(self.directory.resolve(), requested_path)

    def _audio_urls(self, stem: str) -> list[str]:
        base_name = f"{stem}.wav"
        if self.audio_file(base_name) is None:
            return []
        parts = [(1, base_name)]
        prefix = f"{stem}.part"
        for path in self.directory.iterdir():
            name = path.name
            if not name.startswith(prefix) or not name.endswith(".wav"):
                continue
            number_text = name[len(prefix):-4]
            if self._AUDIO_PART_NUMBER.fullmatch(number_text) is None:
                continue
            number = int(number_text)
            if number < 2 or number_text != f"{number:03d}":
                continue
            if self.audio_file(name) is not None:
                parts.append((number, name))
        urls = []
        for expected, (number, name) in enumerate(sorted(parts), start=1):
            if number != expected:
                break
            urls.append(f"/audio/{name}")
        return urls

    def _count_entries(self, path: Path) -> tuple[int, bool, bool, int]:
        count = 0
        count_truncated = False
        scan_truncated = False
        skipped_records = 0
        with path.open("rb") as transcript:
            remaining, snapshot_has_suffix = self._listing_snapshot(transcript)
            while remaining:
                line, remaining = self._read_listing_line(transcript, remaining)
                if not line:
                    break
                terminated = line.endswith(b"\n")
                if not terminated and len(line) > self._MAX_ENTRY_BYTES:
                    remaining = self._drain_oversized_record(transcript, remaining)
                    skipped_records += 1
                    if not remaining:
                        scan_truncated = snapshot_has_suffix is not False
                        break
                    continue
                if not terminated:
                    if not remaining and snapshot_has_suffix is not False:
                        scan_truncated = True
                    else:
                        skipped_records += int(bool(line.strip()))
                    break
                line = line[:-1]
                if line.strip():
                    if self._decode_entry(line) is None:
                        skipped_records += 1
                        continue
                    if count < self.entry_limit:
                        count += 1
                    else:
                        count_truncated = True
                        scan_truncated = True
                        break
            else:
                scan_truncated = snapshot_has_suffix is not False
        return count, count_truncated, scan_truncated, skipped_records

    def _listing_snapshot(self, transcript) -> tuple[int, bool | None]:
        try:
            snapshot_size = os.fstat(transcript.fileno()).st_size
        except (AttributeError, OSError, TypeError):
            return self._MAX_LIST_BYTES, None
        return min(snapshot_size, self._MAX_LIST_BYTES), snapshot_size > self._MAX_LIST_BYTES

    def _read_listing_line(self, transcript, remaining: int) -> tuple[bytes, int]:
        read_size = min(self._MAX_ENTRY_BYTES + 2, remaining)
        line = transcript.readline(read_size)
        return line, remaining - len(line)

    def _drain_oversized_record(self, transcript, remaining: int) -> int:
        while remaining:
            line, remaining = self._read_listing_line(transcript, remaining)
            if not line:
                break
            if line.endswith(b"\n"):
                break
        return remaining

    def _read_recent_entries(self, path: Path) -> tuple[list[dict], bool, bool, int]:
        with path.open("rb") as transcript:
            transcript.seek(0, 2)
            position = transcript.tell()
            remaining = min(position, self._MAX_DETAIL_BYTES)
            right_boundary_complete = position == 0
            if position:
                transcript.seek(-1, 2)
                right_boundary_complete = transcript.read(1) == b"\n"
            state = _ReverseScanState(
                deque(maxlen=self.entry_limit), right_boundary_complete,
            )
            while position and remaining:
                size = min(self._READ_CHUNK_BYTES, position, remaining)
                position -= size
                transcript.seek(position)
                chunk = transcript.read(size)
                remaining -= len(chunk)
                if self._consume_reverse_chunk(state, chunk):
                    return self._reverse_result(state, scan_truncated=False)
            if position:
                if state.record_oversized or not state.right_boundary_complete:
                    state.skipped_records += 1
                return self._reverse_result(state, scan_truncated=True)
            self._consume_oldest_record(state)
        return self._reverse_result(state, scan_truncated=False)

    def _consume_reverse_chunk(
        self, state: _ReverseScanState, chunk: bytes,
    ) -> bool:
        parts = (chunk + state.record).split(b"\n")
        boundary_complete = state.right_boundary_complete
        oversized = state.record_oversized
        for index in range(len(parts) - 1, 0, -1):
            line = parts[index]
            if not boundary_complete:
                state.skipped_records += int(bool(line.strip()) or oversized)
            elif oversized:
                state.skipped_records += 1
            elif line.strip():
                entry = self._decode_entry(line)
                if entry is None:
                    state.skipped_records += 1
                elif len(state.recent) == self.entry_limit:
                    state.entries_truncated = True
                    return True
                else:
                    state.recent.append(entry)
            boundary_complete = True
            oversized = False
        state.record = parts[0]
        state.record_oversized = oversized or len(state.record) > self._MAX_ENTRY_BYTES
        if state.record_oversized:
            state.record = b""
        state.right_boundary_complete = boundary_complete
        return False

    def _consume_oldest_record(self, state: _ReverseScanState) -> None:
        if state.record_oversized:
            state.skipped_records += 1
        elif state.record.strip():
            if not state.right_boundary_complete:
                state.skipped_records += 1
                return
            entry = self._decode_entry(state.record)
            if entry is None:
                state.skipped_records += 1
            elif len(state.recent) == self.entry_limit:
                state.entries_truncated = True
            else:
                state.recent.append(entry)

    @staticmethod
    def _reverse_result(state: _ReverseScanState, scan_truncated: bool):
        return (
            list(reversed(state.recent)), state.entries_truncated,
            scan_truncated, state.skipped_records,
        )

    def _session_files(self, root: Path):
        for path in self.directory.iterdir():
            if path.suffix != ".jsonl":
                continue
            resolved = self._resolved_file(root, path)
            if resolved is not None:
                yield path, resolved

    @staticmethod
    def _resolved_file(root: Path, path: Path) -> Path | None:
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
            if resolved.is_file():
                return resolved
        except (OSError, RuntimeError, ValueError):
            pass
        return None

    @classmethod
    def _decode_entry(cls, line: bytes) -> dict | None:
        if len(line) > cls._MAX_ENTRY_BYTES:
            return None
        try:
            entry = json.loads(line, parse_constant=cls._reject_json_constant)
        except (ValueError, UnicodeDecodeError, RecursionError):
            return None
        if not isinstance(entry, dict) or cls._contains_unicode_surrogate(entry):
            return None
        return entry

    @staticmethod
    def _contains_unicode_surrogate(entry: dict) -> bool:
        pending = [entry]
        while pending:
            value = pending.pop()
            if isinstance(value, str):
                if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
                    return True
            elif isinstance(value, dict):
                pending.extend(value.keys())
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
        return False

    @staticmethod
    def _reject_json_constant(value: str):
        raise ValueError(f"Invalid JSON constant: {value}")

    @staticmethod
    def _warn_corruption(filename: str, skipped_records: int, operation: str) -> None:
        if skipped_records:
            LOGGER.warning(
                "Skipped %d corrupt record%s in %s while %s",
                skipped_records,
                "" if skipped_records == 1 else "s",
                ascii(filename),
                operation,
            )

    @staticmethod
    def _label(stem: str) -> str:
        match = TranscriptHistoryReader._UTC_SESSION_STEM.fullmatch(stem)
        if match is not None:
            try:
                timestamp = datetime.strptime(
                    match.group("local"), "%Y-%m-%d_%H%M%S_%f",
                )
                offset = match.group("offset")
                return (
                    timestamp.strftime("%B %d, %Y at %I:%M %p ")
                    + match.group("zone")
                    + f" (UTC{offset[:3]}:{offset[3:]})"
                )
            except ValueError:
                return stem
        try:
            timestamp = datetime.strptime(stem, "%Y-%m-%d_%H%M%S")
            return timestamp.strftime("%B %d, %Y at %I:%M %p")
        except ValueError:
            return stem

    @classmethod
    def _session_order(cls, path: Path) -> tuple[datetime, str]:
        stem = path.stem
        match = cls._UTC_SESSION_STEM.fullmatch(stem)
        try:
            if match is not None:
                timestamp = datetime.strptime(
                    match.group("utc"), "%Y-%m-%d_%H%M%S_%f",
                ).replace(tzinfo=timezone.utc)
            else:
                timestamp = datetime.strptime(
                    stem, "%Y-%m-%d_%H%M%S",
                ).astimezone(timezone.utc)
        except ValueError:
            timestamp = datetime.min.replace(tzinfo=timezone.utc)
        return timestamp, path.name
