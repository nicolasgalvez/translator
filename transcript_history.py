"""Bounded, presentation-ready access to saved transcript sessions."""

from collections import deque
from datetime import datetime
import heapq
import json
from pathlib import Path


class TranscriptHistoryReader:
    """Read recent transcript sessions without retaining unbounded file data."""

    _READ_CHUNK_BYTES = 65536
    _MAX_ENTRY_BYTES = 65536
    _MAX_DETAIL_BYTES = 4 * 1024 * 1024

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
                key=lambda candidate: candidate[0].name,
            )
        sessions_truncated = len(candidates) > self.session_limit
        transcripts = []
        for display_path, resolved_path in candidates[:self.session_limit]:
            count, count_truncated = self._count_entries(resolved_path)
            transcripts.append({
                "filename": display_path.name,
                "label": self._label(display_path.stem),
                "count": count,
                "count_truncated": count_truncated,
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
        entries, entries_truncated = self._read_recent_entries(path)
        stem = requested_path.stem
        has_audio = self.audio_file(f"{stem}.wav") is not None
        return {
            "label": self._label(stem),
            "entries": list(entries),
            "entries_truncated": entries_truncated,
            "entry_limit": self.entry_limit,
            "has_audio": has_audio,
            "audio_url": f"/audio/{stem}.wav" if has_audio else None,
        }

    def audio_file(self, filename: str) -> Path | None:
        """Return a contained regular WAV path, excluding symlinks."""
        requested_path = self.directory / filename
        if (Path(filename).name != filename or requested_path.suffix != ".wav"
                or requested_path.is_symlink()):
            return None
        return self._resolved_file(self.directory.resolve(), requested_path)

    def _count_entries(self, path: Path) -> tuple[int, bool]:
        count = 0
        with path.open("rb") as transcript:
            while line := transcript.readline(self._MAX_ENTRY_BYTES + 2):
                if line.endswith(b"\n"):
                    line = line[:-1]
                self._validate_entry_size(line)
                if line.strip():
                    json.loads(line)
                    count += 1
                    if count > self.entry_limit:
                        return self.entry_limit, True
        return count, False

    def _read_recent_entries(self, path: Path) -> tuple[list[dict], bool]:
        recent = deque(maxlen=self.entry_limit)
        with path.open("rb") as transcript:
            transcript.seek(0, 2)
            position = transcript.tell()
            remaining = min(position, self._MAX_DETAIL_BYTES)
            leading = b""
            while position and remaining:
                size = min(self._READ_CHUNK_BYTES, position, remaining)
                position -= size
                transcript.seek(position)
                chunk = transcript.read(size)
                remaining -= len(chunk)
                parts = (chunk + leading).split(b"\n")
                leading = parts[0]
                for line in reversed(parts[1:]):
                    if not line.strip():
                        continue
                    if len(recent) == self.entry_limit:
                        return list(reversed(recent)), True
                    recent.append(self._parse_entry(line))
                if len(leading) > self._MAX_ENTRY_BYTES:
                    raise ValueError(
                        f"Transcript entry exceeds the maximum of {self._MAX_ENTRY_BYTES} bytes",
                    )
                if len(recent) == self.entry_limit and leading.strip():
                    return list(reversed(recent)), True
            if position:
                return list(reversed(recent)), True
            if leading.strip():
                if len(recent) == self.entry_limit:
                    return list(reversed(recent)), True
                recent.append(self._parse_entry(leading))
        return list(reversed(recent)), False

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

    def _parse_entry(self, line: bytes) -> dict:
        self._validate_entry_size(line)
        return json.loads(line)

    def _validate_entry_size(self, line: bytes) -> None:
        if len(line) > self._MAX_ENTRY_BYTES:
            raise ValueError(
                f"Transcript entry exceeds the maximum of {self._MAX_ENTRY_BYTES} bytes",
            )

    @staticmethod
    def _label(stem: str) -> str:
        try:
            timestamp = datetime.strptime(stem, "%Y-%m-%d_%H%M%S")
            return timestamp.strftime("%B %d, %Y at %I:%M %p")
        except ValueError:
            return stem
