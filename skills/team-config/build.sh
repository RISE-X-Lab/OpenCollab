#!/bin/sh
# build.sh — assemble a self-contained, interactive HTML blueprint from a team
# YAML file. Pure shell glue: it splices the team YAML into a pre-built HTML
# template (head + [your team.yaml] + body + vendored js-yaml + foot). No
# Python, no build tools, no network — just `cat`.
#
# Usage:
#   sh build.sh <team.yaml> [output.html]
#
# Defaults output to ./team-blueprint.html . Open the result in any browser.
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)   # this skill's directory
SRC=${1:-}
OUT=${2:-team-blueprint.html}

if [ -z "$SRC" ]; then
  echo "usage: sh build.sh <team.yaml> [output.html]" >&2
  exit 2
fi
if [ ! -f "$SRC" ]; then
  echo "build.sh: team file not found: $SRC" >&2
  exit 1
fi
for part in blueprint.head.html blueprint.body.html blueprint.foot.html vendor/js-yaml.min.js vendor/js-yaml.LICENSE; do
  if [ ! -f "$HERE/$part" ]; then
    echo "build.sh: missing template part: $HERE/$part" >&2
    exit 1
  fi
done

# The team YAML is embedded verbatim inside a <script type="text/yaml"> data
# island. The ONLY sequence that would prematurely close that island is a
# literal "</script" inside a prompt — vanishingly rare in a team config, but
# neutralize it defensively so a stray value can never break the page. An HTML
# script end-tag is CASE-INSENSITIVE (</SCRIPT> closes it too), so match any
# case with bracket classes — portable across GNU and BSD sed (no /I flag).
guard() { sed 's,</[Ss][Cc][Rr][Ii][Pp][Tt],<\\/script,g' "$1"; }

# Create the output directory if the caller pointed at a fresh path (e.g. the
# gitignored .opencollab/). The explicit "|| exit 1" on the redirect matters:
# busybox ash does NOT treat a redirection failure on a group command as fatal
# under `set -e`, so without it an unwritable $OUT would falsely report success.
mkdir -p "$(dirname "$OUT")" 2>/dev/null || true
{
  cat "$HERE/blueprint.head.html"
  guard "$SRC"
  cat "$HERE/blueprint.body.html"
  printf '%s\n' '<!-- js-yaml license notice'
  cat "$HERE/vendor/js-yaml.LICENSE"
  printf '%s\n' '-->'
  cat "$HERE/vendor/js-yaml.min.js"
  cat "$HERE/blueprint.foot.html"
} > "$OUT" || { echo "build.sh: cannot write output: $OUT" >&2; exit 1; }

echo "wrote $OUT ($(wc -c < "$OUT" | tr -d ' ') bytes) — open it in a browser."
