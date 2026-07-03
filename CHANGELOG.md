# Changelog

All notable changes to OpenCollab are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project aims to follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-07-03

First public release of OpenCollab — run LLM coding agents three ways (a single
interactive agent, an autonomous team, or a deterministic workflow) behind a
clean architecture where everything but the model sits behind swappable ports.

### Added
- MIT `LICENSE`; `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`.
- GitHub Actions CI (ruff + pytest across Python 3.10–3.12); issue and PR templates.
- `pyproject.toml` metadata (authors, project URLs, classifiers, keywords); README status badges; `.editorconfig`; this changelog.

### Fixed
- Guarded-workflow patch allowlist failed *open* when handed an empty allowlist; it now strips every path (fail-closed) as intended.
- Cross-loop async test harness and a stale LLM-usage fake (test suite: 999 → 1009 passing).
- Hook-timeout process reap could hang on Python 3.11+; the reap is now bounded so a timed-out hook cannot stall the caller.

### Changed
- Trimmed the GLM SWE-bench experiment archive to the final report and prediction files.
- Moved Chinese working notes into `docs/archive/`.

[Unreleased]: https://github.com/YihongDong/OpenCollab/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/YihongDong/OpenCollab/releases/tag/v0.1.0
