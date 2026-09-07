# Changelog

## [0.2.3](https://github.com/nicolasgalvez/translator/compare/v0.2.2...v0.2.3) (2026-09-07)


### Bug Fixes

* **ci:** refresh uv.lock on the release branch ([ecd97ff](https://github.com/nicolasgalvez/translator/commit/ecd97ffa17c351cab4d50f11f0cbd6fde6ce5f71))
* sync the pyproject version with version.txt ([d855133](https://github.com/nicolasgalvez/translator/commit/d85513330f084ae09530c3e95d282ac60757c270))

## [0.2.2](https://github.com/nicolasgalvez/translator/compare/v0.2.1...v0.2.2) (2026-09-07)


### Bug Fixes

* **ci:** transition only the ticket the branch names ([564b5fe](https://github.com/nicolasgalvez/translator/commit/564b5fefc3df29d381ccbc83029ac059db77ef70))

## [0.2.1](https://github.com/nicolasgalvez/translator/compare/v0.2.0...v0.2.1) (2026-09-07)


### Bug Fixes

* replace the deprecated on_event startup hook with a lifespan handler ([f47ff71](https://github.com/nicolasgalvez/translator/commit/f47ff716be05367e59e3a4616c0cd21ecbcae357))


### Performance Improvements

* skip the frontend build when the bundle is up to date ([875785f](https://github.com/nicolasgalvez/translator/commit/875785fb7bf285c31b43865bc030d91794a9d638))

## [0.2.0](https://github.com/nicolasgalvez/translator/compare/v0.1.0...v0.2.0) (2026-09-07)


### Features

* add --host flag and default to localhost-only binding ([951ea80](https://github.com/nicolasgalvez/translator/commit/951ea8013382d18d0298974cb1dd11fef418750b))
* add React transcriber frontend with plugin registry ([aca6c02](https://github.com/nicolasgalvez/translator/commit/aca6c023df85851bbc0f7f343f8765c7af6242ed))
* add WordPress-style hook system and plugin loader ([752a0ca](https://github.com/nicolasgalvez/translator/commit/752a0ca78e3f0e5e974d856a1757aa5f7aac726d))


### Bug Fixes

* **ci:** let a stuck release be retried on demand ([dc6871d](https://github.com/nicolasgalvez/translator/commit/dc6871d062e4e3593f7beee444bc283ebba60422))
* **ci:** put the repo root on sys.path for pytest ([a547300](https://github.com/nicolasgalvez/translator/commit/a547300530116131cbbc2a7b6c9198e434d7416d))
* cut at quietest spot instead of mid-word at MAX_UTTERANCE ([35b6f37](https://github.com/nicolasgalvez/translator/commit/35b6f37a80686a9f4f828adb76eda0810b4b6c65))
* reject an invalid language at startup instead of every utterance ([3f1f636](https://github.com/nicolasgalvez/translator/commit/3f1f63640c9ccc3a44f4b52beaa4eba18511775a))
