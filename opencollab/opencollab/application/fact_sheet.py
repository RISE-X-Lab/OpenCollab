"""Deterministic pre-recon FACT SHEET + complexity sizing (STEP 5a / 5c).

A NON-LLM static extractor: given the target function (named in the goal) and the
in-workspace repo, it builds a manifest the recon scouts start from — target
signature + docstring, call sites (file:line), module imports, sibling functions
in the target file, and referenced type/class defs — so scouts do not burn budget
re-discovering signatures and call-sites. Pure ``ast`` + light regex over the
WORKSPACE repo files the scouts are already allowed to read.

INTEGRITY (critical, STEP 5a): the manifest is extracted ONLY from the in-workspace
(stubbed) repo source. The target function body is STUBBED in the workspace (KOCO
removed the ground truth, leaving signature + docstring + ``raise
NotImplementedError``) — that is the correct, safe source. This module NEVER reads
``test_code/``, ``func_implementation*``, ``*_result.jsonl``, ``*_output.jsonl`` or
any other reference/answer artifact. :func:`is_answer_path` is the guard predicate;
every file the extractor scans is filtered through it, and a final tripwire
(:class:`FactSheetIntegrityError`) refuses to return a manifest whose scanned set
ever contained an answer path.

Complexity sizing (STEP 5c): the GT body is stubbed, so target-body LOC is not a
usable signal. :func:`estimate_target_complexity` keys on the signals that SURVIVE
stubbing — signature arity, docstring size, call-site fan-out, referenced types —
and :func:`size_recon` maps that to a scout COUNT (<= the caller's ceiling) plus a
per-scout depth leash, so a trivial one-liner does not get the full scout fan-out.

Pure application layer: stdlib only (no adapters/domain import) — Clean
Architecture forbids an inner layer importing an outer one.
"""

from __future__ import annotations

import ast
import os
import re
from typing import Any

# -- integrity guard --------------------------------------------------------- #

# Path components / name patterns that NEVER belong to the in-workspace stubbed
# source — they are KOCO answer / reference artifacts. The extractor refuses to
# read any of them. The stubbed workspace already had ``test_code/`` stripped, so
# this is defense-in-depth: a misconfigured root can never leak the ground truth.
_ANSWER_DIR_COMPONENTS = frozenset({"test_code"})
_ANSWER_NAME_SUBSTRINGS = ("func_implementation",)
_ANSWER_NAME_SUFFIXES = ("_result.jsonl", "_output.jsonl")

# Directories pruned from the walk (cost + noise), independent of the answer guard.
_SKIP_DIRS = frozenset(
    {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv", "venv", ".mypy_cache"}
)

# Cap a single file read so a stray huge blob cannot stall recon.
_MAX_FILE_BYTES = 2_000_000
# Bound on how many call sites we surface (the count is always exact).
_MAX_CALL_SITES = 40


class FactSheetIntegrityError(Exception):
    """Raised if the extractor's scanned file set ever contains an answer path.

    This should be unreachable (every file is filtered through
    :func:`is_answer_path` before being read); it is a loud tripwire, not a
    routine error. ``_recon`` degrades gracefully (skips the fact sheet) if it
    ever fires, so no answer content reaches the scouts.
    """


def is_answer_path(path: str) -> bool:
    """True if ``path`` is a KOCO answer/reference artifact the extractor must skip.

    Matches on any path component named ``test_code``, any filename containing
    ``func_implementation``, or any filename ending ``_result.jsonl`` /
    ``_output.jsonl``. Accepts absolute or relative, ``\\`` or ``/`` separators.
    """
    p = str(path).replace("\\", "/")
    parts = [c for c in p.split("/") if c]
    if any(c in _ANSWER_DIR_COMPONENTS for c in parts):
        return True
    name = parts[-1] if parts else p
    if any(sub in name for sub in _ANSWER_NAME_SUBSTRINGS):
        return True
    if any(name.endswith(suf) for suf in _ANSWER_NAME_SUFFIXES):
        return True
    return False


# -- target identification from the goal ------------------------------------- #

# "Implement the function `name`" (KOCO build_prompt) — backtick/quote optional,
# allow a dotted qualname (Class.method).
_FUNC_RE = re.compile(
    r"[Ii]mplement\s+the\s+function\s+[`'\"]?([A-Za-z_][\w.]*)[`'\"]?"
)
# "The function stub is at: <path> (near line N)" (KOCO build_prompt stub_context).
_STUB_RE = re.compile(r"stub\s+is\s+at:\s*(\S+?)\s*\(near\s+line\s*(\d+)\)")


def _parse_goal(goal: str) -> tuple[str | None, str | None, int | None]:
    """Extract (function qualname, stub file path, stub line) from the goal text.

    Returns ``(None, ...)`` for the function when the goal does not name one
    (CLI / non-KOCO tasks) — the caller then skips the fact sheet.
    """
    func = None
    m = _FUNC_RE.search(goal or "")
    if m:
        func = m.group(1)
    stub_file = None
    stub_line = None
    sm = _STUB_RE.search(goal or "")
    if sm:
        stub_file = sm.group(1)
        try:
            stub_line = int(sm.group(2))
        except ValueError:
            stub_line = None
    return func, stub_file, stub_line


def _short_name(qualname: str) -> str:
    return qualname.rsplit(".", 1)[-1]


def _class_qualifier(qualname: str) -> str | None:
    return qualname.rsplit(".", 1)[0] if "." in qualname else None


def _iter_source_files(root: str):
    """Yield ``(abspath, relpath)`` for every ``.py`` file under ``root`` that is
    NOT an answer artifact and not inside a pruned/answer directory."""
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune skip-dirs and any answer-ish directory IN PLACE so os.walk never
        # descends into them.
        dirnames[:] = [
            d for d in dirnames if d not in _SKIP_DIRS and not is_answer_path(d)
        ]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            abspath = os.path.join(dirpath, fn)
            relpath = os.path.relpath(abspath, root)
            if is_answer_path(relpath) or is_answer_path(abspath):
                continue
            yield abspath, relpath


def _resolve_target_file(
    root: str, stub_file: str | None, short_name: str
) -> str | None:
    """Locate the in-workspace ``.py`` file defining the target function.

    Prefers the stub path named in the goal; falls back to the first source file
    that defines ``def <short_name>``. Always returns a path under ``root`` that
    passes the answer guard, or ``None``.
    """
    if stub_file:
        cand = stub_file if os.path.isabs(stub_file) else os.path.join(root, stub_file)
        cand = os.path.normpath(cand)
        if os.path.isfile(cand) and not is_answer_path(cand):
            return cand
        # Try interpreting it as a path relative to root even if it looked absolute.
        tail = stub_file.replace("\\", "/").lstrip("/")
        cand2 = os.path.normpath(os.path.join(root, tail))
        if os.path.isfile(cand2) and not is_answer_path(cand2):
            return cand2
    # Fallback: search for a definition of the short name.
    def_re = re.compile(rf"^\s*(?:async\s+)?def\s+{re.escape(short_name)}\s*\(", re.M)
    for abspath, _rel in _iter_source_files(root):
        try:
            if os.path.getsize(abspath) > _MAX_FILE_BYTES:
                continue
            with open(abspath, "r", encoding="utf-8", errors="replace") as fh:
                if def_re.search(fh.read()):
                    return abspath
        except OSError:
            continue
    return None


def _build_signature(node: ast.AST) -> str:
    """Render ``def name(args) -> ret`` from a function def node (no body)."""
    assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
    try:
        args = ast.unparse(node.args)
    except Exception:  # noqa: BLE001 — fall back below
        args = "..."
    ret = ""
    if node.returns is not None:
        try:
            ret = f" -> {ast.unparse(node.returns)}"
        except Exception:  # noqa: BLE001
            ret = ""
    return f"{prefix}{node.name}({args}){ret}"


def _param_count(node: ast.AST, *, is_method: bool) -> int:
    a = node.args  # type: ignore[attr-defined]
    names = list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)
    count = len(names)
    if is_method and count and names[0].arg in ("self", "cls"):
        count -= 1
    return count


def _annotation_types(node: ast.AST) -> list[str]:
    """Type names referenced in the target's parameter/return annotations."""
    out: list[str] = []
    seen: set[str] = set()

    def _walk(ann: ast.AST | None) -> None:
        if ann is None:
            return
        for sub in ast.walk(ann):
            if isinstance(sub, ast.Name):
                name = sub.id
            elif isinstance(sub, ast.Attribute):
                try:
                    name = ast.unparse(sub)
                except Exception:  # noqa: BLE001
                    continue
            else:
                continue
            if name not in seen:
                seen.add(name)
                out.append(name)

    a = node.args  # type: ignore[attr-defined]
    for arg in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs):
        _walk(arg.annotation)
    if a.vararg:
        _walk(a.vararg.annotation)
    if a.kwarg:
        _walk(a.kwarg.annotation)
    _walk(node.returns)  # type: ignore[attr-defined]
    return out


def _module_imports(tree: ast.Module) -> list[str]:
    out: list[str] = []
    for stmt in tree.body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            try:
                out.append(ast.unparse(stmt))
            except Exception:  # noqa: BLE001
                continue
    return out


def _find_target(
    tree: ast.Module, short_name: str, class_qual: str | None, stub_line: int | None
):
    """Return ``(func_node, enclosing_class_node_or_None)`` for the target.

    Disambiguates overloads by class qualifier first, then by nearest line to the
    stub location, then first definition.
    """
    candidates: list[tuple[ast.AST, ast.ClassDef | None]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == short_name:
                    candidates.append((child, node))
        elif isinstance(node, ast.Module):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == short_name:
                    candidates.append((child, None))
    if not candidates:
        return None, None
    if class_qual:
        for fn, cls in candidates:
            if cls is not None and cls.name == class_qual:
                return fn, cls
    if stub_line is not None and len(candidates) > 1:
        candidates.sort(key=lambda fc: abs(getattr(fc[0], "lineno", 0) - stub_line))
    return candidates[0]


def _siblings(tree: ast.Module, enclosing: ast.ClassDef | None, target: ast.AST) -> list[str]:
    out: list[str] = []
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt is not target:
            out.append(stmt.name)
    if enclosing is not None:
        for stmt in enclosing.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt is not target:
                out.append(f"{enclosing.name}.{stmt.name}")
    return out


def _class_defs(tree: ast.Module) -> list[str]:
    return [n.name for n in tree.body if isinstance(n, ast.ClassDef)]


def _scan_call_sites(
    root: str, short_name: str, target_rel: str, def_lineno: int
) -> tuple[list[str], int, list[str]]:
    """Grep every source file for ``short_name(`` calls.

    Returns ``(sites, total_count, scanned_relpaths)`` where ``sites`` is capped
    at :data:`_MAX_CALL_SITES` ``relpath:line`` strings (the def line itself is
    excluded) and ``scanned_relpaths`` is every file actually read (the integrity
    surface).
    """
    call_re = re.compile(rf"\b{re.escape(short_name)}\s*\(")
    sites: list[str] = []
    total = 0
    scanned: list[str] = []
    for abspath, relpath in _iter_source_files(root):
        try:
            if os.path.getsize(abspath) > _MAX_FILE_BYTES:
                continue
            with open(abspath, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        scanned.append(relpath)
        for i, line in enumerate(lines, start=1):
            if relpath == target_rel and i == def_lineno:
                continue  # skip the definition itself
            if call_re.search(line):
                total += 1
                if len(sites) < _MAX_CALL_SITES:
                    sites.append(f"{relpath}:{i}")
    return sites, total, scanned


def build_fact_sheet(workspace_root: str | None, goal: str) -> dict[str, Any] | None:
    """Build the deterministic pre-recon manifest, or ``None`` if not applicable.

    Returns ``None`` (degrade gracefully — the caller logs and skips) when there
    is no workspace root, the goal names no target function, or the target file /
    definition cannot be located. INTEGRITY: every file read is filtered through
    :func:`is_answer_path`; the returned ``scanned_files`` is verified to contain
    no answer path before returning (else :class:`FactSheetIntegrityError`).
    """
    if not workspace_root or not os.path.isdir(workspace_root):
        return None
    qualname, stub_file, stub_line = _parse_goal(goal)
    if not qualname:
        return None
    short = _short_name(qualname)
    class_qual = _class_qualifier(qualname)

    target_abs = _resolve_target_file(workspace_root, stub_file, short)
    if not target_abs:
        return None
    try:
        with open(target_abs, "r", encoding="utf-8", errors="replace") as fh:
            source = fh.read()
        tree = ast.parse(source)
    except (OSError, SyntaxError, ValueError):
        return None

    fn_node, enclosing = _find_target(tree, short, class_qual, stub_line)
    if fn_node is None:
        return None

    target_rel = os.path.relpath(target_abs, workspace_root)
    is_method = enclosing is not None
    docstring = ast.get_docstring(fn_node) or ""
    referenced = _annotation_types(fn_node) + _class_defs(tree)
    # de-dup, preserve order
    seen: set[str] = set()
    referenced_types = [t for t in referenced if not (t in seen or seen.add(t))]

    call_sites, call_count, scanned = _scan_call_sites(
        workspace_root, short, target_rel, getattr(fn_node, "lineno", -1)
    )
    scanned_files = sorted({target_rel, *scanned})

    # Integrity tripwire — must be unreachable (every file was guard-filtered).
    leaked = [f for f in scanned_files if is_answer_path(f)]
    if leaked:
        raise FactSheetIntegrityError(
            f"fact sheet scanned answer artifacts (would leak GT): {leaked}"
        )

    return {
        "function_name": short,
        "qualname": qualname if is_method else short,
        "enclosing_class": enclosing.name if enclosing is not None else None,
        "target_file": target_rel,
        "signature": _build_signature(fn_node),
        "docstring": docstring,
        "docstring_len": len(docstring),
        "param_count": _param_count(fn_node, is_method=is_method),
        "imports": _module_imports(tree),
        "siblings": _siblings(tree, enclosing, fn_node),
        "referenced_types": referenced_types,
        "call_sites": call_sites,
        "call_site_count": call_count,
        "scanned_files": scanned_files,
    }


def format_fact_sheet_hint(manifest: dict[str, Any], *, max_doc: int = 240) -> str:
    """Render the manifest into a compact hint block prepended to a scout's hints.

    Deterministic, harness-extracted — scouts are told to verify before relying,
    but should not re-derive the signature or re-hunt call sites.
    """
    doc = manifest.get("docstring") or ""
    doc = doc.strip().replace("\n", " ")
    if len(doc) > max_doc:
        doc = doc[:max_doc].rstrip() + " …"
    lines = [
        "Pre-recon fact sheet (static, harness-extracted — verify before relying, "
        "do NOT re-derive the signature or re-hunt call sites):",
        f"- Target: {manifest.get('signature', '')}  [in {manifest.get('target_file', '')}]",
    ]
    if doc:
        lines.append(f"- Docstring: {doc}")
    imports = manifest.get("imports") or []
    if imports:
        lines.append(f"- Module imports: {'; '.join(imports[:12])}")
    siblings = manifest.get("siblings") or []
    if siblings:
        lines.append(f"- Sibling functions in file: {', '.join(siblings[:20])}")
    types = manifest.get("referenced_types") or []
    if types:
        lines.append(f"- Referenced types/classes: {', '.join(types[:20])}")
    sites = manifest.get("call_sites") or []
    count = manifest.get("call_site_count", len(sites))
    if sites:
        shown = ", ".join(sites[:12])
        more = f" (+{count - 12} more)" if count > 12 else ""
        lines.append(f"- Call sites ({count}): {shown}{more}")
    else:
        lines.append("- Call sites: none found in-workspace")
    return "\n".join(lines)


# -- STEP 5c — complexity sizing --------------------------------------------- #


def estimate_target_complexity(manifest: dict[str, Any]) -> int:
    """Cheap static complexity proxy (0..7) for a STUBBED target.

    The GT body is stubbed, so body LOC is not usable. Keys only on signals that
    SURVIVE stubbing: signature arity, docstring size, call-site fan-out, and the
    count of referenced types/classes. Higher = more scouts justified.
    """
    points = 0
    params = int(manifest.get("param_count", 0) or 0)
    if params >= 2:
        points += 1
    if params >= 5:
        points += 1
    doclen = int(manifest.get("docstring_len", 0) or 0)
    if doclen >= 240:
        points += 1
    if doclen >= 800:
        points += 1
    calls = int(manifest.get("call_site_count", len(manifest.get("call_sites") or [])) or 0)
    if calls >= 3:
        points += 1
    if calls >= 12:
        points += 1
    if len(manifest.get("referenced_types") or []) >= 3:
        points += 1
    return points


def size_recon(n_dims: int, complexity: int, *, ceiling: int) -> tuple[int, float]:
    """Map (analyst dimension count, complexity points) -> (scout_count, depth_leash).

    Keeps ``ceiling`` (MAX_SCOUTS) as the hard cap and only ever REDUCES below the
    analyst's ``n_dims`` for simpler targets — a trivial one-liner gets 1 scout no
    matter how many dimensions the analyst over-produced. ``depth_leash`` (0..1)
    scales each scout's token cap down for simpler targets so a lone scout does not
    just absorb the budget freed by dropping its peers (the 822k-on-a-one-liner
    failure mode). The complex bucket returns the full ceiling + leash 1.0, i.e.
    today's behavior.
    """
    if complexity <= 0:
        cap, leash = 1, 0.45
    elif complexity <= 2:
        cap, leash = 2, 0.65
    elif complexity <= 4:
        cap, leash = 3, 0.85
    else:
        cap, leash = ceiling, 1.0
    cap = min(cap, ceiling)
    n_scouts = max(1, min(n_dims, cap))
    return n_scouts, leash


def recon_pool_is_ample(
    remaining: int, recon_floor: int, n_dims: int, scout_budget: int
) -> bool:
    """True when the recon pool can fund EVERY scope dimension at the full per-scout
    ceiling — i.e. the token pool is NOT the binding constraint.

    :func:`size_recon` exists to ration scouts when the pool is scarce (splitting a
    small pool across many scouts starves each one). When the pool is large enough
    that ``pool // n_dims`` already meets ``scout_budget``, there is nothing to
    ration: the caller should run the full fan-out at full depth and let the
    in-loop info-gain wind-down / commit-first brake stop any scout that has nothing
    left to find — a far better signal than a static, body-blind complexity proxy,
    which systematically under-reads a hard target whose surface looks thin (no type
    annotations, in-workspace-only call sites). Returns ``False`` for ``n_dims<=0``.
    """
    if n_dims <= 0:
        return False
    pool = max(0, remaining - recon_floor)
    return (pool // n_dims) >= scout_budget
