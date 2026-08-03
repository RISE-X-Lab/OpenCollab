# Changelog

All notable changes to OpenCollab are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project aims to follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.4.1] - 2026-08-03

### Added
- Added an explicit OpenAI Responses wire protocol with typed streaming,
  locally replayed reasoning and function items, exact tool-call binding,
  configurable reasoning effort, separate stream timeouts, and complete usage
  evidence.
- Added Mini Edict, a bilingual, tested Three Departments and Six Ministries
  example with both a team configuration and a hard-gated workflow.
- Added immutable non-secret effective configuration metadata, public stateless
  tool composition, and caller-owned Local, Worktree, and image-backed Docker
  environment factories for external integrations. Configuration metadata
  includes deep-copied thinking parameters and a SHA-256 endpoint fingerprint
  without exposing the base URL.
- Expanded the public workflow-authoring contract with the supported agent
  controls, draft findings, working-tree diff access, and live token
  observation.
- Added public agent and workflow step limits, cleanup deadlines, workflow
  system prompts, aggregate session metrics, and sanitized child-agent failure
  summaries.
- Added the narrow `VerificationTool` contract for reading parser-verified test
  targets without importing a concrete test adapter.

### Changed
- Team configuration is now explicit: the CLI and SDK use the built-in
  lead-only team unless a file is selected with `--team-config`, `config=`, or
  `OPENCOLLAB_TEAM_FILE`; conventional filenames are no longer auto-discovered.
- Replaced the request-heavy SDK v2 surface with the compact `OpenCollab` facade:
  `agent`, `team`, and `workflow` now share one `RunResult`, central configuration,
  and bootstrap-owned lifecycle wiring.
- Workflow budget stops now use an explicit runtime reason rather than
  interpreting caller output, and timeout or execution failures retain finalized
  session metrics when lifecycle evidence is complete.
- Reduced the everyday Python API to four root exports and moved optional tool,
  environment, and workflow-authoring contracts into small capability modules.
- Adopted the Mulan Permissive Software License v2 (`MulanPSL-2.0`) for OpenCollab.
- Flattened the Python project layout so the repository root owns build metadata, tests, and the canonical license
  while public `opencollab.*` import paths remain unchanged.
- Prepared the public SDK distribution for release with a package-version
  consistency gate, standards-based license metadata, bundled notices, and a
  minimal integration example.
- Moved benchmark datasets, adapters, runners, and reports out of the framework
  package. External evaluation packages now compose the compact SDK and Clean
  Architecture ports, while reusable evidence-capture primitives remain part of
  workflow authoring.
- Raised the package version to 0.4.1 for the incompatible compact SDK boundary.

### Fixed
- Capped structured-output corrective retries at 60 seconds while preserving
  shorter caller deadlines, so endpoints that degrade forced tool choice cannot
  consume the caller's full role budget.
- Restored the exact terminal state after turn-scoped TUI keyboard navigation on
  macOS and added the terminal probe to macOS CI.
- Emitted valid JavaScript comments around the vendored js-yaml license in Team
  Config HTML blueprints.

### Removed
- Removed the SDK v2 request/result DTO graph, its independent API-version integer,
  and the obsolete `sdk.models`, `sdk.runtime`, `sdk.environment`, `sdk.errors`,
  `sdk.tools`, `sdk.usage`, and `sdk.workflows` modules.

## [0.1.0] - 2026-07-03

Initial tagged version of OpenCollab — run LLM coding agents three ways (a single
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

[Unreleased]: https://github.com/RISE-X-Lab/OpenCollab/compare/v0.4.1...HEAD
[0.4.1]: https://github.com/RISE-X-Lab/OpenCollab/compare/563027175e2cc2540d19324def73010a7e436dcc...v0.4.1
[0.1.0]: https://github.com/RISE-X-Lab/OpenCollab/tree/563027175e2cc2540d19324def73010a7e436dcc
