# Changelog

## [0.2.4](https://github.com/nicolasgalvez/translator/compare/v0.2.3...v0.2.4) (2026-09-10)


### Bug Fixes

* admit caption uploads before parsing ([c9a20a9](https://github.com/nicolasgalvez/translator/commit/c9a20a98a4a01e55430c3f39206d42db53b27c9e))
* align documented Python requirement ([#47](https://github.com/nicolasgalvez/translator/issues/47)) ([57385eb](https://github.com/nicolasgalvez/translator/commit/57385eb5caf0bb0c3c4679813575fd5034da7488))
* align the Node.js runtime contract ([ceb456e](https://github.com/nicolasgalvez/translator/commit/ceb456ea174316f624ee783ecba4351a8c9f5d7b))
* bound caption job concurrency and retention ([#43](https://github.com/nicolasgalvez/translator/issues/43)) ([03ab803](https://github.com/nicolasgalvez/translator/commit/03ab803c4188d4c3266d4a864abd3cf0a419e55f))
* bound decoded caption audio ([d7adad2](https://github.com/nicolasgalvez/translator/commit/d7adad22fd7fc77d9c578f054414d5d020b3950a))
* bound live audio buffering and transcription backpressure ([#42](https://github.com/nicolasgalvez/translator/issues/42)) ([fe13d78](https://github.com/nicolasgalvez/translator/commit/fe13d7838021ad40af040b8ff8daa71e40844103))
* bound transcript history rendering ([2f48db4](https://github.com/nicolasgalvez/translator/commit/2f48db40b80a205f0bbc1a2c832664452a41d6e3))
* **ci:** resolve the required merge ref ([9edbf4d](https://github.com/nicolasgalvez/translator/commit/9edbf4db3e10a01f5e963b218847b2d09e645248))
* **ci:** use the native required check ([47f9c7f](https://github.com/nicolasgalvez/translator/commit/47f9c7f659e66471507efdb9a019d4e337f7dcfb))
* **ci:** validate release PRs with required checks ([c0f8caf](https://github.com/nicolasgalvez/translator/commit/c0f8caf1fa71020d93e7a830d412dfc7001ec690))
* enforce Docker runtime contracts ([05caa10](https://github.com/nicolasgalvez/translator/commit/05caa10ee9c279c369ab1f2b043e41603fdc7b13))
* exclude secrets and runtime media from Docker context ([#48](https://github.com/nicolasgalvez/translator/issues/48)) ([edeb87d](https://github.com/nicolasgalvez/translator/commit/edeb87da6d7e3d3e58e30797b0b1d962434492b8))
* isolate websocket delivery ([ca7ad3e](https://github.com/nicolasgalvez/translator/commit/ca7ad3e0e2c4d240efcc4cfb3fb90269d9495a6c))
* make caption controls accessible ([#50](https://github.com/nicolasgalvez/translator/issues/50)) ([bafe2fd](https://github.com/nicolasgalvez/translator/commit/bafe2fd01df9656973a0bf803491bc7607ed1ba1))
* make Docker runtime contract executable ([9f09693](https://github.com/nicolasgalvez/translator/commit/9f096932d902d610b5646d6625f0b37a2c623555))
* make Python lint paths shell-safe ([3611283](https://github.com/nicolasgalvez/translator/commit/3611283fcd648c377fe53a663f708b791fddeaff))
* reject unsupported caption languages ([#49](https://github.com/nicolasgalvez/translator/issues/49)) ([9ec6c14](https://github.com/nicolasgalvez/translator/commit/9ec6c14c01e83f8d116b2391cedb30f1384394fe))
* reject untrusted transcript WebSocket origins ([#41](https://github.com/nicolasgalvez/translator/issues/41)) ([0bc4439](https://github.com/nicolasgalvez/translator/commit/0bc4439e21ed4a99bbbf79d8e8aa704893fad00a))
* remediate frontend dependency vulnerabilities ([#46](https://github.com/nicolasgalvez/translator/issues/46)) ([a1ea78a](https://github.com/nicolasgalvez/translator/commit/a1ea78a684db84cefee7ec6eb0ca8d269895c68c))
* replace vulnerable Stanza dependency ([af72acb](https://github.com/nicolasgalvez/translator/commit/af72acbfed5cbf7801251c5cf81ea4d90fe20d07))
* reserve unique live session files ([f7dd98f](https://github.com/nicolasgalvez/translator/commit/f7dd98fb1e4ecad5e8c7df7147980d8eb33ee6ad))
* rotate long session recordings ([8afd36d](https://github.com/nicolasgalvez/translator/commit/8afd36d703a156d6a04725bec223ce041368a5a6))
* stop failed audio capture loops ([#51](https://github.com/nicolasgalvez/translator/issues/51)) ([cc6407d](https://github.com/nicolasgalvez/translator/commit/cc6407d4fd7afeb318d2ed3b19c626ca242dd779))
* stop failed caption status polling ([b465a87](https://github.com/nicolasgalvez/translator/commit/b465a871ef2e1d4590ffe1effe7083e87b4e7659))
* store caption uploads safely and reject oversized files ([#39](https://github.com/nicolasgalvez/translator/issues/39)) ([78278b2](https://github.com/nicolasgalvez/translator/commit/78278b2d8888f52a6c00724d181e1e801dcebcca))
* support default and mono audio inputs ([#44](https://github.com/nicolasgalvez/translator/issues/44)) ([57ac8bf](https://github.com/nicolasgalvez/translator/commit/57ac8bf25ba77c559186629ffcb0a539209dc675))
* tolerate damaged transcript history ([c36b1a1](https://github.com/nicolasgalvez/translator/commit/c36b1a10591b33dcf9df8e692084fc2779c0cfa3))
* update Starlette template responses ([#75](https://github.com/nicolasgalvez/translator/issues/75)) ([0d09145](https://github.com/nicolasgalvez/translator/commit/0d09145755ff2403d590d6fba0fe22e5fb65f57c))
* upgrade the CUDA Torch runtime ([80fc1e8](https://github.com/nicolasgalvez/translator/commit/80fc1e882f63654262ec7fa842ebeb24d607e50e))
* validate extracted caption audio ([299d5bf](https://github.com/nicolasgalvez/translator/commit/299d5bf6ac01e704076e2b7f3237d6fc7fd6cf9d))
* validate launcher options before setup ([fb07daf](https://github.com/nicolasgalvez/translator/commit/fb07daf4d98b24bc1fdc5a6e5490801f00c73c58))

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
