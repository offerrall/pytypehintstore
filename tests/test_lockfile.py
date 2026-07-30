"""The owner: one process holds a store, and everyone else is told so.

A store keeps its whole state in memory and rewrites the file from it, so two
processes on one path do not share rows — they take turns overwriting them. The
lockfile turns that into an error at startup instead of a silent loss hours
later, and the lock is the owner's PID written into `<file>.lock`.

Three things have to hold at once for that to be worth anything:

  - a live owner is named, so the second process learns who to stop;
  - a lockfile left by a crash is taken over, so one bad night does not make a
    path unusable forever;
  - and taking over is decided by the filesystem rather than agreed between
    readers. The owner keeps its descriptor open for as long as it holds the
    lock: two processes may both read the same orphan and both decide it is
    theirs, and without that open descriptor the loser's `unlink` would delete
    the winner's brand new lockfile, leaving two live owners of one file.

Children run in real processes. A PID that is certainly dead cannot be faked,
and a descriptor cannot be held open by a thread on another process's behalf.
Every wait is bounded, so a hang is reported instead of sat in.

Not here: the dict, the file and its loading, the writer thread and what
shutdown owes the file. The one shutdown question that is the lock's own — a
store that fails to load must not leave the lock behind — is.
"""

import os
import textwrap
import threading
from pathlib import Path

import pytest

from pytypehintstore import StoreLoadError, StoreLockedError
from pytypehintstore.lockfile import alive, lock_path_of
from shared import Task

TESTS = Path(__file__).resolve().parent

# A child answers in a fraction of a second; anything near this is a hang, and a
# hang has to fail the test rather than stop the suite.
DEADLINE = 10.0


# ---- what the store says ---------------------------------------------------

# The two refusals, quoted whole. A test that only looked for "owned by process"
# would pass on a message that had lost the path, the advice, or both — and the
# message is the entire product of a failed acquire.

def owned(name, pid: int) -> str:
    return (f"{name} is owned by process {pid}. A store belongs to one "
            f"process; you are probably running several workers. Run a "
            f"single worker, or reach for a database server — this is not one.")


def retaken(name, lock_name: str) -> str:
    return (f"{name} is locked by another process that keeps retaking "
            f"{lock_name}. Stop it, or delete {lock_name} by hand.")


# ---- children --------------------------------------------------------------

def child(body: str) -> str:
    """A child script that can import the suite's dataclasses."""
    return f"import sys\nsys.path.insert(0, {str(TESTS)!r})\n" + textwrap.dedent(body)


# Opens the store, says so, and then does nothing at all: the lock is held for
# as long as the process lives, and the test decides how it ends.
HOLDER = """
    import time

    from shared import Task
    from pytypehintstore import store_of

    store = store_of(Task, {path!r})
    print("holding", flush=True)
    time.sleep(60)
"""


def heard_from(spoken):
    """Collect one line from a child's stdout without blocking the test on it.

    `readline` on a child that died says nothing and says it forever. Reading on
    a thread turns "wait for the child to speak" into a predicate `until` can
    poll with a deadline.
    """
    heard = []
    reader = threading.Thread(target=lambda: heard.append(spoken.readline()),
                              daemon=True)
    reader.start()
    return heard


def dead_pid(run_python):
    """The PID of a child that ran to the end, or a reason to skip.

    A number is only certainly dead if it was ours to watch. Windows hands PIDs
    out again quickly, so the death is confirmed rather than assumed, and a
    reused number skips the test instead of turning it into a lie.
    """
    done = run_python("import os; print(os.getpid())")
    pid = int(done.stdout.strip())

    if alive(pid):
        pytest.skip(f"pid {pid} was handed out again before the test could use it")

    return pid


# ---- who owns it -----------------------------------------------------------

@pytest.fixture
def store_file(file_of, store_dir):
    """The file this module's own class lands on."""
    return file_of(Task, store_dir)


def test_a_second_store_on_one_path_in_this_process_is_refused(
        open_store, store_dir, store_file):
    """Our own PID is alive, so the second store is refused like any other."""
    open_store(Task, store_dir)

    with pytest.raises(StoreLockedError) as info:
        open_store(Task, store_dir)

    assert str(info.value) == owned(store_file, os.getpid())


def test_a_second_process_is_refused_and_told_the_pid_that_holds_the_lock(
        open_store, store_dir, run_python, tmp_path, store_file):
    """The child writes what it was told to a file: the message carries an em
    dash, and a pipe decoded with the locale encoding is not where an exact
    comparison of text belongs."""
    open_store(Task, store_dir)
    said = tmp_path / "refusal.txt"

    run_python(child(f"""
        from pathlib import Path

        from shared import Task
        from pytypehintstore import store_of, StoreLockedError

        try:
            store_of(Task, {str(store_dir)!r})
        except StoreLockedError as error:
            Path({str(said)!r}).write_text(str(error), encoding="utf-8")
        else:
            Path({str(said)!r}).write_text(
                "the child opened a store the parent was holding",
                encoding="utf-8")
    """))

    assert said.read_text(encoding="utf-8") == owned(store_file, os.getpid())


def test_the_lockfile_holds_the_pid_of_the_store_that_owns_it(
        open_store, store_dir):
    """The PID is on disk while the owner lives, not only when it lets go: it is
    what the next process reads to decide whether the lock is an orphan."""
    store = open_store(Task, store_dir)

    assert lock_path_of(store.path).read_text(encoding="utf-8") == str(os.getpid())


def test_a_lock_left_behind_by_a_dead_process_is_taken_over(
        open_store, store_dir, run_python, store_file):
    """A crash leaves the file; nothing at all removes it. The next process
    reads a PID that no longer runs and takes the path over."""
    lock = lock_path_of(store_file)
    lock.write_text(str(dead_pid(run_python)), encoding="utf-8")

    store = open_store(Task, store_dir)

    assert lock_path_of(store.path).read_text(encoding="utf-8") == str(os.getpid())


@pytest.mark.parametrize("content", [
    pytest.param("abc", id="letters"),
    pytest.param("", id="empty"),
    pytest.param(" ", id="a space"),
    pytest.param("-1", id="negative"),
    pytest.param("⁵", id="superscript five"),
    pytest.param("١٢", id="arabic-indic twelve"),
    pytest.param("1" * 5000, id="5000 digits"),
])
def test_a_lockfile_that_names_no_process_is_taken_over(
        open_store, store_dir, content, store_file):
    """A lockfile that does not name a PID names nobody, and nobody is not an
    owner worth keeping a path locked for.

    The last three used to be worse than a wrong answer. `str.isdigit()` is not
    the question `int()` answers: it says yes to a superscript, which `int()`
    then refuses with a bare ValueError; yes to Arabic-Indic digits, which
    `int()` reads as 12 — a PID no process here ever had, and one that could
    belong to a stranger; and yes to five thousand digits, which trip CPython's
    limit on integer conversion. All three escaped `acquire` as something other
    than a StoreError.
    """
    lock = lock_path_of(store_file)
    lock.write_text(content, encoding="utf-8")

    store = open_store(Task, store_dir)

    assert lock_path_of(store.path).read_text(encoding="utf-8") == str(os.getpid())


def test_a_lock_that_keeps_coming_back_is_reported_as_retaken(
        monkeypatch, open_store, store_dir, store_file):
    """A rival that keeps the lock out of reach is refused, not looped on.

    Whatever settles ownership on this platform — `O_EXCL` over a handle Windows
    will not unlink, an advisory `flock` on POSIX — a rival fast enough to win
    every round would leave `acquire` spinning forever. The loop is bounded
    instead, and what comes out is a refusal naming the file to delete by hand.

    The claim is what is impersonated, because it is the one step both platforms
    agree on: it either hands back a descriptor or says the lock is not ours.
    Everything else in `acquire` — reading the PID, judging it, the bound on the
    loop — is the real thing.
    """
    import pytypehintstore.lockfile as lockfile

    lock = lock_path_of(store_file)
    lock.write_text("not a pid", encoding="utf-8")

    monkeypatch.setattr(lockfile, "_claim", lambda held: None)

    with pytest.raises(StoreLockedError) as info:
        open_store(Task, store_dir)

    assert str(info.value) == retaken(store_file, lock.name)


# ---- giving it back --------------------------------------------------------

def test_close_gives_the_lockfile_back(open_store, store_dir):
    """The file is the lock. A close that kept it would own the path for the
    rest of the process."""
    store = open_store(Task, store_dir)
    lock = lock_path_of(store.path)
    assert lock.is_file(), "the store never took its lock"

    store.close()

    assert not lock.exists()


def test_the_path_takes_a_new_store_once_the_first_one_closes(
        open_store, store_dir):
    """Closing is not only a promise about the file: the path has to be usable
    again straight away, by this process included."""
    first = open_store(Task, store_dir)
    first.close()

    again = open_store(Task, store_dir)

    assert lock_path_of(again.path).read_text(encoding="utf-8") == str(os.getpid())


@pytest.mark.parametrize("broken", ["json", "directory"])
def test_a_store_that_cannot_read_its_file_leaves_no_lockfile(store_dir, broken, store_file):
    """A store that never opened must not leave the path locked.

    The lock is taken before the file is read, and the two ways `_load` gives up
    are a file that is not JSON and a path that cannot be read at all. Either
    one leaving the lockfile behind would lock the path for the rest of the
    process, with no store in existence to close.
    """
    from pytypehintstore import store_of

    if broken == "json":
        store_file.write_text("{not json at all", encoding="utf-8")
    else:
        store_file.mkdir()

    with pytest.raises(StoreLoadError):
        store_of(Task, str(store_dir))

    assert not lock_path_of(store_file).exists()


# ---- is that PID a process? ------------------------------------------------

@pytest.mark.parametrize("pid", [0, -1])
def test_alive_is_false_for_a_number_no_process_can_have(pid):
    """The guard is not decorative. On POSIX `kill(0, 0)` signals the whole
    process group and `kill(-1, 0)` every process the caller can reach, so a
    lockfile carrying either number would be answered by signalling, not by
    asking."""
    assert alive(pid) is False


def test_alive_is_true_for_the_process_asking():
    """The one PID whose answer is known without asking anything."""
    assert alive(os.getpid()) is True


@pytest.mark.skipif(os.name != "nt",
                    reason="259 is STILL_ACTIVE, a Windows number: POSIX keeps "
                           "the low byte of an exit code and reports 3")
def test_alive_is_false_for_a_child_that_exited_with_the_still_running_code(
        spawn_python):
    """A finished process is dead however it chose to say goodbye.

    259 is the exit code that is also STILL_ACTIVE, the number the kernel
    returns for a process that has not finished. Reading the exit code could
    not tell the two apart, and a store whose owner left with 259 kept a lock
    nobody could ever recover — permanently locked, delete it by hand.

    The Popen object still holds a handle to the child, and Windows does not
    hand a PID out again while a handle to that process object is open. So the
    number really is this corpse's and nobody else's: no skip is needed, and no
    guess is being made.
    """
    corpse = spawn_python("import sys; sys.exit(259)")
    assert corpse.wait(timeout=DEADLINE) == 259

    assert alive(corpse.pid) is False


# ---- an owner in another process -------------------------------------------

def test_a_live_owner_in_another_process_is_named_by_its_pid(
        open_store, store_dir, spawn_python, until, store_file):
    """The refusal has to name the process that is actually holding the file,
    not merely refuse. The PID is the whole remedy: it is what the reader stops."""
    holder = spawn_python(child(HOLDER.format(path=str(store_dir))))
    said = heard_from(holder.stdout)
    assert until(lambda: said and said[0].strip() == "holding", timeout=DEADLINE), (
        "the child never reported taking the lock")

    with pytest.raises(StoreLockedError) as info:
        open_store(Task, store_dir)

    assert str(info.value) == owned(store_file, holder.pid)


def test_the_lock_of_an_owner_that_was_killed_is_taken_over(
        open_store, store_dir, spawn_python, until):
    """A killed owner runs no cleanup: the lockfile survives it, still naming
    it. That orphan is the signal, and the next store reads it and takes over."""
    holder = spawn_python(child(HOLDER.format(path=str(store_dir))))
    said = heard_from(holder.stdout)
    assert until(lambda: said and said[0].strip() == "holding", timeout=DEADLINE), (
        "the child never reported taking the lock")

    holder.kill()
    holder.wait(timeout=DEADLINE)
    assert until(lambda: not alive(holder.pid), timeout=DEADLINE), (
        "the killed child was still a process")

    store = open_store(Task, store_dir)

    assert lock_path_of(store.path).read_text(encoding="utf-8") == str(os.getpid())


# ---- the race the two platforms settle differently and answer alike --------

# Two processes that reach for the same orphan at the same instant. They wait on
# a wall-clock deadline rather than on each other, because the point is to
# overlap inside `acquire`, and a handshake would serialise exactly the window
# under test.
RACER = """
    import time

    from shared import Task
    from pytypehintstore import store_of, StoreLockedError

    while time.time() < {start!r}:
        pass

    try:
        store = store_of(Task, {directory!r})
    except StoreLockedError:
        print("locked", flush=True)
    else:
        print("owns", flush=True)
        time.sleep(1.0)
        store.close()
"""


def test_two_processes_reaching_for_one_orphan_leave_a_single_owner(
        store_dir, file_of, lock_of, spawn_python):
    """The property both platforms have to hold, by different means.

    Windows settles it with O_EXCL over a handle that cannot be unlinked while
    it is open; POSIX with an advisory flock the kernel drops when a process
    dies, plus an inode check for the file being replaced underneath. What a
    caller sees is the same either way: one owner, and everyone else told so.

    The lockfile says nothing, which is the honest way to make an orphan — a
    dead PID could be recycled between writing it and reading it.
    """
    import time

    lock = lock_of(file_of(Task, store_dir))
    lock.write_text("not a pid at all", encoding="utf-8")

    start = time.time() + 1.0
    racers = [spawn_python(child(RACER.format(start=start,
                                              directory=str(store_dir))))
              for _ in range(2)]

    verdicts = sorted(racer.communicate(timeout=DEADLINE)[0].strip()
                      for racer in racers)

    assert verdicts == ["locked", "owns"], (
        f"the orphan was claimed by {verdicts.count('owns')} processes")


# ---- the descriptor the owner keeps ----------------------------------------

@pytest.mark.skipif(os.name != "nt",
                    reason="POSIX unlinks an open file happily; only Windows "
                           "refuses, and the refusal is what is under test")
def test_another_process_cannot_delete_the_lockfile_of_an_open_store(
        open_store, store_dir, run_python):
    """The owner holds its descriptor open, and the filesystem defends the file.

    This is the whole of the two-owners fix. Without the open descriptor: two
    processes read the same orphan, both call it abandoned, both unlink, and the
    loser's unlink removes the lockfile the winner has just created — after
    which the loser's O_EXCL succeeds and one file has two live owners. With the
    descriptor held, an outsider can still read the PID but its unlink is
    refused, and O_EXCL is left to answer the only question that settles
    ownership.

    A second process is the only way to ask: the refusal is between processes,
    and this one's own handle would not be refused anything.
    """
    store = open_store(Task, store_dir)
    lock = lock_path_of(store.path)

    attempt = child(f"""
        import os

        try:
            os.unlink({str(lock)!r})
        except PermissionError:
            print("refused")
        except FileNotFoundError:
            print("gone")
        else:
            print("removed")
    """)

    while_held = run_python(attempt)
    store.close()
    once_closed = run_python(attempt)

    assert (while_held.stdout.strip(), once_closed.stdout.strip()) == ("refused", "gone")


def test_release_closes_the_descriptor_it_was_holding(open_store, store_dir):
    """A descriptor left open would survive the store that opened it.

    On Windows that is not a leak that only wastes a handle: `release` unlinks
    the file, and its own unlink would be refused. The path would keep a
    lockfile naming a PID that is very much alive — this process — and nothing
    short of ending the process could free it. That the name is gone, and that
    it can be taken and given back again, is the proof the descriptor closed.
    """
    store = open_store(Task, store_dir)
    lock = lock_path_of(store.path)
    store.close()

    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(fd)
    os.unlink(lock)

    assert not lock.exists()
