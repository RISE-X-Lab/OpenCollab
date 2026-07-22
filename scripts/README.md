# Framework scripts

This directory contains OpenCollab framework launchers and provider diagnostics.
Benchmark generation, evaluation, reporting, and remote execution commands are
owned by an external evaluation harness rather than this framework package.

`start_opencollab.sh` prepares the local environment and starts the framework.
`check_dashscope.py` performs an explicit provider connectivity check using the
caller's configuration.
