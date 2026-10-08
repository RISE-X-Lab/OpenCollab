# Changelog

All notable changes to OpenCollab are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project aims to follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.9.3] - 2026-10-08

This release is the v1.0.0 PreRelease, published as package version 0.9.3.

### Fixed

Failed patch capture preserves local and container worktrees until their changes
have been exported successfully. Failed or stopped team members deliver their
usable edits to the parent task, including prebuilt teammates. Larger changes
remain available in their original workspace with a recovery location.

Git worktree delivery retains coder changes through rebase, pull-triggered
rebase, cherry-pick, conflict recovery, abort, and resets within the coder's own
history. Configured `git pull` and explicit `git pull --rebase` follow the same
history handling, including commit subjects that contain rebase action markers.

Explicitly disabled tools remain disabled through Responses capability handling
and parameter-error retries. Anthropic input-budget estimates count the native
request content once while retaining system instructions, thinking signatures,
and tool data.

### Changed

Anthropic SDK support includes both 0.x and 1.x, with explicit sampling options
forwarded through the supported interface for each SDK generation.

Benchmark figures and result tables include OpenHands and the latest adopted
Duo results for SWE-bench Pro, Terminal-Bench 2.1, and DeepSWE. The three-chart
view uses a shared 40–85 percent scale.

## [0.9.2] - 2026-10-06

### Added

The CLI can create a team configuration from its built-in template and accepts
plain task text for workflow runs. Interactive help links the default agent,
Team, and Duo entry points with matching examples.

### Fixed

Candidate patches use a consistent machine-readable Git format across display
settings and directory-copy worktrees. Each candidate owns its verification
records. Source inspection failures retain recoverable candidate workspaces,
including cancellation followed by a connection-close error. Submodule edits
that a repository patch cannot deliver remain in the preserved workspace.
Duo's own evidence directory is excluded from its source-change comparison.

Cancellation keeps native file writes and late provider work owned until their
effects settle. Workflow children share task capacity and the single extra
budget allowance. Restored turns preserve accepted teammate messages through
targeted cancellation, and dynamic teams retain reservations for calls still
reporting usage during cleanup.

Responses and Chat retries retain already-reported usage, while context sizing
uses the final request's input. Native Responses replay participates in request
estimates. Non-streaming reasoning aliases participate in response parsing and
missing-usage estimates. Structured output limits keep their termination state
and returned usage. Anthropic thinking budgets and dated model aliases follow
the available output allowance and model capabilities.

Context compaction keeps forced state local to each call and falls back to a
raw excerpt for an empty summary. Session journal append failures roll back
before retrying. Run identifiers remain consistent across snapshots, manifests
and traces, and concurrent trace sequence numbers follow queue order. Large
JSON integers retain integer validation semantics.

File pagination accounts for displayed line numbers and continues at the first
unread line. Git tools report truncated or failed audits explicitly. The CLI
rejects interactive options placed before a workflow subcommand and explains
the supported option position before discovering or executing a workflow.

Source executable-bit changes reach candidate workspaces even when Git's
filemode setting ignores them. File display, pagination, and editing use the
same physical line coordinates, and container edits reject lossy decoding.
Workflow entry modules can import a sibling named `workflow.py`. Cancelling an
equal-sized queued budget request returns its own reservation.

Complete streaming tool calls with malformed arguments reach normal tool
validation so the model can correct them. Anthropic end turns honor required
tool checks. Legacy interrupted snapshots restore paired tool results. SDK
clients retain the provider endpoint resolved at construction. Serial teams
yield execution while awaiting coder review, then resume with correct file
ownership and cancellation cleanup. Team timeouts are classified consistently
on Python 3.10 and later.

### Documentation

Current guides describe the runtime's configuration precedence, cancellation,
usage and candidate behavior. Developer and release instructions match the
existing CI checks, while historical designs and migration guides link to the
current entry points. Skill descriptions distinguish truncation from body-size
rejection, and the sequence-diagram skill's YAML metadata loads correctly.

## [0.9.1] - 2026-10-05

### Added

Team files can declare independent token budgets and context policies for each
role. Public team inspection exposes these settings, and initial topology
records retain each seat's model declaration. The team configuration editor
preserves role identities, budgets, context settings, and feedback across edits
and exports.

### Changed

Every SDK run has a unique run id. A single agent and a workflow used to stamp
their records with the agent's or the workflow's name, so all runs of one
setting shared it; they now generate `agent-<uuid>` and `workflow-<uuid>` as a
team already generated `team-<uuid>`. The id is on every trajectory record, in
`result.metrics["run_id"]`, and in `workflow.json` or `team.json`. `agent`,
`team` and `workflow` accept `run_id=` so a harness can name the run and join
its own records to the run's on that id.

Duo V8 asks coders to verify the final deliverable through the task's intended
entry points or outputs, check directly affected existing behavior, and rerun
focused checks after the last relevant edit or cleanup. Shared task-oriented
instructions distinguish unchanged project checks from candidate-added or
modified checks. Results report `prompt_revision=8` for both submission modes.

### Fixed

Workflow role failures retain bounded, message-free exception chains, and sticky
trajectory write failures preserve their original OS error code across the public
API. Evaluation callers can distinguish storage exhaustion from a failed solver
without parsing private error messages.

Single2 follows the task's permissions and delivery requirements instead of
assuming every task is a sealed code-repair evaluation. Task-required
configuration changes and service delivery coexist with existing-test
protection. Explicit caller-owned working-tree capture continues to delegate
commit and submission steps to the caller.

Candidate workspaces preserve committed and uncommitted source content,
binary patches, executable permissions, repository subdirectories, and locally
initialized submodules. Capture and adoption detect source revisions that
change during execution and retain concurrent source edits. Temporary capture
material stays in the repository's private Git storage. Candidate workflows
share the configured model concurrency allowance and return resources after
cancellation or initialization failures.

Session persistence preserves accepted submissions and waiting-turn state,
orders manual and automatic saves, and retains message rewrites. Disk writes
run without blocking the event loop. Summary requests support cancellation
and contribute to session budgets, usage, and request tracing. Reusing an
agent keeps each session's tools and submission results independent.

Provider requests preserve developer message roles, connection timeouts,
default output limits, and sampling options supported by the active reasoning
configuration. Response handling retains refusals, tool calls paired with
empty text, thinking usage, retryable failures, and output-limit termination.
Each provider uses its SDK's native timeout type, including when the installed
OpenAI and Anthropic SDKs expose different timeout implementations.
An empty Anthropic end turn receives one bounded continuation. Trace ownership
and structured failure details survive caller-supplied runtimes.

Duo excludes historical tests invalidated by later edits or uncertain shell
execution from mechanical candidate comparison and retains their evidence for
adjudication. File tools preserve raw Git paths, mathematical integer arguments,
patch line endings, and existing file permissions. Shared Docker edit locks
cover complete native edits and resolve equivalent paths to the same file.
The terminal clears completed tool indicators and retains partial answers
without displaying them twice. CLI configuration reaches the runtime, and
single-turn failures return a failing status while keeping the result view
available. The Typer dependency floor supports Click 8.5.

## [0.9.0] - 2026-10-02

### Added

Optional Chat Completions streaming assembles text, reasoning, and tool-call
fragments with first-event and stream-idle timeouts. Request observations expose
transport timing and retry counts. Public model inspection supports offline
configuration queries.

Team roles can select explicit agent profiles, use unbounded step limits, and
inspect their declared tools and prompts through public APIs. Optional delivery
snapshots record the worktree associated with each role's execution. The adopt
tool is available to explicitly configured roles. Patch application offers
opt-in hunk normalization and unique expected-text relocation.

The complete ICLR integration adds team, handoff, dual-candidate, and research
configurations with their supporting scripts, tests, and source documentation.

### Fixed

Session restoration preserves completed tool results and maintains message
ordering across cancellation, pending child tasks, and new user input. Request
budget estimates follow the actual transmitted history and its model-specific
reasoning fields.

### Changed

The integration retains the Base profile, Duo V7 workflow, and the execution-fact
loop detection and candidate budget inheritance released in 0.8.4. Streaming,
delivery-tree recording, and patch normalization remain explicit opt-ins.
Anthropic SDK compatibility is constrained to supported pre-1.0 releases.

## [0.8.4] - 2026-10-01

### Fixed

Loop detection records native execution and edit facts separately from rendered
tool output. Confirmed edits permit a bounded revalidation of older operations,
and real timeouts permit one longer retry within the retained call window.
Unchanged overwrites preserve their physical write behavior while contributing
no content-change progress. Three unproductive blocked model batches stop a
session, allowing feedback between batches while retaining per-call blocking.
Consumed retry permissions and completed prefix results survive session saves.

Candidate subworkflows inherit the budget approved by the parent allocator,
including remaining-pool caps and concurrent shares. Explicit unbounded mode
continues to propagate unlimited token and step budgets.

## [0.8.3] - 2026-10-01

### Fixed

Duo V7 compares each candidate's concrete behavior against public requirements,
with changed-path evidence for the input, trigger, control flow and expected
output. Equivalent evidence and identical diffs select B, while demonstrated
coverage advantages and explicit missing requirements can still select A.
Unusable adjudications receive at most one targeted review before the default-B
choice, preserving A when the evidence establishes a requirement missing in B.
Comparison payloads within 128,000 UTF-8 bytes provide both full diffs, all
individual and shared public-test records, and reports marked as model-supplied
inline for a complete structured decision. Larger payloads use complete paged
evidence reads. Original evidence files are retained in both modes.

File-read loop detection distinguishes pagination ranges while normalizing
equivalent default arguments. Worktree cleanup remains retryable after a partial
failure, and command-line configuration errors retain their useful diagnostics.
Duo working-tree mode delegates final commits and packaging to its caller.

### Changed

The documentation covers the 0.8.x APIs, Base profile, Duo workflow and benchmark
usage. Distribution builds use Hatchling 1.32.4, and CI uses setup-uv 10.2.0.

## [0.8.2] - 2026-09-28

### Fixed

- Corrected the input windows for Qwen 3.8 Flash and DeepSeek V4.1 Flash, and
  avoided forced tool selection when Qwen thinking mode does not support it.
- Marked reasoning as withheld in traces when a provider bills reasoning tokens
  without returning reasoning content.
- Aligned edit tool descriptions and the handoff experiment's analyst prompt
  with the tools and permissions actually available to each role.
- Retried transient, statusless gateway concurrency and streaming failures, and
  preserved team results and usage when cleanup or trace persistence fails.
- Notified waiting senders when a teammate stops without answering, including
  after session restoration.
- Accepted an optional final line terminator when validating a replacement
  range ending in a blank line, while still rejecting mismatched ranges.

## [0.8.1] - 2026-09-27

### Changed

Tests are organized by agents, workflows, tools, runtime, interfaces, and
packaging. Shared preparation uses explicit test-support imports. Source-tree,
installed-package, CI, and documentation paths follow the same directory layout.
Repeated preparation code is shared by the behavior area that owns it.

Source distributions retain the complete tests. Wheel validation checks that
only runtime distribution content is shipped, with test and cache trees kept
outside the wheel. Test workspaces and reports use temporary or external paths.

## [0.8.0] - 2026-09-27

### Changed

The default standalone agent is Base, currently mapped to Single2. The former
standalone Single prompt and execution path have been removed. The `default`
and `single` profile spellings select Base, while `single2` and `agent2` select
Single2 explicitly. Explicit token and step limits follow Single2 behavior.

### Added

A named profile factory registry keeps single-agent implementations extensible.
The public `opencollab.profiles` module exposes `BASE_PROFILE` and
`resolve_profile_name`, and standalone result metrics record the concrete
implementation name.

## [0.7.1] - Unreleased

### Added

Duo is the single installed dual-candidate workflow available through the CLI
and SDK. Its task-oriented prompts use complete, paged candidate evidence. Public native Bash evidence
observation and Git patch path parsing now belong to OpenCollab, so downstream
evaluators can use the same execution and selection implementation.

### Changed

Named SDK workflow calls discover installed and caller-defined workflows.
Local Duo shell execution has an explicit caller opt-in while the default
continues to require process isolation.

## [0.7.0] - 2026-09-14

### Added

Paired evaluation runtime support with candidate workspace isolation, configured unbounded limits, public model and snapshot inspection, and durable request lifecycle observations.

### Changed

Native project tests run through Bash; the built-in `run_tests` tool is removed.
OpenCollab-Eval 0.7.0 owns parser-backed verification for research workflows.
Docker control operations now allow 60 seconds for daemon cleanup.

### Fixed

Revoked execution environments terminate their sessions instead of continuing
ineffective tool calls. Candidate worktrees retain the correct environment and
cleanup ownership. Redirected CLI input preserves queued tasks and answers,
including pipes, ordinary files, encodings and concurrent questions.


## [0.6.0] - 2026-09-04

### Added
- Added public team and workflow controls for prebuilt rosters, turn
  serialization, per-seat step limits, caller-supplied environments, and
  declared team-role inspection.
- Added public handoff and submit tools, worktree-backed teammate execution,
  structured worktree and trace evidence, and the handoff experiment example.
- Added provider capability contracts, Responses non-streaming support, richer
  usage accounting, and explicit model/provider request validation.

### Changed
- Expanded the SDK and workflow-authoring contracts with explicit agent
  identity, isolation, budgets, deadlines, cancellation, cleanup, and
  lifecycle results. Teams now use the caller's environment when supplied and
  keep their declared topology and resource decisions in the run evidence.
- Hardened scheduler, session, storage, repository-map, and environment
  boundaries so ownership, snapshots, path containment, terminal state, and
  partial work remain explicit across retries and restored runs.
- Reworked the CLI/TUI turn queue and display lifecycle, and split the runtime
  into narrower architecture-bound modules with import-layer checks.

### Fixed
- Fixed per-call budget validation, one-shot budget escapes, cancellation-state
  reset, complete test-run evidence checks, command-safety checks, and
  provider usage estimates. Normalized BSD/macOS `wc -c` output so verified
  container file writes work across host and image shells.
- Fixed handoff accounting, shell-output traversal, container git identity,
  worktree diff bases, stale history sizing, and team/workflow resource
  attribution.

### Removed
- Removed the legacy toolbar and keyboard modules superseded by the current
  CLI/TUI turn-queue implementation.

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

[Unreleased]: https://github.com/RISE-X-Lab/OpenCollab/compare/v0.9.3...HEAD
[0.9.3]: https://github.com/RISE-X-Lab/OpenCollab/compare/v0.9.2...v0.9.3
[0.9.2]: https://github.com/RISE-X-Lab/OpenCollab/compare/v0.9.1...v0.9.2
[0.9.1]: https://github.com/RISE-X-Lab/OpenCollab/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/RISE-X-Lab/OpenCollab/compare/v0.8.4...v0.9.0
[0.8.4]: https://github.com/RISE-X-Lab/OpenCollab/compare/v0.8.3...v0.8.4
[0.8.3]: https://github.com/RISE-X-Lab/OpenCollab/compare/v0.8.2...v0.8.3
[0.8.2]: https://github.com/RISE-X-Lab/OpenCollab/compare/v0.8.1...v0.8.2
[0.8.1]: https://github.com/RISE-X-Lab/OpenCollab/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/RISE-X-Lab/OpenCollab/compare/v0.7.0...v0.8.0
[0.6.0]: https://github.com/RISE-X-Lab/OpenCollab/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/RISE-X-Lab/OpenCollab/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/RISE-X-Lab/OpenCollab/compare/563027175e2cc2540d19324def73010a7e436dcc...v0.4.1
[0.1.0]: https://github.com/RISE-X-Lab/OpenCollab/tree/563027175e2cc2540d19324def73010a7e436dcc

[0.7.0]: https://github.com/RISE-X-Lab/OpenCollab/compare/v0.6.0...v0.7.0
