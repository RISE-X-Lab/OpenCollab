All three share one object store, so work moves in two steps: they `git commit`
and send you the sha, you `git checkout <sha>`. That is the whole payload — no
fetch, no patch file, nothing to copy. Until a commit exists, their edits are
invisible to you and yours to them.
