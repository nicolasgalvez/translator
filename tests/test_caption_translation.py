"""Caption translation policy and dispatch behavior."""

import builtins
import importlib
import sys
from types import SimpleNamespace

import pytest


def make_runtime():
    """Construct the real runtime without starting hardware or model resources."""
    module = importlib.import_module("translator_runtime")
    return module, module.TranslatorRuntime(module.RuntimeConfig.from_environment({}))


@pytest.mark.parametrize("source, target", [("en", "es"), ("es", "en")])
def test_caption_translation_policy_routes_supported_languages(source, target):
    module = importlib.import_module("translator_runtime")

    policy = module.CaptionTranslationPolicy()

    assert policy.route_for(source) == (source, target)


def test_caption_translation_policy_reports_every_unsupported_language():
    module = importlib.import_module("translator_runtime")
    policy = module.CaptionTranslationPolicy()

    with pytest.raises(ValueError) as raised:
        policy.validate_segments([
            {"language": "fr"}, {"language": "en"},
            {"language": "de"}, {"language": "fr"},
        ])

    message = str(raised.value)
    assert "de, fr" in message
    assert "en, es" in message


def test_caption_package_installation_uses_the_policy_routes(monkeypatch):
    _module, runtime = make_runtime()
    installed = []

    class Package:  # pylint: disable=too-few-public-methods
        def __init__(self, from_code, to_code):
            self.from_code = from_code
            self.to_code = to_code

        def install(self):
            installed.append((self.from_code, self.to_code))

    packages = SimpleNamespace(
        get_installed_packages=lambda: [],
        update_package_index=lambda: None,
        get_available_packages=lambda: [Package("en", "es"), Package("es", "en")],
    )
    monkeypatch.setitem(sys.modules, "argostranslate", SimpleNamespace(package=packages))
    monkeypatch.setitem(sys.modules, "argostranslate.package", packages)

    runtime.ensure_argos_packages({})

    assert installed == [("en", "es"), ("es", "en")]
    assert installed == list(runtime.caption_translation_policy.routes)


def test_caption_translation_dispatches_a_supported_language_mix(monkeypatch):
    _module, runtime = make_runtime()
    calls = []

    def translate(text, source, target):
        calls.append((text, source, target))
        return {"Hello": "Hola", "mundo": "world"}[text]

    translation = SimpleNamespace(translate=translate)
    monkeypatch.setitem(sys.modules, "argostranslate", SimpleNamespace(translate=translation))
    monkeypatch.setitem(sys.modules, "argostranslate.translate", translation)

    original, translated = runtime.build_srt_entries([
        {"start": 0, "end": 1, "text": "Hello", "language": "en"},
        {"start": 1, "end": 2, "text": "mundo", "language": "es"},
    ], {})

    assert original == [
        {"start": 0, "end": 1, "text": "Hello"},
        {"start": 1, "end": 2, "text": "mundo"},
    ]
    assert translated == [
        {"start": 0, "end": 1, "text": "Hola"},
        {"start": 1, "end": 2, "text": "world"},
    ]
    assert calls == [("Hello", "en", "es"), ("mundo", "es", "en")]


def test_caption_translation_rejects_unsupported_segments_before_argos(monkeypatch):
    _module, runtime = make_runtime()
    original_import = builtins.__import__
    argos_imported = False

    def reject_argos(name, *args, **kwargs):
        nonlocal argos_imported
        if name.startswith("argostranslate"):
            argos_imported = True
            raise AssertionError("Argos must not be loaded for unsupported languages")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_argos)

    with pytest.raises(ValueError, match="fr"):
        runtime.build_srt_entries([
            {"start": 0, "end": 1, "text": "bonjour", "language": "fr"},
        ], {})

    assert not argos_imported
