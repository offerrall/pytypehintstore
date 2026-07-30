"""One process owns a store.

The lockfile holds the owner's PID and stays open while the lock is held, so a
crash leaves an orphan the next process takes over, and two processes racing
for that orphan are settled by the filesystem rather than by agreement.
"""

import ctypes
import os
import re
from pathlib import Path

from pytypehintstore.errors import StoreLockedError

# str.isdigit() is not this question: it says yes to a superscript, which int()
# then refuses, and to an Arabic-Indic digit, which int() reads as a number no
# process here ever had.
_PID = re.compile(r"[0-9]{1,9}")


def lock_path_of(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


def acquire(path: Path, name: str):
    """Take the lock for `path`, or say who owns it. Returns the held lock."""
    lock = lock_path_of(path)

    # Two rounds at most: create, and if a stale file was in the way, remove it
    # and create again. A third failure means someone else won the race, and
    # that someone is a live owner worth reporting.
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            pass
        else:
            os.write(fd, str(os.getpid()).encode("utf-8"))

            # The descriptor stays open while the lock is held. Two processes
            # can read the same orphan and both decide to take it, and the
            # loser's unlink would otherwise delete the winner's brand new
            # lockfile, leaving two live owners. An open file cannot be
            # unlinked.
            return lock, fd

        pid = _owner(lock)

        if pid is not None and alive(pid):
            raise StoreLockedError(
                f"{name} is owned by process {pid}. A store belongs to one "
                f"process; you are probably running several workers. Run a "
                f"single worker, or reach for a database server — this is not one.")

        # An orphan from a crash, or a lockfile whose contents say nothing.
        try:
            os.unlink(lock)
        except OSError:
            # Gone already, or held open by the process that took it while we
            # were deciding it was abandoned. O_EXCL settles the next round.
            pass

    raise StoreLockedError(
        f"{name} is locked by another process that keeps retaking "
        f"{lock.name}. Stop it, or delete {lock.name} by hand.")


def release(held) -> None:
    lock, fd = held

    # The handle goes first: on Windows the file cannot be unlinked while it
    # is open, and a lock nobody can remove outlives its owner.
    try:
        os.close(fd)
    except OSError:
        pass

    try:
        os.unlink(lock)
    except OSError:
        # Removed by hand, or the disk is gone: neither is worth failing a
        # close over.
        pass


def _owner(lock: Path):
    try:
        text = lock.read_bytes().decode("utf-8", "replace").strip()
    except OSError:
        return None

    return int(text) if _PID.fullmatch(text) else None


def alive(pid: int) -> bool:
    if pid <= 0:
        return False

    if os.name == "nt":
        return _alive_windows(pid)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Someone else's process, and it exists.
        return True

    return True


# os.kill is not an option on Windows: signal 0 is not a probe there, it reaches
# TerminateProcess. Ask the kernel directly instead.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_ERROR_ACCESS_DENIED = 5
_WAIT_OBJECT_0 = 0

if os.name == "nt":
    # A handle is a pointer, and the default return type would cut a large one
    # in half — silently, since the halves of a handle look like a handle.
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
    _kernel32.OpenProcess.restype = ctypes.c_void_p
    _kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
    _kernel32.WaitForSingleObject.restype = ctypes.c_ulong
    _kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    _kernel32.CloseHandle.restype = ctypes.c_int


def _alive_windows(pid: int) -> bool:
    handle = _kernel32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, pid)

    if not handle:
        # Denied means the process is there and belongs to someone else;
        # anything else (no such PID) means it is gone.
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED

    try:
        # A process that has ended is signalled, and waiting zero milliseconds
        # asks that without waiting. Its exit code cannot answer: 259 is both
        # "exited with 259" and "still running", and that lock would never be
        # recovered.
        return _kernel32.WaitForSingleObject(handle, 0) != _WAIT_OBJECT_0
    finally:
        _kernel32.CloseHandle(handle)
