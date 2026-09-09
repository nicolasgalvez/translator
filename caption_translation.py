"""Supported language policy for bilingual caption translation."""


class UnsupportedCaptionLanguageError(ValueError):
    """One or more detected caption languages have no translation route."""


class CaptionTranslationPolicy:
    """Own the complete set of supported caption translation routes."""

    _TARGET_BY_SOURCE = {"en": "es", "es": "en"}

    @property
    def routes(self) -> tuple[tuple[str, str], ...]:
        """Return every package and dispatch route in stable order."""
        return tuple(self._TARGET_BY_SOURCE.items())

    @property
    def supported_languages(self) -> tuple[str, ...]:
        """Return every accepted detected-language code."""
        return tuple(self._TARGET_BY_SOURCE)

    def route_for(self, source: str) -> tuple[str, str]:
        """Return the source and target codes, or reject an unsupported source."""
        try:
            return source, self._TARGET_BY_SOURCE[source]
        except KeyError as exc:
            raise self._unsupported_error([source]) from exc

    def validate_segments(self, segments: list[dict]) -> None:
        """Reject all unsupported detected languages in one actionable error."""
        unsupported = sorted({
            segment.get("language")
            for segment in segments
            if segment.get("language") not in self._TARGET_BY_SOURCE
        }, key=str)
        if unsupported:
            raise self._unsupported_error(unsupported)

    def _unsupported_error(self, codes) -> UnsupportedCaptionLanguageError:
        unsupported = ", ".join(map(str, codes))
        supported = ", ".join(self.supported_languages)
        return UnsupportedCaptionLanguageError(
            f"Unsupported caption language(s): {unsupported}. "
            f"Supported languages: {supported}.",
        )
