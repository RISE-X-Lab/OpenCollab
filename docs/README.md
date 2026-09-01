# OpenCollab documentation

Start with the repository [README](../README.md) for setup and the two runtime
modes, then use the [package guide](../opencollab/README.md) for the CLI, Python
API, and architecture boundary.

## Current documentation

The [SDK 0.4 visual guide](sdk-0.4-explainer.html) explains the public research
interfaces. The [configuration guide](../configs/README.md) covers providers,
models, and team files. The [skills guide](../skills/README.md) explains
on-demand agent skills. Contribution checks and vulnerability reporting are in
[CONTRIBUTING.md](../CONTRIBUTING.md) and [SECURITY.md](../SECURITY.md).
The [0.5.0 migration guide](migrations/0.5.0.md) lists the explicit lifecycle,
isolation, scheduler, and workflow-contract changes currently under review.

[The collaborating team](2026-08-31-collab-team.md) documents
`configs/team.collab.yaml`: what the three-role team is, the three ways to run
it, and the four conditions that make a run a team's rather than one seat's.

## Design records

Dated Markdown files in this directory and `archive/` record earlier design
work. Their branch names, line-number anchors, test counts, and implementation
status reflect the repository at the time of writing. The package guide,
current source, and tests define current behavior.
