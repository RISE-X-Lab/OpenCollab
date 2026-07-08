#!/usr/bin/env python3
"""G1.1 command entry for the SWE Pro-Lite runner."""

import importlib.util
import sys
from pathlib import Path


def main() -> int:
    legacy_runner = Path(__file__).with_name("swe_v1_prolite_runner.py")
    spec = importlib.util.spec_from_file_location("swe_v1_prolite_runner", legacy_runner)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    sys.argv[0] = str(Path(__file__))
    return int(module.main())


if __name__ == "__main__":
    raise SystemExit(main())
