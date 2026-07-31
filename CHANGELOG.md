# Changelog

## [0.5.3](https://github.com/shigechika/boxadm-mcp/compare/v0.5.2...v0.5.3) (2026-07-31)


### Bug Fixes

* **deps:** cap the MCP SDK below v2 ([#35](https://github.com/shigechika/boxadm-mcp/issues/35)) ([912c473](https://github.com/shigechika/boxadm-mcp/commit/912c473a51ad60933646fc41071539638ed7c8af))

## [0.5.2](https://github.com/shigechika/boxadm-mcp/compare/v0.5.1...v0.5.2) (2026-07-27)


### Bug Fixes

* **ci:** read AI-review guidance from the base revision, drop the checkout ([#33](https://github.com/shigechika/boxadm-mcp/issues/33)) ([e3ff584](https://github.com/shigechika/boxadm-mcp/commit/e3ff584187aeb3cba90106885528f4824823e25e))
* report the auth mode in effect, not the configured string ([#32](https://github.com/shigechika/boxadm-mcp/issues/32)) ([33f3e0f](https://github.com/shigechika/boxadm-mcp/commit/33f3e0f0e34784447e8359942d30dafaea4fa85d))
* sync the smoke-test engine ([#30](https://github.com/shigechika/boxadm-mcp/issues/30)) ([993cf89](https://github.com/shigechika/boxadm-mcp/commit/993cf899161de4ab6998e8611b2cb38c1f4f792f))

## [0.5.1](https://github.com/shigechika/boxadm-mcp/compare/v0.5.0...v0.5.1) (2026-07-26)


### Bug Fixes

* separate "nothing to report" from "could not look" ([#27](https://github.com/shigechika/boxadm-mcp/issues/27)) ([04a9df6](https://github.com/shigechika/boxadm-mcp/commit/04a9df6a1890f29843f4feeb9f756c03600c9ade))

## [0.5.0](https://github.com/shigechika/boxadm-mcp/compare/v0.4.0...v0.5.0) (2026-07-26)


### Features

* live smoke test that exercises every registered tool ([#25](https://github.com/shigechika/boxadm-mcp/issues/25)) ([3230f80](https://github.com/shigechika/boxadm-mcp/commit/3230f80bc613b605547323c5bbdf3cb816b066a8))

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
