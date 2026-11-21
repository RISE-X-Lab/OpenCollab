"""Hold one ordered group of existing local file locks for a parent process."""

import contextlib
import json
import math
import select
import sys
import time

from filelock import FileLock, Timeout


def _reply(status: str, detail: str = "") -> None:
    print(json.dumps((status, detail)), flush=True)


def main() -> None:
    setup = sys.stdin.buffer.readline()
    if not setup:
        return
    paths, timeout, poll = json.loads(setup)
    owner = select.poll()
    owner.register(int(sys.argv[1]), select.POLLIN)
    try:
        with contextlib.ExitStack() as held:
            for path, lock_path in paths:
                lock = FileLock(lock_path, timeout=0)
                deadline = time.monotonic() + timeout
                while not owner.poll(0):
                    try:
                        lock.acquire(timeout=0)
                    except Timeout:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            _reply("timeout", f"timed out acquiring write lock for {path}")
                            return
                        if owner.poll(math.ceil(min(poll, remaining) * 1000)):
                            return
                    else:
                        held.callback(lock.release)
                        break
                else:
                    return
            _reply("ready")
            owner.poll()
    except BrokenPipeError:
        return
    except OSError as exc:
        _reply("error", str(exc))


if __name__ == "__main__":
    main()
