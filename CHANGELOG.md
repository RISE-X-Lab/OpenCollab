# Changelog

All notable changes to OpenCollab are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project aims to follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.5.1] - 2026-09-05

### Fixed
- Normalized the whitespace-padded byte count emitted by BSD/macOS `wc -c`,
  so verified Docker container writes use the same cross-platform contract as
  GNU `wc`.

## [0.5.0] - 2026-08-13

### Added
- Added Mini Edict, a bilingual, tested Three Departments and Six Ministries
  example with both a team configuration and a hard-gated workflow.
- Added a public maintainer release procedure covering exact-SHA validation,
  artifact construction, signed tagging, publication, and failure handling.
- Added explicit lifecycle exceptions for rejected concurrent turns,
  duplicate live spawns, scheduler turn failures/stalls, and unavailable
  isolated snapshots. See the 0.5.0 migration guide for import paths and
  recovery actions.
- Added `task_concurrency` to workflow runs and `cleanup_timeout` to team runs
  so callers can bound non-agent workflow units and scheduler shutdown
  independently.
- Added a native OpenAI Responses API transport with typed streaming, encrypted
  reasoning replay, exact function-call identity, structured-output projection,
  provider usage accounting, and a shared provider-failure retry budget.

### Changed
- Replaced changelog links that depended on the absent remote `v0.1.0` tag with
  exact historical commit links.
- Scheduler and session boundaries now preserve terminal failures, ownership,
  budgets, deadlines, and snapshot isolation instead of silently treating
  partial work as success.
- Configuration and team schemas reject unknown keys, API-key fallback is
  provider/endpoint specific, watchdog/low-yield wind-down latches reset per
  user turn while the hard budget and its protected reserve remain
  session-lifetime (with an allocation-time autosave), and useful partial
  compaction is retained.
- DeepSeek V4 Flash model aliases now use the full 1,048,576-token context
  window, and workflow calls can bind the supported `max` reasoning effort.

### Fixed
- Capped structured-output corrective retries at 60 seconds while preserving
  shorter caller deadlines, so endpoints that degrade forced tool choice cannot
  consume the caller's full role budget.
- Honored native Anthropic manual and adaptive thinking settings, including
  provider-compatible sampling, tool selection, and signed thinking continuity.
- Prevented truncated provider output, stale restored turns, unbounded teammate
  delivery, deferred-tool contract bypasses, and reviewer PASS results from
  masking failed or incomplete work.
- Preserved Responses reasoning and function-call state across stateless tool
  rounds while rejecting incomplete streams, mismatched terminal output, and
  malformed or orphaned call identities.

## [0.4.1] - 2026-07-31

### Added
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

[Unreleased]: https://github.com/RISE-X-Lab/OpenCollab/compare/v0.5.1...HEAD
[0.5.1]: https://github.com/RISE-X-Lab/OpenCollab/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/RISE-X-Lab/OpenCollab/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/RISE-X-Lab/OpenCollab/compare/563027175e2cc2540d19324def73010a7e436dcc...v0.4.1
[0.1.0]: https://github.com/RISE-X-Lab/OpenCollab/tree/563027175e2cc2540d19324def73010a7e436dcc
