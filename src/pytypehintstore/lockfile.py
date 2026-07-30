"""One process owns a store.

The lockfile holds the owner's PID, and who holds it is settled by the operating
system rather than by agreement between readers: an open handle on Windows, an
advisory `flock` on POSIX. A crash leaves an orphan the next process takes over,
and two processes racing for that orphan cannot both end up owning it.

The contract is the same on both: one owner, orphans reclaimed, and the same
error naming the PID that holds it.
"""

import ctypes
import os
import re
import sys
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

    # Two rounds at most: claim, and if something stale was in the way, clear it
    # and claim again. A third failure means someone else won the race, and that
    # someone is a live owner worth reporting.
    for _ in range(2):
        fd = _claim(lock)

        if fd is not None:
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode("utf-8"))
            return lock, fd

        pid = _owner(lock)

        if pid is not None and alive(pid):
            raise StoreLockedError(
                f"{name} is owned by process {pid}. A store belongs to one "
                f"process; you are probably running several workers. Run a "
                f"single worker, or reach for a database server — this is not one.")

        _clear(lock)

    raise StoreLockedError(
        f"{name} is locked by another process that keeps retaking "
        f"{lock.name}. Stop it, or delete {lock.name} by hand.")


def release(held) -> None:
    lock, fd = held

    try:
        _let_go(lock, fd)
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
    """Whether a process with this id is running.

    A id of zero or less is nobody: on POSIX it would reach a process group
    instead of a process.
    """
    return pid > 0 and _running(pid)


# The two halves below hold one contract each way. They are declared under
# `sys.platform` rather than `os.name` because that is the form a type checker
# reads as "this is for that platform only", so neither half is looked up on
# the system that does not have it.

if sys.platform == "win32":
    # os.kill is not a probe on Windows: signal 0 reaches TerminateProcess.
    # Ask the kernel instead — and tell ctypes what it is calling, because a
    # handle is a pointer and the default return type would cut a large one in
    # half, silently, since the halves of a handle look like a handle.
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _SYNCHRONIZE = 0x00100000
    _ERROR_ACCESS_DENIED = 5
    _WAIT_OBJECT_0 = 0

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
    _kernel32.OpenProcess.restype = ctypes.c_void_p
    _kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
    _kernel32.WaitForSingleObject.restype = ctypes.c_ulong
    _kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    _kernel32.CloseHandle.restype = ctypes.c_int

    def _running(pid: int) -> bool:
        handle = _kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, pid)

        if not handle:
            # Denied means the process is there and belongs to someone else;
            # anything else (no such PID) means it is gone.
            return ctypes.get_last_error() == _ERROR_ACCESS_DENIED

        try:
            # A process that has ended is signalled, and waiting zero
            # milliseconds asks that without waiting. Its exit code cannot
            # answer: 259 is both "exited with 259" and "still running", and
            # that lock would never be recovered.
            return _kernel32.WaitForSingleObject(handle, 0) != _WAIT_OBJECT_0
        finally:
            _kernel32.CloseHandle(handle)

    def _claim(lock: Path):
        """O_EXCL decides, and the handle stays open for as long as the lock is
        held: Windows refuses to unlink an open file, so the loser of a race
        cannot delete the winner's brand new lockfile."""
        try:
            return os.open(lock, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError:
            return None

    def _clear(lock: Path) -> None:
        try:
            os.unlink(lock)
        except OSError:
            # Gone already, or held open by the process that took it while we
            # were deciding it was abandoned. O_EXCL settles the next round.
            pass

    def _let_go(lock: Path, fd: int) -> None:
        # The handle first: the file cannot be unlinked while it is open, and a
        # lock nobody can remove outlives its owner.
        os.close(fd)
        os.unlink(lock)

else:
    import fcntl

    def _running(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # Someone else's process, and it exists.
            return True

        return True

    def _claim(lock: Path):
        """`flock` decides, and the kernel drops it when the process dies.

        An orphan therefore needs no stealing: it is simply a file nobody
        holds, and the next claim takes it. The inode is checked afterwards
        because another process may have unlinked the file between the open and
        the lock, and a lock on a file with no name guards nothing.
        """
        fd = os.open(lock, os.O_CREAT | os.O_RDWR)

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

            if os.fstat(fd).st_ino != os.stat(lock).st_ino:
                raise OSError("the lockfile was replaced while it was claimed")
        except OSError:
            os.close(fd)
            return None

        return fd

    def _clear(lock: Path) -> None:
        # Nothing to clear: what makes an orphan an orphan is that its flock
        # died with its owner, and unlinking here would only race with whoever
        # is about to claim it.
        pass

    def _let_go(lock: Path, fd: int) -> None:
        # The name first, while the lock is still ours: between the unlink and
        # the close, a claim can only create a file of its own and win it
        # outright, which is exactly what should happen.
        os.unlink(lock)
        os.close(fd)
