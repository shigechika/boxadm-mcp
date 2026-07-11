# Changelog

## [0.4.0](https://github.com/shigechika/boxadm-mcp/compare/v0.3.7...v0.4.0) (2026-07-11)


### Features

* scan-level wall-clock deadline + per-request timeout knob ([#15](https://github.com/shigechika/boxadm-mcp/issues/15)) ([bf0cca8](https://github.com/shigechika/boxadm-mcp/commit/bf0cca808ed1c12fd5e5aeb4845e25d8a93e9039))

## [0.3.7](https://github.com/shigechika/boxadm-mcp/compare/v0.3.6...v0.3.7) (2026-07-11)


### Bug Fixes

* retry 429/transient errors with backoff in the read path ([#12](https://github.com/shigechika/boxadm-mcp/issues/12)) ([7c4f2d8](https://github.com/shigechika/boxadm-mcp/commit/7c4f2d824a8b6697149527a3abe98b9c65ff3ace))

## [0.3.6](https://github.com/shigechika/boxadm-mcp/compare/v0.3.5...v0.3.6) (2026-07-10)


### Performance

* parallelize per-folder collaboration lookups in _scan() ([#9](https://github.com/shigechika/boxadm-mcp/issues/9)) ([5922581](https://github.com/shigechika/boxadm-mcp/commit/5922581f8a7c06dd492693327761c421ad9482d4))

## [0.3.5](https://github.com/shigechika/boxadm-mcp/compare/v0.3.4...v0.3.5) (2026-07-09)


### Bug Fixes

* use is_externally_owned flag instead of owner-domain heuristic ([#5](https://github.com/shigechika/boxadm-mcp/issues/5)) ([0235f8f](https://github.com/shigechika/boxadm-mcp/commit/0235f8f84bd3bb2886c5745b69fb9d6b9cb7255d))

## [0.3.4](https://github.com/shigechika/boxadm-mcp/compare/v0.3.3...v0.3.4) (2026-07-09)


### Bug Fixes

* skip externally-owned folders in external-collaborator scan ([#3](https://github.com/shigechika/boxadm-mcp/issues/3)) ([5b3d37e](https://github.com/shigechika/boxadm-mcp/commit/5b3d37eced6253d626e4a2c2368cc83d3b2d3c68))

## [0.3.3](https://github.com/shigechika/boxadm-mcp/compare/v0.3.2...v0.3.3) (2026-07-08)


### Bug Fixes

* translate the OAuth callback page to English ([f0f9ded](https://github.com/shigechika/boxadm-mcp/commit/f0f9deda046b1658cd2e0fce57f1784e8c43a5d6))

## [0.3.2](https://github.com/shigechika/boxadm-mcp/compare/v0.3.1...v0.3.2) (2026-07-08)


### Bug Fixes

* catch asyncio.CancelledError on ^C, not just KeyboardInterrupt ([102e901](https://github.com/shigechika/boxadm-mcp/commit/102e9011b52009cf5fa924f5f252f319ef4c1ee4))
* skip the SIGINT test on Windows (signal semantics differ) ([124aab7](https://github.com/shigechika/boxadm-mcp/commit/124aab70a893a197cff32e05ae2848bb3786264f))

## Changelog
