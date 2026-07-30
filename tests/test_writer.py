"""The writer thread and the way down.

The rows live in memory and a background thread carries them to the file: a
burst of writes is given `debounce` seconds to settle, a dump that fails is
tried again instead of lost, and `close()` — called by hand or by the
interpreter on the way out — takes the thread down and leaves the last state on
disk. What follows is about that thread and that shutdown and nothing else; the
shape of the file, the rotated copies and the load errors are tested elsewhere.

Two rules keep these tests fast and honest. A dump is counted, and made to
fail, at `os.replace` through the `replaces` fixture, because that is the call
the file's arrival really depends on — never by standing in for the store's own
`_write`, which is an implementation detail and not the contract. And waiting
is `until(...)`, a poll on the thing itself, never a sleep long enough to be
safe: the writer answers in milliseconds and a suite that sleeps for it spends
its whole budget waiting.

One subtlety runs through the file. `replaces` records an arrival once the file
is in place, but the store prunes its rotated copies afterwards and a reader on
Windows can still meet the replace itself. Every test therefore waits on the
file's own contents and counts the dumps afterwards, never the other way round.
"""

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from pytypehintstore import store_of
from shared import Task


@dataclass
class Row:
    """What the child scripts below store. Declared here too, so the parent can
    work out which file their store lands on."""

    title: str

# Long enough that a thread reaching it is hung rather than slow: these waits
# are for handing a failure back instead of stopping the suite.
JOIN = 5.0


def titles_in(path):
    """The titles the file carries, or None while there is no whole file yet.

    A predicate for `until` runs while the writer is working, so it can meet the
    path between the part file and the replace. Anything unreadable is "not yet"
    rather than an error.
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    return [row["title"] for row in payload["rows"].values()]


class _GatedOs:
    """The store's view of `os`, with the first fsync held until it is let go.

    `_write` fsyncs the part file before it replaces anything, and it does so
    outside the store's lock — which is exactly where "the writer is mid-dump"
    lives. Swapping the module's own name, rather than the attribute of the real
    `os`, keeps the pretence inside the store: nothing else in the process ever
    meets this gate.
    """

    def __init__(self, real):
        self._real = real
        self.entered = threading.Event()
        self.go = threading.Event()
        self._held = False

    def __getattr__(self, name):
        return getattr(self._real, name)

    def fsync(self, fd):
        if not self._held:
            self._held = True
            self.entered.set()
            self.go.wait(JOIN)

        return self._real.fsync(fd)


@pytest.fixture
def mid_dump(monkeypatch):
    """Hold the writer inside its first dump until the test lets it out."""
    from pytypehintstore import store as store_module

    gate = _GatedOs(os)
    monkeypatch.setattr(store_module, "os", gate)
    yield gate

    # Released before anything else is torn down: a store closed with the writer
    # still gated would hang the teardown rather than fail the test.
    gate.go.set()


# ---- the debounce ----------------------------------------------------------


@pytest.fixture
def store_file(file_of, store_dir):
    """The file this module's own class lands on."""
    return file_of(Task, store_dir)


@pytest.fixture
def child_file(file_of, store_dir):
    """The file the child scripts land their rows on."""
    return file_of(Row, store_dir)


def test_a_burst_of_writes_costs_a_single_dump(open_store, store_dir, replaces,
                                               until, rows_of, store_file):
    """Twenty writes inside one debounce reach the file as one dump.

    The general case of "two writes in a row cost at most one": every write
    pushes the deadline back, so only the settled state is ever written.
    """
    attempts = replaces()
    store = open_store(Task, store_dir, debounce=0.3)

    for n in range(20):
        store.add(Task(f"task {n}"))

    assert until(lambda: len(titles_in(store_file) or []) == 20), (
        "the burst never reached the file")
    assert len(attempts) == 1, f"the burst cost {len(attempts)} dumps"
    assert len(rows_of(store.path)) == 20


def test_every_write_pushes_the_deadline_further_away(open_store, store_dir,
                                                      replaces, until, store_file):
    """A write arriving before the deadline moves it, rather than riding it.

    The burst above cannot show this: its twenty writes are instant, so a
    deadline set once by the first of them would produce the same single dump.
    Here the writes are spaced wider than half the debounce and narrower than
    the debounce itself, which is exactly the gap a deadline that never moves
    would dump inside of.
    """
    attempts = replaces()
    store = open_store(Task, store_dir, debounce=0.2)

    for n in range(4):
        store.add(Task(f"task {n}"))
        time.sleep(0.12)

    assert len(attempts) == 0, (
        f"the dump landed after {len(attempts)} attempts while writes were "
        f"still arriving inside the debounce")
    assert until(lambda: len(titles_in(store_file) or []) == 4), (
        "the settled state never reached the file")


def test_a_write_with_no_debounce_reaches_the_file_without_a_close(
        open_store, store_dir, until, store_file):
    """With debounce=0 the dump arrives on its own, unhelped.

    No close anywhere: if the writer thread did not carry it, nothing would.
    """
    store = open_store(Task, store_dir, debounce=0.0)
    store.add(Task("on its own"))

    assert until(lambda: titles_in(store_file) == ["on its own"]), (
        "the write never reached the file by itself")


def test_a_write_after_a_dump_starts_the_debounce_again(open_store, store_dir,
                                                        replaces, until, store_file):
    """The writer settles, wakes for the next write, and dumps a second time."""
    attempts = replaces()
    store = open_store(Task, store_dir, debounce=0.05)

    store.add(Task("first"))
    assert until(lambda: titles_in(store_file) == ["first"]), (
        "the first write never reached the file")

    store.add(Task("second"))
    assert until(lambda: titles_in(store_file) == ["first", "second"]), (
        "the second write never reached the file")

    assert len(attempts) == 2


def test_close_dumps_what_is_pending_without_waiting_for_the_debounce(
        open_store, store_dir, rows_of, store_file):
    """A close does not sit out the debounce it interrupted."""
    store = open_store(Task, store_dir, debounce=30.0)
    store.add(Task("now"))

    assert not store_file.exists()

    started = time.monotonic()
    store.close()
    spent = time.monotonic() - started

    assert spent < 5.0, f"close waited {spent:.2f}s, near the whole debounce"
    assert list(rows_of(store.path)) == ["1"]


def test_close_writes_nothing_when_nothing_is_pending(open_store, store_dir,
                                                      replaces, until, store_file):
    """A state the writer already put on disk is not written a second time."""
    attempts = replaces()
    store = open_store(Task, store_dir, debounce=0.05)

    store.add(Task("only"))
    assert until(lambda: titles_in(store_file) == ["only"]), (
        "the write never reached the file")

    store.close()

    assert len(attempts) == 1, "close dumped a state that was already on disk"


# ---- a dump that fails -----------------------------------------------------


def test_a_dump_that_fails_twice_still_lands_on_the_third_try(
        open_store, store_dir, replaces, until, store_file):
    """Two failures do not take the writer thread with them.

    No close in this test: the thread has to come back to the file on its own,
    or every later dump of the process is lost with it — in silence, with the
    rows living on in memory as if they were on disk.
    """
    replaces(fail_first=2)
    store = open_store(Task, store_dir, debounce=0.0)

    store.add(Task("kept"))

    assert until(lambda: titles_in(store_file) == ["kept"]), (
        "the writer never came back to the file after two failed dumps")


def test_the_dump_that_lands_after_two_failures_leaves_nothing_pending(
        open_store, store_dir, replaces, until, store_file):
    """Once the retry lands, the state is written and close has nothing to add.

    "Dirty is false again" is not observable from outside; a close that writes
    nothing is the same claim in the store's own terms.
    """
    attempts = replaces(fail_first=2)
    store = open_store(Task, store_dir, debounce=0.0)

    store.add(Task("kept"))
    assert until(lambda: titles_in(store_file) == ["kept"]), (
        "the retry never reached the file")

    store.close()

    assert len(attempts) == 1, "close wrote a state the retry had already landed"


def test_the_dump_after_a_failure_writes_the_state_that_was_pending(
        open_store, store_dir, replaces, until, store_file):
    """The retry carries the state as it is now, not the snapshot that failed."""
    attempts = replaces(fail_first=1)
    store = open_store(Task, store_dir, debounce=0.0)

    store.add(Task("first"))
    assert until(lambda: len(attempts.asked) == 1), "the first dump never tried"

    # This row arrives after the attempt that failed, so a retry that rewrote
    # that attempt's text would put a state on disk that is already stale.
    store.add(Task("second"))

    assert until(lambda: titles_in(store_file) == ["first", "second"]), (
        "the retry never reached the file with both rows")


def test_a_dump_that_raises_a_base_exception_does_not_kill_the_writer(
        open_store, store_dir, replaces, until, store_file):
    """A failure that is not an Exception still leaves the thread alive.

    `_serialize` reaches user code through `encode`, so what comes back out of a
    dump is not the store's to choose. A writer that only guarded `Exception`
    would die here for the rest of the process; the check is made before any
    close, because a dead writer's rows still reach the file at close time and
    the two cases would look alike afterwards.
    """
    replaces(fail_first=1, error=KeyboardInterrupt())
    store = open_store(Task, store_dir, debounce=0.0)

    store.add(Task("kept"))

    assert until(lambda: titles_in(store_file) == ["kept"]), (
        "a BaseException from the dump took the writer thread with it")


def test_a_dump_that_always_fails_is_retried_at_a_pace_and_not_in_a_spin(
        open_store, store_dir, replaces):
    """A retry gap of zero is a spin, and zero is a legitimate debounce.

    Zero answers how long a burst may push a dump back; it is no answer at all
    to how soon a failed dump may try again, and `_MIN_RETRY` is the floor that
    separates the two questions. A second is measured here with a plain sleep on
    purpose: this test counts attempts inside a window, and the window is the
    measurement. Ten or so attempts is the floor working; thousands is the spin.
    """
    attempts = replaces(fail_forever=True)
    store = open_store(Task, store_dir, debounce=0.0)

    store.add(Task("never lands"))
    time.sleep(1.0)

    tried = len(attempts.asked)

    assert 2 <= tried <= 60, f"{tried} attempts in a second"
    assert len(attempts) == 0


# ---- closing on top of the writer ------------------------------------------


def test_close_while_the_writer_is_mid_dump_waits_and_keeps_the_last_write(
        open_store, store_dir, mid_dump, payload_of, until, store_file):
    """Close waits for the dump in flight instead of running through it.

    The second row arrives while the writer is busy with a snapshot taken before
    it, so only the close that follows can still get it onto the disk, and the
    file has to end up whole and carrying both — never a half of either state.

    Waiting for a thing not to happen has no poll to hang on, so the window is
    short and bounded: a close that skipped the wait would have its own dump on
    the disk long inside it, through the very same part file the writer is
    holding open.
    """
    store = open_store(Task, store_dir, debounce=0.0)

    store.add(Task("first"))
    assert mid_dump.entered.wait(JOIN), "the writer never reached the dump"

    store.add(Task("second"))

    closing = threading.Thread(target=store.close, name="closing")
    closing.start()
    try:
        assert not until(store_file.exists, timeout=0.25), (
            "close laid a second dump on top of the one already in flight")

        mid_dump.go.set()
        closing.join(JOIN)
    finally:
        mid_dump.go.set()

    assert not closing.is_alive(), "close hung on a writer that was mid-dump"

    payload = payload_of(store.path)
    assert [row["title"] for row in payload["rows"].values()] == ["first", "second"]


def test_a_second_close_returns_only_once_the_store_is_really_down(
        open_store, store_dir, mid_dump, lock_of, until, store_file):
    """The caller who waits is told the truth when it is finally true.

    A close that returned while the first one was still writing would promise a
    store on disk and a lock that is gone when neither is so yet. The second
    caller therefore looks at both the moment it comes back, which is a claim
    about what it saw rather than about how long it waited.
    """
    store = open_store(Task, store_dir, debounce=0.0)

    store.add(Task("first"))
    assert mid_dump.entered.wait(JOIN), "the writer never reached the dump"
    store.add(Task("second"))

    seen = {}
    reached = threading.Event()

    def close_and_look():
        reached.set()
        store.close()
        seen["lock"] = lock_of(store.path).exists()
        seen["titles"] = titles_in(store_file)

    first = threading.Thread(target=store.close, name="first close")
    later = threading.Thread(target=close_and_look, name="second close")

    first.start()
    later.start()
    try:
        assert reached.wait(JOIN)
        mid_dump.go.set()
        first.join(JOIN)
        later.join(JOIN)
    finally:
        mid_dump.go.set()

    assert not first.is_alive() and not later.is_alive(), "a close hung"
    assert seen == {"lock": False, "titles": ["first", "second"]}


def test_two_threads_closing_at_once_both_return_and_give_the_lock_back(
        open_store, store_dir, lock_of, payload_of):
    """Two closes racing from a standing start leave one whole store behind."""
    store = open_store(Task, store_dir, debounce=30.0)
    store.add(Task("the last write"))

    failures = []
    start = threading.Barrier(2, timeout=JOIN)

    def close():
        try:
            start.wait()
            store.close()
        except BaseException as error:
            failures.append(error)

    closers = [threading.Thread(target=close, name=f"closer {n}") for n in range(2)]

    for closer in closers:
        closer.start()

    for closer in closers:
        closer.join(JOIN)
        assert not closer.is_alive(), f"{closer.name} hung"

    assert failures == []
    assert [row["title"] for row in payload_of(store.path)["rows"].values()] == [
        "the last write"]
    assert not lock_of(store.path).exists()


def test_closing_three_times_neither_raises_nor_writes_twice(
        open_store, store_dir, replaces):
    """Close is idempotent: the store goes down once and is written once."""
    attempts = replaces()
    store = open_store(Task, store_dir, debounce=30.0)
    store.add(Task("written once"))

    store.close()
    store.close()
    store.close()

    assert len(attempts) == 1, f"the store was written {len(attempts)} times"


def test_a_second_close_raises_the_failure_the_first_one_hit(
        open_store, store_dir, replaces):
    """A close that reports success it did not have is worse than a failure.

    Both callers are closing the same store; returning quietly to the second one
    would tell it the rows are on disk when the first caller has just been told
    they are not.
    """
    replaces(fail_forever=True)
    store = open_store(Task, store_dir, debounce=30.0)
    store.add(Task("never lands"))

    with pytest.raises(OSError) as first:
        store.close()

    with pytest.raises(OSError) as second:
        store.close()

    assert second.value is first.value


def test_close_gives_the_lock_back_even_when_the_last_dump_fails(
        open_store, store_dir, replaces, lock_of):
    """The store is down either way, so the path has to be usable either way.

    A lock kept back here would own the path for the rest of the process, and
    the exit hook that would normally let it go has already been unregistered.
    """
    replaces(fail_forever=True)
    store = open_store(Task, store_dir, debounce=30.0)
    store.add(Task("never reaches the disk"))

    with pytest.raises(OSError):
        store.close()

    assert not lock_of(store.path).exists()

    again = open_store(Task, store_dir)
    assert again.add(Task("the path is usable again")) == 1


def test_a_writer_that_cannot_start_leaves_no_lockfile(monkeypatch, store_dir,
                                                       lock_of, store_file):
    """A store that never opened must not hold the path for the process.

    A thread that could not start leaves the file locked just as surely as one
    that could not be read, and there is no `close` to call: the store the
    caller would have closed was never handed to them.
    """
    from pytypehintstore import store as store_module

    class Stillborn(threading.Thread):
        def start(self):
            raise RuntimeError("can't start new thread")

    class Threading:
        """The store's view of `threading`, with only Thread stood in for."""

        def __init__(self, real):
            self._real = real
            self.Thread = Stillborn

        def __getattr__(self, name):
            return getattr(self._real, name)

    monkeypatch.setattr(store_module, "threading", Threading(threading))

    with pytest.raises(RuntimeError):
        store_of(Task, store_dir)

    assert not lock_of(child_file).exists()


# ---- the interpreter's own way out -----------------------------------------

CHILD = """
from dataclasses import dataclass

from pytypehintstore import store_of


@dataclass
class Row:
    title: str


store = store_of(Row, {path!r}, debounce=30.0)

for n in range(3):
    store.add(Row(title="row %d" % n))
{ending}"""


REFUSING_CHILD = """
import os
from dataclasses import dataclass

from pytypehintstore import store_of
from pytypehintstore import store as store_module


class Refuses:
    def __getattr__(self, name):
        return getattr(os, name)

    def replace(self, src, dst):
        raise OSError("refused")


@dataclass
class Row:
    title: str


store = store_of(Row, {path!r}, debounce=30.0)
store.add(Row(title="lost"))
store_module.os = Refuses()
"""


def test_a_child_whose_last_dump_fails_still_exits_zero(
        store_dir, run_python, child_file):
    """The declared limit: the exit hook complains, the exit code does not.

    The dump the hook runs on the way out cannot land, so the row is lost and
    the interpreter says so on stderr — but the process still ends 0, and a
    supervisor watching only the exit code sees a clean run.
    """
    done = run_python(REFUSING_CHILD.format(path=str(store_dir)), check=False)

    assert done.returncode == 0, done.stderr
    assert "refused" in done.stderr, (
        f"the failure left no trace on the way out:\n{done.stderr}")
    assert not child_file.exists(), "the dump was supposed to be refused"


def test_a_child_that_exits_without_closing_still_leaves_its_rows(
        store_dir, run_python, rows_of, lock_of, child_file, store_file):
    """The exit hook closes the store the caller forgot.

    The debounce is far longer than the child's whole life, so the dump the
    writer never reached has to happen on the way out — and it has to happen
    now rather than in half a minute, which the elapsed time guards.
    """
    started = time.monotonic()

    run_python(CHILD.format(path=str(store_dir), ending=""))

    spent = time.monotonic() - started
    rows = rows_of(child_file)

    assert spent < 20.0, f"the exit path waited {spent:.1f}s for a 30s debounce"
    assert [row["title"] for row in rows.values()] == ["row 0", "row 1", "row 2"]
    assert not lock_of(store_file).exists()


def test_a_child_that_leaves_by_os_exit_leaves_nothing_behind(
        store_dir, run_python, child_file):
    """The other half of the same contract: os._exit runs no hook.

    Nothing else could have written this file — the debounce outlives the child
    by half a minute, so the exit hook is the only way the rows ever reach the
    disk, and this is the exit that does not run it.
    """
    run_python(CHILD.format(path=str(store_dir),
                            ending="\nimport os\nos._exit(0)\n"))

    assert not child_file.exists(), "os._exit ran the exit hook after all"
