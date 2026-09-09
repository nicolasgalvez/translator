"""Compatibility coverage for the locked Argos Translate and Stanza packages."""

import importlib.metadata
import json
import tomllib
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import argostranslate
import stanza
from argostranslate import sbd, translate


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MINIMUM_SAFE_STANZA = (1, 12, 2)


def locked_version(package_name):
    with (REPOSITORY_ROOT / "uv.lock").open("rb") as lock_file:
        lock = tomllib.load(lock_file)

    versions = {
        package["version"]
        for package in lock["package"]
        if package["name"] == package_name
    }
    assert len(versions) == 1
    return versions.pop()


def test_locked_argos_uses_real_safe_stanza_sentence_boundaries(monkeypatch, tmp_path):
    assert Path(argostranslate.__file__).resolve().is_relative_to(
        Path(stanza.__file__).resolve().parents[2]
    )
    assert importlib.metadata.version("argostranslate") == locked_version(
        "argostranslate"
    )
    stanza_version = importlib.metadata.version("stanza")
    assert stanza_version == locked_version("stanza")
    assert tuple(map(int, stanza_version.split("."))) >= MINIMUM_SAFE_STANZA

    stanza_directory = tmp_path / "stanza"
    stanza_directory.mkdir()
    resources = {
        "en": {
            "lang_name": "English",
            "packages": {},
            "tokenize": {"default": {"md5": "unused"}},
        }
    }
    (stanza_directory / "resources.json").write_text(
        json.dumps(resources), encoding="utf-8"
    )

    real_pipeline = stanza.Pipeline
    monkeypatch.setattr(
        stanza,
        "Pipeline",
        partial(
            real_pipeline,
            download_method=None,
            tokenize_pretokenized=True,
        ),
    )

    package = SimpleNamespace(
        from_code="en",
        package_path=tmp_path,
        target_prefix="",
        tokenizer=SimpleNamespace(
            encode=str.split,
            decode=" ".join,
        ),
    )
    sentencizer = sbd.StanzaSentencizer(package)
    translated_batches = []

    def translate_batch(tokenized, **_options):
        translated_batches.extend(tokenized)
        return [
            SimpleNamespace(
                hypotheses=[[token.upper() for token in sentence]],
                scores=[-0.1],
            )
            for sentence in tokenized
        ]

    hypotheses = translate.apply_packaged_translation(
        package,
        "First sentence.\nSecond sentence.",
        SimpleNamespace(translate_batch=translate_batch),
        sentencizer,
        num_hypotheses=1,
    )

    assert isinstance(sentencizer.stanza_pipeline, real_pipeline)
    assert translated_batches == [["First", "sentence."], ["Second", "sentence."]]
    assert hypotheses[0].value == "FIRST SENTENCE. SECOND SENTENCE."
