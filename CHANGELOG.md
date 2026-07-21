# Changelog

All notable changes to OpenCollab are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project aims to follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- Replaced the request-heavy SDK v2 surface with the compact `OpenCollab` facade:
  `agent`, `team`, and `workflow` now share one `RunResult`, central configuration,
  and bootstrap-owned lifecycle wiring.
- Reduced the everyday Python API to four root exports and moved optional tool,
  environment, and workflow-authoring contracts into small capability modules.
- Adopted the Mulan Permissive Software License v2 (`MulanPSL-2.0`) for OpenCollab.
- Flattened the Python project layout so the repository root owns build metadata, tests, and the canonical license
  while public `opencollab.*` import paths remain unchanged.
- Prepared the public SDK distribution for release with a package-version consistency gate, typed-package metadata,
  bundled license text, and a minimal integration example.
- Moved benchmark runtime configuration to OpenCollab-Eval and historical experiment records to the external documentation workspace.
- Added versioned SDK capability modules for external runtimes while keeping benchmark policy outside the framework package.
- Raised the package version to 0.4.0 for the incompatible compact SDK boundary.

### Removed
- Removed the SDK v2 request/result DTO graph, its independent API-version integer,
  and the obsolete `sdk.models`, `sdk.runtime`, `sdk.environment`, `sdk.errors`,
  `sdk.tools`, `sdk.usage`, and `sdk.workflows` modules.

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
