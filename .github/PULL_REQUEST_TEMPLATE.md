## Summary

<!-- What does this change, and why? -->

## Type of change

- [ ] Bug fix (`fix:`)
- [ ] New feature (`feat:`)
- [ ] Refactor (`refactor:` — behavior-preserving)
- [ ] Docs / chore / ci

## How was this verified?

<!-- Commands run, tests added, manual checks. -->

```bash
uv run pytest -q && uv run ruff check .
```

## Checklist

- [ ] Tests added/updated and the suite is green
- [ ] Lint passes (`ruff check`)
- [ ] No inward → outward imports (adapters → application → domain preserved)
- [ ] Public names kept re-exported when a module was split
- [ ] Conventional-commit title; public-facing text in English
- [ ] No secrets committed
