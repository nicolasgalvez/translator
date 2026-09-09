"""Bounded, rotating PCM WAV recording for one live transcript session."""

from contextlib import suppress
import os
from pathlib import Path
import re
import stat
import wave


class RecordingError(OSError):
    """Recording could not continue for this session."""


class RecordingStorageError(RecordingError):
    """The managed recording storage budget cannot fit another write."""


# The recorder owns its format, quota, part, and lifecycle state together.
# pylint: disable=too-many-instance-attributes
class RotatingWavRecorder:
    """Write one session into independently playable size-bounded WAV parts."""

    _CHANNELS = 1
    _SAMPLE_WIDTH = 2
    _SAMPLE_RATE = 48000
    _RIFF_SIZE_LIMIT = (1 << 32) - 1
    _WAV_HEADER_BYTES = 44
    _PART_NAME = re.compile(r"^(?P<stem>.+?)(?:\.part(?P<part>[0-9]{3,}))?\.wav$")
    _DEFAULT_PART_DATA_LIMIT = (
        (_RIFF_SIZE_LIMIT - 36) // (_CHANNELS * _SAMPLE_WIDTH)
        * (_CHANNELS * _SAMPLE_WIDTH)
    )

    def __init__(
        self, directory: Path, session_stem: str, storage_budget_bytes: int,
        part_data_limit: int | None = None,
        *, first_part_stream=None,
    ):
        self.directory = directory
        self.session_stem = session_stem
        self.storage_budget_bytes = storage_budget_bytes
        self.part_data_limit = (
            self._DEFAULT_PART_DATA_LIMIT if part_data_limit is None else part_data_limit
        )
        frame_bytes = self._CHANNELS * self._SAMPLE_WIDTH
        if self.part_data_limit > self._DEFAULT_PART_DATA_LIMIT:
            raise ValueError("WAV part data limit exceeds the RIFF size field")
        if self.part_data_limit <= 0 or self.part_data_limit % frame_bytes:
            raise ValueError("WAV part data limit must contain whole PCM frames")
        self._part_number = 1
        self._written_bytes = 0
        self._writer = None
        self._stream = first_part_stream
        self._current_part_created = first_part_stream is not None
        self._current_part_identity = None
        self._closed = False
        self._enabled = True

    @property
    def enabled(self) -> bool:
        """Report whether this session can still retain recording data."""
        return self._enabled and not self._closed

    def write(self, pcm: bytes) -> None:
        """Write one complete PCM chunk, rotating before it would cross the limit."""
        if not self.enabled:
            return
        try:
            self._write(pcm)
        except Exception as exc:
            self._disable()
            if isinstance(exc, RecordingError):
                raise
            raise RecordingError("Recording write failed") from exc

    def open(self) -> None:
        """Open the first WAV part after making room for its finalized header."""
        if not self.enabled or self._writer is not None:
            return
        try:
            self._ensure_capacity(self._WAV_HEADER_BYTES)
            self._open_writer()
        except Exception as exc:
            self._disable()
            if isinstance(exc, RecordingError):
                raise
            raise RecordingError("Recording open failed") from exc

    def _write(self, pcm: bytes) -> None:
        if len(pcm) % (self._CHANNELS * self._SAMPLE_WIDTH):
            raise RecordingError("PCM writes must contain whole PCM frames")
        if len(pcm) > self.part_data_limit:
            raise RecordingError("PCM chunk exceeds the WAV part data limit")
        if self._writer is not None and self._written_bytes + len(pcm) > self.part_data_limit:
            self._close_writer()
            self._part_number += 1
            self._written_bytes = 0
        additional_bytes = len(pcm)
        if self._writer is None:
            additional_bytes += self._WAV_HEADER_BYTES
        self._ensure_capacity(additional_bytes)
        if self._writer is None:
            self._open_writer()
        try:
            self._writer.writeframes(pcm)
        except Exception:
            if self._written_bytes == 0:
                self._discard_unconfirmed_part()
            raise
        self._written_bytes += len(pcm)

    def close(self) -> None:
        """Finalize the current WAV part once."""
        if self._closed:
            return
        self._closed = True
        self._enabled = False
        self._close_writer()

    def _open_writer(self) -> None:
        part_path = self._part_path()
        root = self.directory.resolve()
        if part_path.is_symlink() or part_path.parent.resolve() != root:
            raise RecordingError("Refusing unsafe recording path")
        if (part_path.exists() or part_path.is_symlink()) and self._safe_regular_file(
            root, part_path,
        ) is None:
            raise RecordingError("Refusing unsafe recording path")
        writer = None
        stream = self._stream
        part_identity = None
        try:
            if stream is not None:
                part_identity = self._stream_path_identity(stream, part_path)
                if part_identity is None:
                    raise RecordingError("Refusing a replaced reserved audio path")
            else:
                stream = part_path.open("xb")
                part_identity = self._stream_path_identity(stream, part_path)
                if part_identity is None:
                    raise RecordingError("Refusing unsafe recording path")
            writer = wave.open(stream, "wb")
            writer.setnchannels(self._CHANNELS)
            writer.setsampwidth(self._SAMPLE_WIDTH)
            writer.setframerate(self._SAMPLE_RATE)
        except Exception:
            if writer is not None:
                with suppress(Exception):
                    writer.close()
            if stream is not None:
                with suppress(Exception):
                    stream.close()
                self._remove_owned_path(part_path, part_identity)
            self._stream = None
            self._current_part_created = False
            self._current_part_identity = None
            raise
        self._writer = writer
        self._stream = stream
        self._current_part_created = True
        self._current_part_identity = part_identity

    def _close_writer(self) -> None:
        writer, self._writer = self._writer, None
        stream, self._stream = self._stream, None
        self._current_part_created = False
        self._current_part_identity = None
        error = None
        if writer is not None:
            try:
                writer.close()
            except Exception as exc:  # pylint: disable=broad-exception-caught
                error = exc
        if stream is not None:
            try:
                stream.close()
            except Exception as exc:  # pylint: disable=broad-exception-caught
                if error is None:
                    error = exc
        if error is not None:
            raise error

    def _discard_unconfirmed_part(self) -> None:
        part_path = self._part_path()
        writer, self._writer = self._writer, None
        stream, self._stream = self._stream, None
        created, self._current_part_created = self._current_part_created, False
        identity, self._current_part_identity = self._current_part_identity, None
        if created and identity is None and stream is not None:
            identity = self._stream_path_identity(stream, part_path)
        if writer is not None:
            with suppress(Exception):
                writer.close()
        if stream is not None:
            with suppress(Exception):
                stream.close()
        if created:
            self._remove_owned_path(part_path, identity)

    def _disable(self) -> None:
        self._enabled = False
        with suppress(Exception):
            if self._writer is None and self._current_part_created:
                self._discard_unconfirmed_part()
            else:
                self._close_writer()

    def _ensure_capacity(self, additional_bytes: int) -> None:
        groups, retained_bytes = self._managed_groups()
        for _oldest, stem, paths, group_bytes in sorted(groups):
            if retained_bytes + additional_bytes <= self.storage_budget_bytes:
                break
            if stem == self.session_stem:
                continue
            self._delete_group(paths)
            retained_bytes -= group_bytes
        if retained_bytes + additional_bytes > self.storage_budget_bytes:
            raise RecordingStorageError("Recording storage budget exhausted")

    def _managed_groups(self):
        root = self.directory.resolve()
        grouped = {}
        for path in self.directory.iterdir():
            parsed = self._managed_part(root, path)
            if parsed is None:
                continue
            stem, part_number, size, modified = parsed
            group = grouped.setdefault(stem, {"paths": [], "size": 0, "oldest": modified})
            group["paths"].append((part_number, path, size))
            group["size"] += size
            group["oldest"] = min(group["oldest"], modified)
        if self._writer is not None:
            active = grouped[self.session_stem]
            current_path = self._part_path()
            on_disk_bytes = next(
                size for _number, path, size in active["paths"] if path == current_path
            )
            active["size"] += max(
                0, self._WAV_HEADER_BYTES + self._written_bytes - on_disk_bytes,
            )
        groups = [
            (group["oldest"], stem, group["paths"], group["size"])
            for stem, group in grouped.items()
        ]
        return groups, sum(group[3] for group in groups)

    def _managed_part(self, root: Path, path: Path):
        match = self._PART_NAME.fullmatch(path.name)
        if match is None or path.is_symlink():
            return None
        part_text = match.group("part")
        part_number = 1
        if part_text is not None:
            part_number = int(part_text)
            if part_number < 2 or part_text != f"{part_number:03d}":
                return None
        stem = match.group("stem")
        if stem != self.session_stem and self._safe_regular_file(
            root, self.directory / f"{stem}.jsonl",
        ) is None:
            return None
        resolved = self._safe_regular_file(root, path)
        if resolved is None:
            return None
        status = resolved.stat()
        return (
            stem, part_number,
            status.st_size, status.st_mtime_ns,
        )

    @staticmethod
    def _safe_regular_file(root: Path, path: Path) -> Path | None:
        if path.is_symlink():
            return None
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
            if resolved.is_file():
                return resolved
        except (OSError, RuntimeError, ValueError):
            pass
        return None

    @staticmethod
    def _stream_path_identity(stream, path: Path) -> tuple[int, int] | None:
        try:
            opened = os.fstat(stream.fileno())
            current = path.stat(follow_symlinks=False)
        except (OSError, ValueError):
            return None
        identity = opened.st_dev, opened.st_ino
        if stat.S_ISREG(current.st_mode) and identity == (current.st_dev, current.st_ino):
            return identity
        return None

    @staticmethod
    def _remove_owned_path(path: Path, identity: tuple[int, int] | None) -> None:
        if identity is None:
            return
        try:
            current = path.stat(follow_symlinks=False)
            if (current.st_dev, current.st_ino) == identity:
                path.unlink()
        except OSError:
            pass

    @staticmethod
    def _delete_group(paths) -> None:
        for _part_number, path, _size in sorted(paths):
            path.unlink()

    def _part_path(self) -> Path:
        if self._part_number == 1:
            return self.directory / f"{self.session_stem}.wav"
        return self.directory / f"{self.session_stem}.part{self._part_number:03d}.wav"
