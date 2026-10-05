# Releasing OpenCollab

This guide covers tagged GitHub releases and their distribution artifacts. PyPI
publishing is outside the current documented release process. Supported
installation paths use a GitHub source checkout or the wheel attached to a
GitHub release.

## Release invariants

- Release only a clean commit already present on `main`.
- Use package version `X.Y.Z` and signed annotated tag `vX.Y.Z`.
- Verify tests, artifacts, and GitHub checks against the exact release SHA.
- Push only the intended tag ref; never use `git push --tags` for a release.
- Never move or replace a published tag. Correct a published defect with a new
  patch release.
- Protecting release tags is recommended. If protection is unavailable, record
  an explicit maintainer waiver before publishing; tag immutability remains an
  operational requirement.

## Finalize the release

Use a focused pull request with a Conventional Commit title using an English
type and Chinese description. Move the intended entries from `Unreleased` into
a dated version section in `CHANGELOG.md` and update its comparison links.
Align the version in `pyproject.toml`, `uv.lock`, `opencollab/__init__.py`, and
any exact-version tests. Regenerate the lock file when project metadata changes
and keep the release pull request focused on that version.

After the pull request is merged, refresh `main` and record the candidate SHA.

```bash
git fetch origin main
git switch main
git pull --ff-only origin main
git status --short --branch
release_sha="$(git rev-parse HEAD)"
test "$release_sha" = "$(git rev-parse origin/main)"
```

## Verify the exact candidate

Run the repository checks from the clean candidate with Node 20 or newer
available for the blueprint DOM tests.

```bash
uv sync --locked --extra dev --python 3.12
uv lock --check
uv run ruff check .
uv run lint-imports
uv run deptry .
uv run pytest -q
npm ci --prefix tests/workflows/blueprint_dom --no-audit --no-fund
npm test --prefix tests/workflows/blueprint_dom
```

Wait for every GitHub check on `release_sha`, including the Python 3.10–3.14
matrix, blueprint DOM tests, Distribution artifacts, macOS platform integrity,
Hygiene, Conventional Title, and Security checks. Record their results against
the exact release commit.

## Build and inspect artifacts

Read the candidate version from project metadata, then build the wheel from
the source distribution as CI does. The following Bash commands set variables
used across multiple steps, so run them all in the same shell session.

```bash
set -euo pipefail
shopt -s nullglob
release_version="$(uv run python -c 'from pathlib import Path; import tomllib; print(tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"])')"
artifact_root="$(mktemp -d -t "opencollab-${release_version}.XXXXXX")"
mkdir -p "$artifact_root/sdist" "$artifact_root/wheel" "$artifact_root/assets"

uv build --sdist --no-sources --out-dir "$artifact_root/sdist"
sdists=("$artifact_root"/sdist/*.tar.gz)
test "${#sdists[@]}" -eq 1

uv build --wheel --no-sources "${sdists[0]}" --out-dir "$artifact_root/wheel"
wheels=("$artifact_root"/wheel/*.whl)
test "${#wheels[@]}" -eq 1

uvx --from twine==7.0.0 twine check "${sdists[0]}" "${wheels[0]}"
cp "${sdists[0]}" "${wheels[0]}" "$artifact_root/assets/"
(
  cd "$artifact_root/assets"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum ./*.tar.gz ./*.whl > SHA256SUMS
  else
    shasum -a 256 ./*.tar.gz ./*.whl > SHA256SUMS
  fi
)
```

Install the wheel in a new environment and run the probe from a separate
directory outside the checkout (to ensure the installed package is used
rather than the local source).

```bash
probe_root="$(mktemp -d -t "opencollab-${release_version}-probe.XXXXXX")"
install_root="$probe_root/venv"
uv venv --no-project --python 3.12 "$install_root"
uv pip install --python "$install_root/bin/python" --link-mode copy "${wheels[0]}"
(
  cd "$probe_root"
  "$install_root/bin/python" -c \
    "import importlib.metadata as metadata; import opencollab; assert opencollab.__version__ == metadata.version('opencollab') == '${release_version}'"
  "$install_root/bin/opencollab" --help >/dev/null
  "$install_root/bin/opencollab" workflow --help >/dev/null
  "$install_root/bin/opencollab" workflow run --help >/dev/null
)
```

## Tag and publish

Create and verify the signed annotated tag. If signing is unavailable, obtain
an explicit maintainer decision before using an unsigned annotated tag.

```bash
git tag -s "v${release_version}" "$release_sha" -m "OpenCollab v${release_version}"
git tag -v "v${release_version}"
git push origin "refs/tags/v${release_version}"

remote_sha="$(git ls-remote origin "refs/tags/v${release_version}^{}" | cut -f1)"
test "$remote_sha" = "$release_sha"
```

Prepare curated notes from the matching changelog section. The maintainer
chooses whether the GitHub entry is a regular release or a prerelease. The
following command publishes a regular GitHub release. Write `release-notes.md`
in Chinese before running it.

```bash
gh release create "v${release_version}" \
  "$artifact_root/assets/"* \
  --repo RISE-X-Lab/OpenCollab \
  --verify-tag \
  --title "OpenCollab ${release_version}" \
  --notes-file release-notes.md
```

## Verify the public release

Download the published assets into a new directory, verify their hashes, and
repeat the wheel installation probe. Confirm that the release page, changelog
links, tag SHA, and anonymous clone all resolve as expected.

If GitHub Release creation fails after the tag is pushed, fix the release entry
against the same verified tag. Do not delete, recreate, or move the tag. If an
artifact or code defect is discovered after publication, issue a new patch
release.
