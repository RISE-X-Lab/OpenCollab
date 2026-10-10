"""Export a committed example with its matching OpenCollab wheel."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="HEAD", help="Git revision to export")
    parser.add_argument("--output", type=Path, required=True, help="New ZIP path")
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        parser.error("output already exists; choose a new path")
    repo = Path(__file__).resolve().parents[2]
    revision = subprocess.check_output(
        ["git", "rev-parse", "--verify", args.ref + "^{commit}"], cwd=repo, text=True
    ).strip()
    with tempfile.TemporaryDirectory(prefix="weave-submission-") as temporary:
        root = Path(temporary)
        source = root / "source"
        source.mkdir()
        archive = root / "source.tar"
        subprocess.run(
            ["git", "archive", "--format=tar", "-o", str(archive), revision],
            cwd=repo, check=True,
        )
        subprocess.run(["tar", "-xf", str(archive), "-C", str(source)], check=True)
        package = root / "submission"
        shutil.copytree(source / "examples/arcbench-evolution-r6", package)
        wheels = package / "wheels"
        subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(wheels)],
            cwd=source, check=True,
        )
        wheel, = wheels.glob("opencollab-*.whl")
        (package / "requirements.txt").write_text(
            f"./wheels/{wheel.name}\n./platform/arcbench-agent-runtime\n",
            encoding="utf-8",
        )
        (package / "SUBMISSION.json").write_text(
            json.dumps({"source_commit": revision, "workflow": "weave",
                        "entry": "main.py"}, indent=2) + "\n", encoding="utf-8",
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output, "x", zipfile.ZIP_DEFLATED) as bundle:
            for item in sorted(package.rglob("*")):
                if item.is_file():
                    bundle.write(item, item.relative_to(package).as_posix())
        with zipfile.ZipFile(output) as bundle:
            assert bundle.testzip() is None
            for item in bundle.namelist():
                assert bundle.read(item) == (package / item).read_bytes()
        print(json.dumps({"zip": str(output), "source_commit": revision,
                          "files": len(bundle.namelist())}))


if __name__ == "__main__":
    main()
