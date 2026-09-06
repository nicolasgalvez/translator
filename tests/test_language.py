"""Language option resolution — no Whisper model is loaded by any of this."""

import unittest

from language import LanguageOption


class BlankFallsBackToDefaultTests(unittest.TestCase):
    """Empty, whitespace-only, and absent must all mean "the default"."""

    def test_blank_resolves_to_default(self):
        for raw in ["", "   ", "\t\n", None]:
            with self.subTest(raw=raw):
                self.assertEqual(LanguageOption(raw, default="es").code, "es")

    def test_blank_never_reaches_the_backend_as_empty_string(self):
        # The bug: os.environ.get(name, "es") returns "" for an exported-but-empty
        # var, and '' then reaches Whisper as a language code.
        self.assertNotEqual(LanguageOption("", default="es").code, "")

    def test_missing_env_var_uses_default(self):
        self.assertEqual(LanguageOption.from_env({}, default="es").code, "es")

    def test_empty_env_var_uses_default(self):
        option = LanguageOption.from_env({"TRANSLATOR_LANGUAGE": ""}, default="es")
        self.assertEqual(option.code, "es")


class AutoMeansDetectTests(unittest.TestCase):
    """`auto` is how a user asks Whisper to detect, which the API spells None."""

    def test_auto_resolves_to_none(self):
        for raw in ["auto", "AUTO", " Auto "]:
            with self.subTest(raw=raw):
                self.assertIsNone(LanguageOption(raw).code)

    def test_auto_is_not_an_error(self):
        self.assertTrue(LanguageOption("auto").is_auto)


class ValidCodePassesThroughTests(unittest.TestCase):

    def test_known_code_is_returned_unchanged(self):
        for raw in ["en", "es", "ja", "yue"]:
            with self.subTest(raw=raw):
                self.assertEqual(LanguageOption(raw).code, raw)

    def test_surrounding_space_and_case_are_normalized(self):
        for raw in [" en ", "EN", "Es"]:
            with self.subTest(raw=raw):
                self.assertEqual(LanguageOption(raw).code, raw.strip().lower())


class UnknownCodeIsRejectedTests(unittest.TestCase):

    def test_unknown_code_raises(self):
        with self.assertRaises(ValueError):
            LanguageOption("klingon")

    def test_message_names_the_offending_value(self):
        with self.assertRaisesRegex(ValueError, "klingon"):
            LanguageOption("klingon")

    def test_message_points_at_the_accepted_list(self):
        with self.assertRaisesRegex(ValueError, r"\ben\b"):
            LanguageOption("klingon")

    def test_a_default_that_is_itself_invalid_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "nope"):
            LanguageOption("", default="nope")


if __name__ == "__main__":
    unittest.main()
