"""Hold one ordered group of existing local file locks for a parent process."""

import contextlib
import json
import select
import sys
import time

from filelock import FileLock, Timeout


def _parent_closed(wait: float | None = 0) -> bool:
    # The parent sends one setup message, then keeps its pipe open as ownership.
    readable, _, _ = select.select([sys.stdin.buffer], [], [], wait)
    return bool(readable)


def _reply(status: str, detail: str = "") -> None:
    print(json.dumps((status, detail)), flush=True)


def main() -> None:
    setup = sys.stdin.buffer.readline()
    if not setup:
        return
    paths, timeout, poll = json.loads(setup)
    try:
        with contextlib.ExitStack() as held:
            for path, lock_path in paths:
                lock = FileLock(lock_path, timeout=0)
                deadline = time.monotonic() + timeout
                while not _parent_closed():
                    try:
                        lock.acquire(timeout=0)
                    except Timeout:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            _reply("timeout", f"timed out acquiring write lock for {path}")
                            return
                        if _parent_closed(min(poll, remaining)):
                            return
                    else:
                        held.callback(lock.release)
                        break
                else:
                    return
            _reply("ready")
            _parent_closed(None)
    except BrokenPipeError:
        return
    except OSError as exc:
        _reply("error", str(exc))


if __name__ == "__main__":
    main()
