"""Resolution of the configured transcription language.

Whisper accepts a two-or-three letter code, or nothing at all to mean "detect
it". Everything else it rejects — including the empty string, which is easy to
produce by accident and only surfaces once audio is already flowing.
"""

# Whisper's accepted codes, including `yue`, which arrived with large-v3.
#
# Deliberately a literal rather than an import from faster-whisper or
# mlx-whisper: this module has to be importable, and testable, without either
# optional backend installed. The CI test job installs requirements-dev.txt only.
WHISPER_LANGUAGE_CODES = frozenset("""
af am ar as az ba be bg bn bo br bs ca cs cy da de el en es et eu fa fi fo fr
gl gu ha haw he hi hr ht hu hy id is it ja jw ka kk km kn ko la lb ln lo lt lv
mg mi mk ml mn mr ms mt my ne nl nn no oc pa pl ps pt ro ru sa sd si sk sl sn
so sq sr su sv sw ta te tg th tk tl tr tt uk ur uz vi yi yo zh yue
""".split())


class LanguageOption:
    """The configured language, resolved to what a backend actually takes.

    Blank input means "unset" and falls back to the default — the empty string
    is not a language, and treating it as one is the bug this class exists to
    prevent. `auto` means detect, which the backend API spells as None.

    Raises ValueError on anything Whisper would reject, so a bad value fails at
    startup with a message naming it, rather than once per utterance forever.
    """

    AUTO = "auto"
    ENV_VAR = "TRANSLATOR_LANGUAGE"

    def __init__(self, raw: str | None, default: str = "es") -> None:
        self._default = self._validated(default, source="default")
        value = (raw or "").strip().lower()
        self._value = (
            self._validated(value, source=self.ENV_VAR) if value else self._default
        )

    @classmethod
    def from_env(cls, environ, default: str = "es") -> "LanguageOption":
        """Build from an environment mapping. Absent and empty behave alike."""
        return cls(environ.get(cls.ENV_VAR), default=default)

    @property
    def is_auto(self) -> bool:
        """True when the language should be detected rather than declared."""
        return self._value == self.AUTO

    @property
    def code(self) -> str | None:
        """The value to hand a backend: a code, or None to auto-detect."""
        return None if self.is_auto else self._value

    def __str__(self) -> str:
        return self.AUTO if self.is_auto else str(self.code)

    @classmethod
    def _validated(cls, value: str, source: str) -> str:
        candidate = (value or "").strip().lower()
        if candidate == cls.AUTO or candidate in WHISPER_LANGUAGE_CODES:
            return candidate
        raise ValueError(
            f"{candidate!r} is not a valid language ({source}). "
            f"Use 'auto' to detect, or one of: {', '.join(sorted(WHISPER_LANGUAGE_CODES))}"
        )
