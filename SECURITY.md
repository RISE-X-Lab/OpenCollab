# Security Policy

## Reporting a vulnerability

Report suspected security vulnerabilities **privately** through GitHub's
[Report a vulnerability](https://github.com/RISE-X-Lab/OpenCollab/security/advisories/new)
(Security Advisories). We aim to acknowledge reports within 72 hours.

## Risk surface

OpenCollab runs LLM-generated code and shell/tool calls against a real
repository, uses provider API keys, and can drive Docker containers. Treat it
as code execution and use these precautions.

- Run untrusted or benchmark tasks only in an **isolated, disposable
  environment** (container or VM), never against sensitive systems.
- Keep credentials in `configs/.env` (gitignored). **Never commit real API
  keys.** See `configs/.env.example`.
- Review an agent's tool permissions before granting broad filesystem or shell
  access.

## Supported versions

OpenCollab is pre-1.0. Security fixes land on `main`.
