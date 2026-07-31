"""Tests for the ``team-config`` skill (skills/team-config/).

The skill ships a pure-``sh`` renderer (``build.sh``) that splices a team YAML
into a self-contained, interactive HTML blueprint — no Python at runtime. These
tests exercise the shell glue via a subprocess and cross-check the shipped
``template.team.yaml`` against OpenCollab's *real* team-config loader so the
template can never drift out of the schema it teaches.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "skills" / "team-config"
BUILD_SH = SKILL_DIR / "build.sh"
TEMPLATE = SKILL_DIR / "template.team.yaml"
JS_YAML = SKILL_DIR / "vendor/js-yaml.min.js"
JS_YAML_LICENSE = SKILL_DIR / "vendor/js-yaml.LICENSE"

# Exact files from the upstream js-yaml 4.1.1 tag.
_JS_YAML_DIST_SHA256 = "0de3dec92d20eab9e0b46a5d928cd45ec025d73e348ddf458dbfb01da00cb473"
_JS_YAML_LICENSE_SHA256 = "a07bc24468b9654ce76a547d47a2db282d07733b715db4c73a98bd63961f9550"

_ISLAND = re.compile(
    r'<script id="team-src" type="text/yaml">\n(.*?)\n</script>', re.S
)
_SCRIPT = re.compile(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>", re.S | re.I)

pytestmark = pytest.mark.skipif(
    shutil.which("sh") is None, reason="POSIX sh required to run build.sh"
)


def _build(src: Path, out: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", str(BUILD_SH), str(src), str(out)],
        capture_output=True,
        text=True,
    )


def _island_yaml(html: str) -> str:
    m = _ISLAND.search(html)
    assert m, "team.yaml data island not found in output HTML"
    # build.sh neutralizes a literal </script inside the island; undo it to parse.
    return m.group(1).replace("<\\/script", "</script")


def test_skill_files_present() -> None:
    for name in (
        "SKILL.md",
        "build.sh",
        "template.team.yaml",
        "blueprint.head.html",
        "blueprint.body.html",
        "blueprint.foot.html",
        "vendor/js-yaml.min.js",
        "vendor/js-yaml.LICENSE",
    ):
        assert (SKILL_DIR / name).is_file(), f"missing skill file: {name}"


def test_vendored_js_yaml_is_patched_and_keeps_upstream_license() -> None:
    source = JS_YAML.read_bytes()
    license_text = JS_YAML_LICENSE.read_bytes()

    assert source.startswith(b"/*! js-yaml 4.1.1 ")
    assert hashlib.sha256(source).hexdigest() == _JS_YAML_DIST_SHA256
    assert b'"__proto__"===t?Object.defineProperty' in source
    assert hashlib.sha256(license_text).hexdigest() == _JS_YAML_LICENSE_SHA256


def test_build_produces_self_contained_blueprint(tmp_path: Path) -> None:
    out = tmp_path / "bp.html"
    proc = _build(TEMPLATE, out)
    assert proc.returncode == 0, proc.stderr
    html = out.read_text(encoding="utf-8")

    # exactly three script closes: yaml island, vendored js-yaml, renderer.
    assert html.count("</script>") == 3
    # self-contained: the parser is inlined, and nothing is fetched over the wire.
    assert "js-yaml 4.1.1" in html
    assert JS_YAML_LICENSE.read_text(encoding="utf-8") in html
    assert "/* js-yaml license notice\n" in html
    assert "\n*/\n/*! js-yaml 4.1.1" in html
    assert "<!-- js-yaml license notice" not in html
    assert "OpenCollab Team Blueprint" in html
    # no external subresource loads (the SVG xmlns URI is a constant, not a fetch).
    assert 'src="http' not in html
    assert 'href="http' not in html
    assert "@import" not in html
    # the embedded YAML round-trips to exactly the source structure.
    assert yaml.safe_load(_island_yaml(html)) == yaml.safe_load(
        TEMPLATE.read_text(encoding="utf-8")
    )


def test_build_embeds_syntax_valid_javascript(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js required for generated JavaScript syntax check")

    out = tmp_path / "bp.html"
    proc = _build(TEMPLATE, out)
    assert proc.returncode == 0, proc.stderr
    html = out.read_text(encoding="utf-8")

    scripts = [
        match.group("body")
        for match in _SCRIPT.finditer(html)
        if 'type="text/yaml"' not in match.group("attrs")
    ]
    assert len(scripts) == 2
    for index, script in enumerate(scripts, start=1):
        checked = subprocess.run(
            [node, "--check", "-"],
            input=script,
            capture_output=True,
            text=True,
        )
        assert checked.returncode == 0, (
            f"generated JavaScript block {index} is invalid:\n{checked.stderr}"
        )


def test_build_neutralizes_script_close(tmp_path: Path) -> None:
    src = tmp_path / "tricky.yaml"
    src.write_text(
        "entry: lead\n"
        "roles:\n"
        "  lead:\n"
        "    tools: [bash, spawn_agent]\n"
        "    prompt: |\n"
        "      Contains a literal </script></SCRIPT> plus a colon: value and <tags>.\n"
        "  worker:\n"
        "    tools: [bash]\n"
        "    prompt: do work\n"
        "topology:\n"
        "  lead: [worker]\n",
        encoding="utf-8",
    )
    out = tmp_path / "bp.html"
    proc = _build(src, out)
    assert proc.returncode == 0, proc.stderr
    html = out.read_text(encoding="utf-8")

    # HTML script end-tags are case-insensitive: only the 3 real closes (island,
    # js-yaml, renderer) may remain; the </script></SCRIPT> in the prompt must be
    # neutralized in BOTH cases so a browser can't close the island early.
    real_closes = re.findall(r"</[ \t]*script", html, re.IGNORECASE)
    assert len(real_closes) == 3, real_closes
    assert "<\\/script" in html  # guard applied
    # The island still parses, and its STRUCTURE survives (only the pathological
    # script-tag text inside the prompt is deliberately neutralized).
    parsed = yaml.safe_load(_island_yaml(html))
    assert parsed["entry"] == "lead"
    assert set(parsed["roles"]) == {"lead", "worker"}
    assert parsed["topology"] == {"lead": ["worker"]}


def test_build_usage_errors(tmp_path: Path) -> None:
    missing_arg = subprocess.run(["sh", str(BUILD_SH)], capture_output=True, text=True)
    assert missing_arg.returncode == 2

    absent = subprocess.run(
        ["sh", str(BUILD_SH), str(tmp_path / "nope.yaml")],
        capture_output=True,
        text=True,
    )
    assert absent.returncode == 1


@pytest.mark.parametrize("cfg", ["team.example.yaml"])
def test_shipped_team_configs_render(cfg: str, tmp_path: Path) -> None:
    src = REPO_ROOT / "configs" / cfg
    out = tmp_path / "bp.html"
    proc = _build(src, out)
    assert proc.returncode == 0, proc.stderr
    html = out.read_text(encoding="utf-8")
    assert html.count("</script>") == 3
    assert yaml.safe_load(_island_yaml(html)) == yaml.safe_load(
        src.read_text(encoding="utf-8")
    )


def test_template_is_valid_per_real_loader() -> None:
    """The shipped template must load under OpenCollab's actual team-config
    parser and use only real tool names — guards against schema drift."""
    from opencollab.bootstrap.team_config import _build_team_config
    from opencollab.bootstrap.tool_registry import KNOWN_TOOL_NAMES

    data = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    team = _build_team_config(data, TEMPLATE.parent)

    assert team.entry in team.roles
    assert team.roles, "template declares no roles"
    for name, role in team.roles.items():
        assert role.prompt, f"role {name} has an empty prompt"
        unknown = set(role.tools) - KNOWN_TOOL_NAMES
        assert not unknown, f"role {name} names unknown tools: {sorted(unknown)}"
    # every topology endpoint the template wires is a declared role.
    for src, dsts in team.topology.edges.items():
        assert src in team.roles
        for dst in dsts:
            assert dst in team.roles, f"topology target {dst!r} is not a declared role"
