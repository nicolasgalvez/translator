"""Validated translator configuration with no third-party dependencies."""

from __future__ import annotations

from dataclasses import dataclass

from language import LanguageOption
from websocket_security import WebSocketOriginPolicy


def _positive_integer_setting(environ, variable: str, default: str) -> int:
    try:
        value = int(environ.get(variable, default))
        if value <= 0:
            raise ValueError
    except ValueError as exc:
        raise ValueError(f"{variable} must be a positive integer") from exc
    return value


@dataclass(frozen=True)
class RuntimeConfig:  # pylint: disable=too-many-instance-attributes
    """Validated environment values; constructing these acquires no resources."""

    host: str
    port: int
    model: str
    device_name: str
    backend_name: str
    language: LanguageOption
    max_upload_bytes: int
    allowed_origins: frozenset[str] = frozenset()
    caption_concurrency: int = 1
    caption_queue_capacity: int = 2
    caption_retention_seconds: int = 86400
    history_session_limit: int = 50
    history_entry_limit: int = 500
    websocket_queue_capacity: int = 32
    recording_storage_bytes: int = 8 * 1024 * 1024 * 1024
    max_decoded_audio_bytes: int = 256 * 1024 * 1024

    @classmethod
    def from_environment(cls, environ):
        values = {}
        for name, default in (
            ("HOST", "127.0.0.1"), ("MODEL", "small"),
            ("DEVICE", "default"), ("BACKEND", "faster-whisper"),
        ):
            value = environ.get(f"TRANSLATOR_{name}", default)
            if not value.strip():
                raise ValueError(f"TRANSLATOR_{name} must not be blank")
            values[name] = value
        try:
            port = int(environ.get("TRANSLATOR_PORT", "8765"))
        except ValueError as exc:
            raise ValueError("TRANSLATOR_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise ValueError("TRANSLATOR_PORT must be between 1 and 65535")
        try:
            max_upload_bytes = int(environ.get("TRANSLATOR_MAX_UPLOAD_BYTES", "1073741824"))
        except ValueError as exc:
            raise ValueError("TRANSLATOR_MAX_UPLOAD_BYTES must be a positive integer") from exc
        if max_upload_bytes <= 0:
            raise ValueError("TRANSLATOR_MAX_UPLOAD_BYTES must be a positive integer")
        if values["BACKEND"] not in ("faster-whisper", "mlx-whisper"):
            raise ValueError("TRANSLATOR_BACKEND must be 'faster-whisper' or 'mlx-whisper'")
        origin_setting = environ.get("TRANSLATOR_ALLOWED_ORIGINS", "")
        try:
            allowed_origins = WebSocketOriginPolicy(frozenset(
                origin.strip() for origin in origin_setting.split(",")
            ) if origin_setting.strip() else frozenset()).additional_origins
        except ValueError as exc:
            raise ValueError(
                "TRANSLATOR_ALLOWED_ORIGINS must contain exact HTTP(S) origins"
            ) from exc
        caption_settings = []
        for name, default in (("CONCURRENCY", "1"), ("QUEUE_CAPACITY", "2"),
                              ("RETENTION_SECONDS", "86400")):
            variable = f"TRANSLATOR_CAPTION_{name}"
            try:
                value = int(environ.get(variable, default))
                if value <= 0:
                    raise ValueError
            except ValueError as exc:
                raise ValueError(f"{variable} must be a positive integer") from exc
            caption_settings.append(value)
        history_settings = []
        for name, default in (("SESSION_LIMIT", "50"), ("ENTRY_LIMIT", "500")):
            variable = f"TRANSLATOR_HISTORY_{name}"
            history_settings.append(_positive_integer_setting(environ, variable, default))
        websocket_queue_capacity = _positive_integer_setting(
            environ, "TRANSLATOR_WEBSOCKET_QUEUE_CAPACITY", "32",
        )
        return cls(values["HOST"], port, values["MODEL"], values["DEVICE"],
                   values["BACKEND"], LanguageOption.from_env(environ), max_upload_bytes,
                   allowed_origins, *caption_settings, *history_settings,
                   websocket_queue_capacity, _positive_integer_setting(
                       environ, "TRANSLATOR_RECORDING_STORAGE_BYTES",
                       str(8 * 1024 * 1024 * 1024),
                   ), _positive_integer_setting(
                       environ, "TRANSLATOR_MAX_DECODED_AUDIO_BYTES",
                       str(256 * 1024 * 1024),
                   ))
