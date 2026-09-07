# Changelog

## [0.2.0](https://github.com/nicolasgalvez/translator/compare/v0.1.0...v0.2.0) (2026-09-07)


### Features

* add --host flag and default to localhost-only binding ([951ea80](https://github.com/nicolasgalvez/translator/commit/951ea8013382d18d0298974cb1dd11fef418750b))
* add React transcriber frontend with plugin registry ([15e890d](https://github.com/nicolasgalvez/translator/commit/15e890d4ca5622f53fcd68a77187767c63c211a5))
* add WordPress-style hook system and plugin loader ([9041351](https://github.com/nicolasgalvez/translator/commit/9041351fcbb056074b599197a6d3791798145d3a))


### Bug Fixes

* **ci:** let a stuck release be retried on demand ([065212e](https://github.com/nicolasgalvez/translator/commit/065212eb4fc1812df1bb246f445fae01b3ec856f))
* **ci:** put the repo root on sys.path for pytest ([26c3fcc](https://github.com/nicolasgalvez/translator/commit/26c3fccc44f553ddefcaf8c72c4ec44f7e1df6cf))
* cut at quietest spot instead of mid-word at MAX_UTTERANCE ([35b6f37](https://github.com/nicolasgalvez/translator/commit/35b6f37a80686a9f4f828adb76eda0810b4b6c65))
* reject an invalid language at startup instead of every utterance ([9df418b](https://github.com/nicolasgalvez/translator/commit/9df418b84be6203f7252f170c412c092a47093d2))
