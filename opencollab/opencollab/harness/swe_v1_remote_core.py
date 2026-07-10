"""Ownership, process, and durable-file primitives for the V1 remote runner."""

# ruff: noqa: F403, F405

from opencollab.harness.swe_v1_remote_state import *


class RecordInputLimitError(ValueError):
    pass


class RecordInputFormatError(ValueError):
    pass


def block_spawn_signals():
    state = {"previous": {}, "pending": [], "restored": False}

    def defer(signum, frame):
        if signum not in state["pending"]:
            state["pending"].append(signum)

    try:
        for signum in SPAWN_SIGNALS:
            state["previous"][signum] = signal.getsignal(signum)
            signal.signal(signum, defer)
    except BaseException:
        for signum, handler in state["previous"].items():
            signal.signal(signum, handler)
        raise
    return state


def restore_spawn_signals(state):
    if state.get("restored"):
        return
    for signum, handler in state["previous"].items():
        signal.signal(signum, handler)
    state["restored"] = True
    for signum in state["pending"]:
        handler = state["previous"].get(signum, signal.SIG_DFL)
        if handler == signal.SIG_IGN:
            continue
        if handler == signal.SIG_DFL:
            os.kill(os.getpid(), signum)
        else:
            handler(signum, None)


def wait_for_owned_cleanup(done, timeout):
    deadline = time.monotonic() + max(0.0, timeout)
    interruption = None
    while not done.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            done.wait(min(0.05, remaining))
        except (KeyboardInterrupt, SystemExit) as exc:
            if interruption is None:
                interruption = exc
    return done.is_set(), interruption


def consume_process_exit(proc):
    try:
        proc.wait()
    except BaseException:
        pass
    while process_group_exists(proc.pid):
        time.sleep(0.1)
    ACTIVE_CHILD_PGIDS.discard(proc.pid)


def schedule_process_exit_consumer(proc):
    threading.Thread(
        target=consume_process_exit,
        args=(proc,),
        name=f"prolite-reap-{getattr(proc, 'pid', 'unknown')}",
        daemon=True,
    ).start()


def process_group_exists(pgid):
    try:
        os.kill(-pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_for_process_group_exit(pgid, deadline):
    while process_group_exists(pgid):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.01, remaining))
    return True


def terminate_process_group_owned(proc, term_timeout, kill_timeout):
    pgid = proc.pid
    term_deadline = time.monotonic() + max(0.0, term_timeout)
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        except PermissionError:
            schedule_process_exit_consumer(proc)
            return False

    leader_reaped = False
    try:
        proc.wait(timeout=max(0.0, term_deadline - time.monotonic()))
        leader_reaped = True
    except ChildProcessError:
        leader_reaped = True
    except subprocess.TimeoutExpired:
        pass

    group_gone = wait_for_process_group_exit(pgid, term_deadline)
    if leader_reaped and group_gone:
        return True

    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except (ProcessLookupError, PermissionError):
            pass

    kill_deadline = time.monotonic() + max(0.0, kill_timeout)
    if not leader_reaped:
        try:
            proc.wait(timeout=max(0.0, kill_deadline - time.monotonic()))
            leader_reaped = True
        except ChildProcessError:
            leader_reaped = True
        except subprocess.TimeoutExpired:
            pass
    group_gone = wait_for_process_group_exit(pgid, kill_deadline)
    if not leader_reaped or not group_gone:
        schedule_process_exit_consumer(proc)
    return leader_reaped and group_gone


def terminate_process_group_bounded(
    proc,
    term_timeout=PROCESS_TERM_GRACE_SECONDS,
    kill_timeout=PROCESS_KILL_REAP_TIMEOUT_SECONDS,
):
    state = {}
    done = threading.Event()

    def cleanup():
        try:
            state["reaped"] = terminate_process_group_owned(
                proc,
                term_timeout,
                kill_timeout,
            )
        except BaseException as exc:
            state["error"] = exc
        finally:
            done.set()

    cleanup_thread = threading.Thread(
        target=cleanup,
        name=f"prolite-cleanup-{getattr(proc, 'pid', 'unknown')}",
        daemon=True,
    )
    cleanup_thread.start()
    completed, interruption = wait_for_owned_cleanup(
        done,
        term_timeout + kill_timeout + PROCESS_CLEANUP_OUTER_SLACK_SECONDS,
    )
    if completed and "reaped" in state:
        reaped = bool(state["reaped"])
    else:
        reaped = False
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except (ProcessLookupError, PermissionError):
                pass
    if interruption is not None:
        raise interruption
    return reaped


def ensure_process_group_quiesced_after_wait(
    proc,
    term_timeout=PROCESS_TERM_GRACE_SECONDS,
    kill_timeout=PROCESS_KILL_REAP_TIMEOUT_SECONDS,
):
    if not process_group_exists(proc.pid):
        return True
    return terminate_process_group_bounded(
        proc,
        term_timeout=term_timeout,
        kill_timeout=kill_timeout,
    )


def slice_label():
    end_index = start_index + max(limit, 0) - 1
    return str(start_index) if end_index <= start_index else f"{start_index}-{end_index}"


def terminate_active_children(_sig=signal.SIGTERM):
    owned = set(ACTIVE_CHILD_PGIDS)
    for pgid in owned:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            ACTIVE_CHILD_PGIDS.discard(pgid)
    term_deadline = time.monotonic() + PROCESS_TERM_GRACE_SECONDS
    for pgid in list(owned):
        if wait_for_process_group_exit(pgid, term_deadline):
            ACTIVE_CHILD_PGIDS.discard(pgid)
            owned.discard(pgid)
    for pgid in owned:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            ACTIVE_CHILD_PGIDS.discard(pgid)
    kill_deadline = time.monotonic() + PROCESS_KILL_REAP_TIMEOUT_SECONDS
    for pgid in list(owned):
        if wait_for_process_group_exit(pgid, kill_deadline):
            ACTIVE_CHILD_PGIDS.discard(pgid)
            owned.discard(pgid)
    return not owned


def cleanup_fifo(path):
    try:
        pathlib.Path(path).unlink(missing_ok=True)
    finally:
        ACTIVE_FIFO_PATHS.discard(pathlib.Path(path))


def cleanup_active_fifos():
    for path in list(ACTIVE_FIFO_PATHS):
        cleanup_fifo(path)


def signal_exit(signum, frame):
    terminate_active_children(signal.SIGTERM)
    cleanup_active_fifos()
    raise SystemExit(128 + int(signum))


def fsync_directory(path):
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_all(fd, payload):
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short harness file write")
        view = view[written:]


def open_regular_file(path, flags, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_flags = flags | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    for _attempt in range(SAFE_FILE_OPEN_RETRIES):
        try:
            before = path.lstat()
        except FileNotFoundError:
            before = None
        if before is not None and not stat.S_ISREG(before.st_mode):
            raise OSError(f"harness file must be regular: {path}")
        try:
            if before is None:
                fd = os.open(path, safe_flags | os.O_CREAT | os.O_EXCL, mode)
            else:
                fd = os.open(path, safe_flags)
        except (FileExistsError, FileNotFoundError):
            continue
        try:
            opened = os.fstat(fd)
            current = path.lstat()
            identity = (opened.st_dev, opened.st_ino)
            if (
                not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(current.st_mode)
                or (current.st_dev, current.st_ino) != identity
                or (before is not None and (before.st_dev, before.st_ino) != identity)
            ):
                os.close(fd)
                continue
            return fd
        except BaseException:
            os.close(fd)
            raise
    raise OSError(f"harness file did not stabilize while opening: {path}")


def acquire_lock(fd, label):
    deadline = time.monotonic() + HARNESS_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"timed out acquiring {label}")
        time.sleep(min(0.01, remaining))


@contextmanager
def open_locked_append(path):
    fd = open_regular_file(path, os.O_RDWR | os.O_APPEND)
    locked = False
    handle = None
    try:
        acquire_lock(fd, f"append lock {path}")
        locked = True
        handle = os.fdopen(fd, "ab", closefd=False)
        yield handle
        handle.flush()
        os.fsync(fd)
        fsync_directory(path.parent)
    finally:
        if handle is not None:
            handle.close()
        try:
            if locked:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def atomic_write_bytes(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        before = path.lstat()
    except FileNotFoundError:
        before = None
    if before is not None and not stat.S_ISREG(before.st_mode):
        raise OSError(f"harness destination must be regular or absent: {path}")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            write_all(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            current = path.lstat()
        except FileNotFoundError:
            current = None
        if before is None:
            if current is not None:
                raise OSError(f"harness destination appeared during write: {path}")
        elif (
            current is None
            or not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise OSError(f"harness destination changed during write: {path}")
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def process_start_identity(pid):
    try:
        raw = pathlib.Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        remainder = raw.rsplit(")", 1)[1].split()
        if len(remainder) > 19 and remainder[19].isdigit():
            return f"proc:{remainder[19]}"
    except (OSError, IndexError):
        pass
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    value = result.stdout.strip()
    return f"ps:{value}" if result.returncode == 0 and value else ""


def _runner_owner_record():
    try:
        context = open_regular_binary(base_run_dir / "runner.pid")
        handle = context.__enter__()
    except FileNotFoundError:
        return None
    try:
        opened = os.fstat(handle.fileno())
        if opened.st_size <= 0 or opened.st_size > 4096:
            raise RuntimeError("runner owner must be a bounded regular file")
        raw = handle.read(4097)
        if len(raw) > 4096:
            raise RuntimeError("runner owner exceeds its byte limit")
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("runner owner record is invalid") from exc
    finally:
        context.__exit__(None, None, None)
    if (
        not isinstance(value, dict)
        or value.get("schema") != "opencollab.prolite_runner_owner.v1"
        or isinstance(value.get("pid"), bool)
        or not isinstance(value.get("pid"), int)
        or value["pid"] <= 1
        or not isinstance(value.get("start_identity"), str)
        or not value["start_identity"]
        or re.fullmatch(r"[0-9a-f]{32}", str(value.get("owner_nonce") or "")) is None
    ):
        raise RuntimeError("runner owner record is invalid")
    return value


def _pid_exists(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def write_runner_pid():
    global RUNNER_LOCK_FD, RUNNER_OWNER_RECORD
    if RUNNER_LOCK_FD is not None:
        return RUNNER_OWNER_RECORD
    base_run_dir.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    start_identity = process_start_identity(pid)
    if not start_identity or not re.fullmatch(r"[0-9a-f]{32}", owner_nonce):
        raise RuntimeError("runner ownership identity could not be established")
    lock_fd = open_regular_file(base_run_dir / ".runner.lock", os.O_RDWR)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise RuntimeError("another ProLite runner owns this run directory") from exc
            raise
        existing = _runner_owner_record()
        if existing is not None:
            existing_pid = existing["pid"]
            current_identity = process_start_identity(existing_pid)
            if existing_pid == pid and existing.get("owner_nonce") == owner_nonce:
                if existing.get("start_identity") != start_identity:
                    raise RuntimeError("current runner owner identity changed")
            elif _pid_exists(existing_pid):
                if not current_identity:
                    raise RuntimeError("existing runner owner identity is unverifiable")
                if current_identity == existing["start_identity"]:
                    raise RuntimeError("a live ProLite runner already owns this run directory")
        record = {
            "schema": "opencollab.prolite_runner_owner.v1",
            "pid": pid,
            "start_identity": start_identity,
            "owner_nonce": owner_nonce,
        }
        atomic_write_bytes(
            base_run_dir / "runner.pid",
            (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"),
        )
        RUNNER_LOCK_FD = lock_fd
        RUNNER_OWNER_RECORD = record
        lock_fd = -1
        return record
    finally:
        if lock_fd >= 0:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)


def initialize_runner_ownership():
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, signal_exit)
    write_runner_pid()
    atexit.register(cleanup_active_fifos)
    atexit.register(terminate_active_children)


__all__ = [name for name in globals() if not name.startswith("__")]
