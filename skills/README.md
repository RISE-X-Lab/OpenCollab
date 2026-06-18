# Skills

A **skill** is an on-demand *instruction set* — a packaged unit of procedural
knowledge an agent can pull into its context when the task calls for it. A skill is
**not** a tool: it carries no code and registers no function. It is plain instructions
that the model loads by name.

How it works at runtime:

1. On startup, every skill under this directory is discovered and a **catalog**
   (each skill's `name` + `description`) is folded into the agent's system prompt.
2. When a catalogued skill matches the task, the model calls the generic
   `use_skill(name)` tool, which loads that skill's full instruction **body** into the
   conversation.

One `use_skill` tool serves every skill — adding skills never adds tools.

> Internals / design rationale: see
> [`docs/2026-06-18-skill-interface-design.md`](../docs/2026-06-18-skill-interface-design.md).

---

## Adding a skill

### 1. Create the file

Each skill is one directory holding a `SKILL.md`:

```
skills/
└── <skill-name>/
    └── SKILL.md
```

`SKILL.md` is YAML frontmatter (delimited by `---`) followed by the instruction body:

```markdown
---
name: review-migration
description: Review a database migration for safety and reversibility
---

When asked to review a migration:

1. Check the migration is reversible (a `down`/rollback exists and is correct).
2. Flag any non-concurrent index build or table rewrite on a large table.
3. Confirm new NOT NULL columns have a default or a backfill plan.
4. Summarise risk as LOW / MEDIUM / HIGH with the single biggest concern.
```

Frontmatter fields:

| Field | Required | Purpose |
|---|---|---|
| `name` | yes | The **invocation key** — what the model passes to `use_skill(name)`. Keep it equal to the directory name. |
| `description` | yes | One line shown in the catalog. This is what the model matches against the task, so make it specific and trigger-worthy. |

Everything after the closing `---` is the **body** — the instructions loaded when the
skill is invoked.

### 2. Enable skills for a role (the opt-in switch)

A role only sees the catalog and gets the `use_skill` tool if you add `use_skill` to
its `tools:` list in your team config (`team.yaml`):

```yaml
roles:
  specialist:
    tools: [bash, file_read, use_skill]   # <- add use_skill
```

Without this, skills stay invisible to that role. This is intentional — skills are
**off by default** until a role opts in.

### 3. That's it

No registration, no code changes. Restart the app and the role's system prompt will
list your new skill; the model invokes it by name when relevant.

---

## Conventions & limits

- **Naming.** Use a short, kebab-case `name` that matches the directory. The model
  must type it exactly, so prefer clarity over cleverness.
- **Descriptions are triggers.** The model decides whether to load a skill from its
  `description` alone — write it as "when you need to …", not as a title.
- **Bodies should be self-contained instructions.** A body may *refer* to tools the
  role already has (e.g. "run the tests with bash"), but it cannot grant new tools.
- **Size caps** (enforced when loading, single-sited in the store):
  - body: 8000 characters (longer bodies are truncated with a clear marker);
  - description: 500 characters.
  Keep bodies tight — they cost context every time they're loaded.
- **Malformed skills are skipped, never fatal.** A `SKILL.md` with no `name`, an
  unclosed frontmatter block, or an unreadable file is silently ignored at startup —
  it will simply not appear in the catalog. If your skill isn't showing up, check the
  frontmatter delimiters and that `name` is present.
- **Unknown invocation is graceful.** If the model calls `use_skill` with a name that
  doesn't exist, it gets back `Unknown skill '<x>'. Available skills: …` rather than
  an error.

## Where skills are loaded from

Skills are read from the `skills/` directory at the **workspace root** the agent runs
against. Today that is this repository's `skills/`. (A global skills layer and
per-project overrides are planned but not yet implemented — see the design doc.)
