# Releasing OpenCollab

This guide covers tagged GitHub releases and their distribution artifacts. PyPI
publishing is not currently part of the OpenCollab release process.

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

## 1. Finalize the release

Use a focused pull request with an English Conventional Commit title. In that
pull request:

1. Move the intended entries from `Unreleased` into a dated version section in
   `CHANGELOG.md` and update its comparison links.
2. Align the version in `pyproject.toml`, `uv.lock`, `opencollab/__init__.py`, and
   any exact-version tests.
3. Regenerate the lock file when project metadata changes.
4. Keep unrelated changes out of the release pull request.

After the pull request is merged, refresh `main` and record the candidate SHA:

```bash
git fetch origin main
git switch main
git pull --ff-only origin main
git status --short --branch
release_sha="$(git rev-parse HEAD)"
test "$release_sha" = "$(git rev-parse origin/main)"
```

## 2. Verify the exact candidate

Run the repository checks from the clean candidate:

```bash
uv sync --locked --extra dev
uv lock --check
uv run ruff check .
uv run pytest -q
```

Wait for every GitHub check on `release_sha`, including the Python matrix,
Distribution artifacts, macOS platform integrity, hygiene, title, and security
checks. A successful job on another commit is not release evidence.

## 3. Build and inspect artifacts

Set the intended version explicitly, then build the wheel from the source
distribution just as CI does:

```bash
set -euo pipefail
release_version=0.5.1
artifact_root="$(mktemp -d -t "opencollab-${release_version}.XXXXXX")"
mkdir -p "$artifact_root/sdist" "$artifact_root/wheel" "$artifact_root/assets"

uv build --sdist --no-sources --out-dir "$artifact_root/sdist"
sdists=("$artifact_root"/sdist/*.tar.gz)
test "${#sdists[@]}" -eq 1

uv build --wheel --no-sources "${sdists[0]}" --out-dir "$artifact_root/wheel"
wheels=("$artifact_root"/wheel/*.whl)
test "${#wheels[@]}" -eq 1

uvx --from twine==6.2.0 twine check "${sdists[0]}" "${wheels[0]}"
cp "${sdists[0]}" "${wheels[0]}" "$artifact_root/assets/"
(
  cd "$artifact_root/assets"
  sha256sum ./*.tar.gz ./*.whl > SHA256SUMS
)
```

Install the wheel outside the checkout and probe the installed distribution:

```bash
probe_root="$(mktemp -d -t "opencollab-${release_version}-probe.XXXXXX")"
uv venv --no-project --python 3.12 "$probe_root"
uv pip install --python "$probe_root/bin/python" --link-mode copy "${wheels[0]}"
"$probe_root/bin/python" -c \
  "import opencollab; assert opencollab.__version__ == '${release_version}'"
"$probe_root/bin/opencollab" --help >/dev/null
```

## 4. Tag and publish

Create and verify the signed annotated tag. If signing is unavailable, stop and
obtain an explicit maintainer decision before using an unsigned annotated tag.

```bash
git tag -s "v${release_version}" "$release_sha" -m "OpenCollab v${release_version}"
git tag -v "v${release_version}"
git push origin "refs/tags/v${release_version}"

remote_sha="$(git ls-remote origin "refs/tags/v${release_version}^{}" | cut -f1)"
test "$remote_sha" = "$release_sha"
```

Prepare curated notes from the matching changelog section. While the project is
classified as Alpha, publish it as a GitHub prerelease:

```bash
gh release create "v${release_version}" \
  "$artifact_root/assets/"* \
  --repo RISE-X-Lab/OpenCollab \
  --verify-tag \
  --prerelease \
  --title "OpenCollab ${release_version}" \
  --notes-file release-notes.md
```

## 5. Verify the public release

Download the published assets into a new directory, verify their hashes, and
repeat the wheel installation probe. Confirm that the release page, changelog
links, tag SHA, and anonymous clone all resolve as expected.

If GitHub Release creation fails after the tag is pushed, fix the release entry
against the same verified tag. Do not delete, recreate, or move the tag. If an
artifact or code defect is discovered after publication, issue a new patch
release.
