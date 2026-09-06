"""Language option resolution — no Whisper model is loaded by any of this."""

import pytest

from language import LanguageOption


class TestUnsetFallsBackToDefault:
    """Empty, whitespace-only, and absent must all mean "the default"."""

    @pytest.mark.parametrize("raw", ["", "   ", "\t\n", None])
    def test_blank_resolves_to_default(self, raw):
        assert LanguageOption(raw, default="es").code == "es"

    def test_blank_never_reaches_the_backend_as_empty_string(self):
        # The bug: os.environ.get(name, "es") returns "" for an exported-but-empty
        # var, and '' then reaches Whisper as a language code.
        assert LanguageOption("", default="es").code != ""

    def test_missing_env_var_uses_default(self):
        assert LanguageOption.from_env({}, default="es").code == "es"

    def test_empty_env_var_uses_default(self):
        assert LanguageOption.from_env({"TRANSLATOR_LANGUAGE": ""}, default="es").code == "es"


class TestAutoMeansDetect:
    """`auto` is how a user asks Whisper to detect, which the API spells None."""

    @pytest.mark.parametrize("raw", ["auto", "AUTO", " Auto "])
    def test_auto_resolves_to_none(self, raw):
        assert LanguageOption(raw).code is None

    def test_auto_is_not_an_error(self):
        assert LanguageOption("auto").is_auto


class TestValidCodePassesThrough:
    @pytest.mark.parametrize("raw", ["en", "es", "ja", "yue"])
    def test_known_code_is_returned_unchanged(self, raw):
        assert LanguageOption(raw).code == raw

    @pytest.mark.parametrize("raw", [" en ", "EN", "Es"])
    def test_surrounding_space_and_case_are_normalized(self, raw):
        assert LanguageOption(raw).code == raw.strip().lower()


class TestUnknownCodeIsRejected:
    def test_unknown_code_raises(self):
        with pytest.raises(ValueError):
            LanguageOption("klingon")

    def test_message_names_the_offending_value(self):
        with pytest.raises(ValueError, match="klingon"):
            LanguageOption("klingon")

    def test_message_points_at_the_accepted_list(self):
        with pytest.raises(ValueError, match="en"):
            LanguageOption("klingon")

    def test_a_default_that_is_itself_invalid_is_rejected(self):
        with pytest.raises(ValueError, match="nope"):
            LanguageOption("", default="nope")
