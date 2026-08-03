# Skills

A **skill** is an on-demand instruction set stored in `SKILL.md`. The model loads
it by name when a task matches its description. A skill directory may also hold
templates, scripts, or assets used through the role's existing tools. All skills
share the generic `use_skill` tool.

At runtime, discovery and loading happen in two steps.

1. On startup, every skill under this directory is discovered and a **catalog**
   (each skill's `name` + `description`) is folded into the agent's system prompt.
2. When a catalogued skill matches the task, the model calls the generic
   `use_skill(name)` tool, which loads that skill's full instruction **body** into the
   conversation.

The same `use_skill` tool serves the full catalog.

> The implementation history is recorded in
> [`docs/2026-06-18-skill-interface-design.md`](../docs/2026-06-18-skill-interface-design.md).

---

## Adding a skill

### 1. Create the file

Each skill uses one directory containing `SKILL.md` and any supporting files.

```
skills/
└── <skill-name>/
    ├── SKILL.md
    └── scripts/ or templates/ (optional)
```

`SKILL.md` contains YAML frontmatter delimited by `---`, followed by the
instruction body.

```markdown
---
name: review-migration
description: Review a database migration for safety and reversibility
---

When asked to review a migration, follow these steps.

1. Check the migration is reversible (a `down`/rollback exists and is correct).
2. Flag any non-concurrent index build or table rewrite on a large table.
3. Confirm new NOT NULL columns have a default or a backfill plan.
4. Summarise risk as LOW / MEDIUM / HIGH with the single biggest concern.
```

The frontmatter has two required fields.

| Field | Required | Purpose |
|---|---|---|
| `name` | yes | The invocation key passed to `use_skill(name)`. Keep it equal to the directory name. |
| `description` | yes | One line shown in the catalog. This is what the model matches against the task, so make it specific and trigger-worthy. |

Everything after the closing `---` is the body loaded when the skill is invoked.

### 2. Enable skills for a role

A role sees the catalog and gets the `use_skill` tool after you add `use_skill` to
its `tools:` list in your team config (`team.yaml`).

```yaml
roles:
  specialist:
    tools: [bash, file_read, use_skill]   # <- add use_skill
```

Adding `use_skill` gives the role access to the skill catalog and loader. Roles
that omit it keep their existing tool set and system prompt.

### 3. Restart OpenCollab

After restart, the role's system prompt lists the new skill and the model can
invoke it by name.

---

## Conventions & limits

| Topic | Convention |
| --- | --- |
| Naming | Use a short kebab-case `name` equal to the directory name. The model must type it exactly. |
| Description | Write a specific task trigger such as "when you need to …". The model decides whether to load the skill from this field. |
| Body | Write self-contained instructions that use tools already assigned to the role. |
| Size | The loader caps the body at 8000 characters and the description at 500 characters. It marks a truncated body clearly. |
| Malformed file | The loader omits a `SKILL.md` with a missing `name`, unclosed frontmatter, or read error. Check the frontmatter and `name` if a skill is absent from the catalog. |
| Unknown name | `use_skill` returns `Unknown skill '<x>'. Available skills: …`. |

## Where skills are loaded from

The current loader reads the `skills/` directory at the workspace root. In this
repository, that path is `skills/`. The design record covers possible global and
project-specific search paths.
