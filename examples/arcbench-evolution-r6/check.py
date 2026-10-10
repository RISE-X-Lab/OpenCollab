"""Check a prepared ARC-Bench application using public requirement inputs."""

import argparse
import json
import signal
from pathlib import Path
from threading import Event

import yaml
from arc_light.delivery import capture_baseline, verify
from arc_light.public_checks import write_public_checks
from arc_light.reports import write_json
from arc_light.spec import write_cards


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--requirements", type=Path)
    parser.add_argument("--requirement-id", action="append", default=None)
    parser.add_argument("--initialize", action="store_true", help="Capture the original input once, before editing")
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--confirm-stability", action="store_true")
    args = parser.parse_args()
    cancelled = Event()
    previous = signal.signal(signal.SIGINT, lambda *_: cancelled.set())
    try:
        if args.requirements:
            document = yaml.safe_load(args.requirements.read_text(encoding="utf-8"))
            write_cards(document, args.workspace, directory_name=".arc/evolution-spec/by-id")
            write_public_checks(document, args.workspace, requirement_ids=args.requirement_id)
        elif args.requirement_id:
            parser.error("--requirement-id needs --requirements")
        if args.initialize:
            capture_baseline(args.workspace, input_source=args.requirements)
        report = verify(
            args.workspace, timeout=args.timeout, cancel_event=cancelled, confirm_stability=args.confirm_stability
        )
        print(json.dumps({"ok": report["ok"], "report": str(args.workspace / ".arc/checks/verification.json")}))
        return 130 if cancelled.is_set() else 0 if report["ok"] else 1
    except (OSError, ValueError) as exc:
        report = {
            "ok": False,
            "input_error": str(exc),
            "checks": [{"step": "verification_inputs", "ok": False, "repairable": False, "detail": str(exc)}],
        }
        write_json(args.workspace / ".arc/checks/verification.json", report)
        print(json.dumps({"ok": False, "input_error": str(exc)}))
        return 2
    finally:
        signal.signal(signal.SIGINT, previous)


if __name__ == "__main__":
    raise SystemExit(main())
