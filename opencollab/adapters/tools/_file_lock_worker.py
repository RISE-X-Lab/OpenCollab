"""Hold one ordered group of existing local file locks for a parent process."""

import contextlib
import errno
import fcntl
import json
import math
import os
import select
import sys
import time
from collections.abc import Iterator

from filelock import FileLock, Timeout


@contextlib.contextmanager
def _try_file_lock(path: str, *, shared: bool = False) -> Iterator[FileLock | None]:
    if shared:
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o666)
        except FileNotFoundError as exc:
            if os.path.isdir(os.path.dirname(path)):
                raise Timeout(path) from exc
            raise
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Timeout(path) from exc
            except OSError as exc:
                if exc.errno != errno.ENOSYS:
                    raise
            else:
                try:
                    # FileLock can unlink its inode between open and flock.
                    if not os.fstat(fd).st_nlink:
                        raise Timeout(path)
                    yield None
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                return
        finally:
            # Other shared holders still coordinate through this inode.
            os.close(fd)

    with FileLock(path, timeout=0) as lock:
        yield lock


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
            for path, lock_path, shared in paths:
                deadline = time.monotonic() + timeout
                while not owner.poll(0):
                    try:
                        held.enter_context(_try_file_lock(lock_path, shared=shared))
                    except Timeout:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            _reply("timeout", f"timed out acquiring write lock for {path}")
                            return
                        if owner.poll(math.ceil(min(poll, remaining) * 1000)):
                            return
                    else:
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
