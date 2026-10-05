# Skills

A **skill** is an on-demand instruction set stored in `SKILL.md`. It adds
procedural guidance to a role without registering a new runtime function or a
dedicated tool. A skill directory may contain supporting files used through
tools the role already has. Enabled roles load skills through the generic
`use_skill` tool.

On startup, the loader scans immediate skill directories for `SKILL.md` and
builds a catalog from each accepted skill's `name` and `description`. Roles with
`use_skill` receive that catalog in their system prompt. When a catalogued skill
matches the task, the model calls `use_skill` with a `name` argument and receives
the complete instruction body as the tool result.

> The design record explains this interface in
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

The frontmatter identifies the skill and supplies its catalog description.

| Field | Required | Purpose |
|---|---|---|
| `name` | yes | The invocation key passed to `use_skill(name)`. Keep it equal to the directory name. |
| `description` | recommended | Text shown in the catalog and used to match a task. An omitted description loads as an empty string. |

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
invoke it by name. Adding a skill requires no registration or code change.

---

## Conventions & limits

| Topic | Convention |
| --- | --- |
| Naming | Use a short kebab-case `name` equal to the directory name. The model must type it exactly. |
| Description | Write a specific task trigger such as "when you need to …". The model decides whether to load the skill from this field. |
| Body | Write self-contained instructions that use tools already assigned to the role. A skill cannot grant additional tools. |
| Body size | A body of up to 8,000 characters loads in full. A longer body is rejected and excluded from the catalog. `FileSkillStore.load_diagnostics` and a warning identify the rejected skill. |
| Description size | Descriptions are truncated to 500 characters for the catalog. |
| File and directory limits | A skill file is limited to 64 KiB. The root scan accepts up to 256 package directories and 4,096 entries. Exceeding either directory limit raises `ValueError`. |
| Malformed or unsafe file | The loader skips a missing or non-string `name`, non-string description, unclosed frontmatter, read error, symlink or oversized file. A missing, non-directory or symlink root gives an empty catalog. |
| Duplicate name | Sorted package directories determine discovery order. The first accepted skill with a name is retained. |
| Unknown name | `use_skill` reports the requested name and available catalog entries, or states that the catalog is empty. |

## Where skills are loaded from

The current loader reads the `skills/` directory at the workspace root. In this
repository, that path is `skills/`. The design record discusses global and
project-specific search paths that the current loader does not implement.
