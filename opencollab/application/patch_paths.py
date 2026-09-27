"""Small, shared primitives for parsing Git patch blocks and paths."""

from __future__ import annotations

_GIT_C_ESCAPES = {
    "a": 0x07,
    "b": 0x08,
    "t": 0x09,
    "n": 0x0A,
    "v": 0x0B,
    "f": 0x0C,
    "r": 0x0D,
    '"': 0x22,
    "\\": 0x5C,
}


def decode_git_c_path(value: str) -> str:
    value = str(value or "")
    quoted = value.startswith('"')
    index = 1 if quoted else 0
    decoded = bytearray()
    while index < len(value):
        char = value[index]
        if quoted and char == '"':
            break
        if char != "\\":
            decoded.extend(char.encode("utf-8", errors="surrogatepass"))
            index += 1
            continue
        index += 1
        if index >= len(value):
            decoded.append(ord("\\"))
            break
        escaped = value[index]
        if escaped in "01234567":
            end = index
            while end < len(value) and end < index + 3 and value[end] in "01234567":
                end += 1
            decoded.append(int(value[index:end], 8))
            index = end
            continue
        decoded.append(_GIT_C_ESCAPES.get(escaped, ord(escaped)))
        index += 1
    return decoded.decode("utf-8", errors="surrogateescape")


def git_header_tokens(header: str) -> list[str]:
    """Read the two Git header paths, including unquoted spaces in filenames."""
    text = str(header or "").rstrip("\r\n")
    prefix = "diff --git "
    if not text.startswith(prefix):
        return []
    text = text[len(prefix) :]
    if text.startswith('"'):
        index = 1
        while index < len(text):
            if text[index] == "\\":
                index += 2
                continue
            if text[index] == '"':
                end = index + 1
                target = text[end:].lstrip(" ")
                return [text[:end], target] if target else [text[:end]]
            index += 1
        return []
    if not text.startswith("a/"):
        return []
    body = text[2:]
    half = (len(body) - 3) // 2
    if half >= 0 and body[half:half + 3] == " b/" and body[:half] == body[half + 3:]:
        return ["a/" + body[:half], "b/" + body[half + 3:]]
    for marker in (' "b/', " b/"):
        split = text.rfind(marker)
        if split >= 0:
            return [text[:split], text[split + 1:]]
    return [text]


def diff_target_path(header: str) -> str:
    paths = git_header_tokens(header)
    if len(paths) >= 2:
        target = decode_git_c_path(paths[1])
        if target.startswith("b/"):
            return target[2:]
    if paths:
        source = decode_git_c_path(paths[0])
        if source.startswith("a/"):
            return source[2:]
    return ""


def git_diff_endpoint(token: str, side: str) -> str:
    path = decode_git_c_path(token)
    if path == "/dev/null":
        return ""
    prefix = f"{side}/"
    if path.startswith(prefix):
        path = path[len(prefix) :]
    return path


def patch_entries(patch: str) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for block in split_patch_blocks(patch):
        if not block or not block[0].startswith("diff --git "):
            continue
        old_path = new_path = ""
        for line in block[1:]:
            line = line.rstrip("\r\n")
            if line.startswith("--- "):
                old_path = git_diff_endpoint(line[4:].removesuffix("\t"), "a")
            elif line.startswith("+++ "):
                new_path = git_diff_endpoint(line[4:].removesuffix("\t"), "b")
            elif line.startswith(("rename from ", "copy from ")):
                old_path = decode_git_c_path(line.split(" ", 2)[2])
            elif line.startswith(("rename to ", "copy to ")):
                new_path = decode_git_c_path(line.split(" ", 2)[2])
            elif line.startswith(("@@", "GIT binary patch", "Binary files ")):
                break
        if not old_path and not new_path:
            tokens = git_header_tokens(block[0])
            if len(tokens) >= 2:
                old_path = git_diff_endpoint(tokens[0], "a")
                new_path = git_diff_endpoint(tokens[1], "b")
        if old_path or new_path:
            entries.append((old_path, new_path))
    return entries


def patch_paths(patch: str) -> list[str]:
    paths: dict[str, None] = {}
    for old_path, new_path in patch_entries(patch):
        for path in (old_path, new_path):
            if path:
                paths.setdefault(path, None)
    return list(paths)


def patch_block_target_path(block: list[str]) -> str:
    entries = patch_entries("".join(block))
    if len(entries) != 1:
        return ""
    old_path, new_path = entries[0]
    return new_path or old_path


def normalize_patch_path(path: str) -> str:
    return str(path or "").strip().replace("\\", "/").lstrip("/")


def split_patch_blocks(patch: str) -> list[list[str]]:
    """Split on Git's LF separators while preserving each block's original bytes."""
    blocks: list[list[str]] = []
    current: list[str] = []
    text = str(patch or "")
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if index < len(lines) - 1:
            line += "\n"
        elif not line:
            continue
        if line.startswith("diff --git ") and current:
            blocks.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append(current)
    return blocks
