"""Collision-resistant, atomically claimed paths for live sessions."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import errno
import os
from pathlib import Path
import re
import secrets
from typing import BinaryIO

from wav_recorder import RotatingWavRecorder


class SessionReservationError(OSError):
    """A safe live-session filename could not be reserved."""


@dataclass
class LiveSessionFiles:
    """Paired transcript and audio paths owned by one reserved session stem."""

    stem: str
    transcript_path: Path
    audio_path: Path
    _audio_stream: BinaryIO | None = field(repr=False, compare=False)

    _TOKEN = re.compile(r"[0-9a-f]{16}")
    _PART = re.compile(r"\.part[0-9]{3,}\.wav")
    _ZONE = re.compile(r"[A-Za-z0-9+-]{1,16}")
    _MAX_ATTEMPTS = 128

    @classmethod
    def reserve(cls, directory: Path, clock=None, token_factory=None):
        """Atomically claim a unique marker without following directory symlinks."""
        clock = cls._local_now if clock is None else clock
        token_factory = cls._token if token_factory is None else token_factory
        root, directory_fd = cls._open_directory(directory)
        try:
            for _attempt in range(cls._MAX_ATTEMPTS):
                instant = clock()
                if instant.tzinfo is None or instant.utcoffset() is None:
                    raise ValueError("Live session timestamps must include a time zone")
                token = token_factory()
                if cls._TOKEN.fullmatch(token) is None:
                    raise ValueError(
                        "Live session tokens must be 16 lowercase hexadecimal digits"
                    )
                stem = cls._stem(instant, token)
                marker_name = f"{stem}.jsonl"
                audio_name = f"{stem}.wav"
                claimed = cls._claim_candidate(
                    directory_fd, marker_name, audio_name, stem,
                )
                if claimed is not None:
                    return cls(
                        stem=stem,
                        transcript_path=root / marker_name,
                        audio_path=root / audio_name,
                        _audio_stream=claimed,
                    )
        finally:
            os.close(directory_fd)
        raise SessionReservationError("Could not reserve a unique live session filename")

    def create_audio_recorder(
        self, storage_budget_bytes: int, part_data_limit: int | None = None,
    ) -> RotatingWavRecorder:
        """Transfer the reserved WAV descriptor into this session's recorder."""
        if self._audio_stream is None:
            raise RuntimeError("Reserved audio path has already been transferred")
        recorder = RotatingWavRecorder(
            self.transcript_path.parent,
            self.stem,
            storage_budget_bytes,
            part_data_limit,
            first_part_stream=self._audio_stream,
        )
        self._audio_stream = None
        return recorder

    def close(self) -> None:
        """Close a reserved audio descriptor that was not transferred."""
        stream, self._audio_stream = self._audio_stream, None
        if stream is not None:
            stream.close()

    @staticmethod
    def _local_now() -> datetime:
        return datetime.now().astimezone()

    @staticmethod
    def _token() -> str:
        return secrets.token_hex(8)

    @classmethod
    def _stem(cls, instant: datetime, token: str) -> str:
        zone = instant.tzname() or "LOCAL"
        if cls._ZONE.fullmatch(zone) is None:
            zone = "LOCAL"
        return "--".join((
            instant.astimezone(timezone.utc).strftime("%Y-%m-%d_%H%M%S_%fZ"),
            instant.strftime(f"%Y-%m-%d_%H%M%S_%f%z-{zone}"),
            token,
        ))

    @staticmethod
    def _open_directory(directory: Path) -> tuple[Path, int]:
        directory = Path(directory)
        if directory.is_symlink():
            raise SessionReservationError("Refusing a symlinked transcript directory")
        try:
            root = directory.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise SessionReservationError("Could not open the transcript directory") from exc
        if not root.is_dir():
            raise SessionReservationError("Transcript directory is not a directory")
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            return root, os.open(root, flags)
        except OSError as exc:
            raise SessionReservationError("Could not open the transcript directory") from exc

    @classmethod
    def _claim_candidate(
        cls, directory_fd: int, marker_name: str, audio_name: str, stem: str,
    ) -> BinaryIO | None:
        marker_fd = None
        audio_fd = None
        try:
            marker_fd = cls._create_file(directory_fd, marker_name)
        except FileExistsError:
            return None
        marker_identity = cls._identity(marker_fd)
        try:
            audio_fd = cls._create_file(directory_fd, audio_name)
        except FileExistsError:
            os.close(marker_fd)
            cls._remove_owned(directory_fd, marker_name, marker_identity)
            return None
        except OSError:
            os.close(marker_fd)
            cls._remove_owned(directory_fd, marker_name, marker_identity)
            raise
        audio_identity = cls._identity(audio_fd)
        if cls._part_exists(directory_fd, stem):
            os.close(audio_fd)
            os.close(marker_fd)
            cls._remove_owned(directory_fd, audio_name, audio_identity)
            cls._remove_owned(directory_fd, marker_name, marker_identity)
            return None
        try:
            os.close(marker_fd)
            return os.fdopen(audio_fd, "wb")
        except Exception as exc:
            if audio_fd is not None:
                try:
                    os.close(audio_fd)
                except OSError:
                    pass
            cls._remove_owned(directory_fd, audio_name, audio_identity)
            cls._remove_owned(directory_fd, marker_name, marker_identity)
            raise SessionReservationError("Could not retain a session reservation") from exc

    @staticmethod
    def _create_file(directory_fd: int, name: str) -> int:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        return os.open(name, flags, 0o600, dir_fd=directory_fd)

    @staticmethod
    def _identity(descriptor: int) -> tuple[int, int]:
        status = os.fstat(descriptor)
        return status.st_dev, status.st_ino

    @staticmethod
    def _remove_owned(directory_fd: int, name: str, identity: tuple[int, int]) -> None:
        try:
            status = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return
            raise SessionReservationError(
                "Could not safely release a colliding session marker"
            ) from exc
        if (status.st_dev, status.st_ino) != identity:
            raise SessionReservationError(
                "Could not safely release a replaced session marker"
            )
        try:
            os.unlink(name, dir_fd=directory_fd)
        except OSError as exc:
            raise SessionReservationError(
                "Could not safely release a colliding session marker"
            ) from exc

    @classmethod
    def _part_exists(cls, directory_fd: int, stem: str) -> bool:
        prefix = f"{stem}.part"
        return any(
            name.startswith(prefix) and cls._PART.fullmatch(name[len(stem):]) is not None
            for name in os.listdir(directory_fd)
        )
