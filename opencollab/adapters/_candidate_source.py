"""Compare a retained source state across Git conversion changes."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any

from opencollab.adapters._candidate_physical import (
    _has_worktree_conversions,
    _initialized_submodule_paths,
    _physical_submodule_trees,
    _physical_tree,
    _submodule_base_tree,
    _tree_patch,
)
from opencollab.adapters._git_patch_bytes import _complete, _patch_temporary


@dataclass(slots=True)
class _SourceSnapshot:
    owner: Any
    root: str
    revision: str
    index_revision: str
    patch: str
    excluded: tuple[str, ...]
    physical_tree: str | None
    modules: tuple[tuple[str, str], ...]
    initialized: tuple[str, ...]

    async def _original_tree(self) -> str:
        environment = self.owner._environment
        async with _patch_temporary(environment, self.root, self.patch, prefix=".candidate-source-original-") as patch:
            async with _patch_temporary(environment, self.root, "", prefix=".candidate-source-index-") as index:
                git = f"GIT_NO_REPLACE_OBJECTS=1 GIT_INDEX_FILE={shlex.quote(index)} git -C {shlex.quote(self.root)}"
                command = f"{git} read-tree {shlex.quote(self.revision)}"
                if self.patch.strip():
                    command += f" && {git} apply --cached --binary --whitespace=nowarn -- {shlex.quote(patch)}"
                if self.excluded:
                    command += (
                        f" && {git} --literal-pathspecs rm -r --quiet --cached --ignore-unmatch -- "
                        f"{shlex.join(self.excluded)}"
                    )
                return _complete(await environment.exec_cmd(
                    f"{command} && {git} write-tree", timeout=900,
                ), "original source contents").strip()

    def _included(self, paths: tuple[str, ...]) -> set[str]:
        return {path for path in paths if not any(
            path == excluded or path.startswith(excluded + "/") for excluded in self.excluded
        )}

    async def changed(self, patch: str | None) -> bool:
        if patch is None:
            raise RuntimeError("candidate source diff is unavailable")
        if (
            await self.owner.source_revision() != self.revision
            or await self.owner.source_index_revision() != self.index_revision
        ):
            return True
        environment = self.owner._environment
        untracked_excluded = (*await self.owner._source_untracked_paths(self.root), *self.excluded)
        initialized = await _initialized_submodule_paths(environment, self.root)
        if self._included(initialized) != self._included(self.initialized):
            return True
        current_modules = dict(await _physical_submodule_trees(
            environment, self.root, include_paths=tuple(path for path, _tree in self.modules),
            exclude_paths=untracked_excluded, exclude_tree_paths=self.excluded,
            module_paths=initialized,
        ))
        original_modules = dict(self.modules)
        for path in original_modules.keys() | current_modules.keys():
            before, after = original_modules.get(path), current_modules.get(path)
            if before is None:
                before = await _submodule_base_tree(environment, self.root, self.revision, path)
            if before == after:
                continue
            if before is None or after is None:
                return True
            excluded = tuple(item[len(path) + 1:] for item in self.excluded if item.startswith(path + "/"))
            if not excluded or (await _tree_patch(
                environment, self.root + "/" + path, before, after, exclude_paths=excluded,
            )).strip():
                return True
        if self.physical_tree is None and patch == self.patch and not await _has_worktree_conversions(
            environment, self.root, exclude_paths=self.excluded,
            exclude_untracked_paths=untracked_excluded,
        ):
            return False
        # A plain source's saved Git patch contains its physical bytes. Build
        # that original tree only when a later conversion or diff requires it.
        original = self.physical_tree or await self._original_tree()
        current = await _physical_tree(
            environment, self.root, self.revision,
            exclude_paths=untracked_excluded, exclude_tree_paths=self.excluded,
        )
        return current != original


async def _source_snapshot(owner: Any, patch: str | None, excluded: tuple[str, ...]) -> _SourceSnapshot:
    if patch is None:
        raise RuntimeError("candidate source diff is unavailable")
    root = await owner._repository_root()
    environment = owner._environment
    revision = await owner.source_revision()
    index_revision = await owner.source_index_revision()
    untracked_excluded = (*await owner._source_untracked_paths(root), *excluded)
    initialized = await _initialized_submodule_paths(environment, root)
    modules = await _physical_submodule_trees(
        environment, root, exclude_paths=untracked_excluded, exclude_tree_paths=excluded,
        module_paths=initialized,
    )
    physical_tree = None
    if await _has_worktree_conversions(
        environment, root, exclude_paths=excluded, exclude_untracked_paths=untracked_excluded,
    ):
        physical_tree = await _physical_tree(
            environment, root, revision, exclude_paths=untracked_excluded, exclude_tree_paths=excluded,
        )
    return _SourceSnapshot(
        owner, root, revision, index_revision, patch, excluded,
        physical_tree, modules, initialized,
    )
