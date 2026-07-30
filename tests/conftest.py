"""What every area of the suite borrows.

A store is opened on a directory and lands on a file named for its class and
the fingerprint of its schema, so a test that wants the file asks `file_of`.
Waiting is `until`; a dump is counted and made to fail at `os.replace`.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
HERE = Path(__file__).resolve().parent

for entry in (str(SRC), str(HERE)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from shared import Bag, Priority, Tag, Task  # noqa: E402

__all__ = ["Bag", "Priority", "Tag", "Task"]


# ---- where a store lives ---------------------------------------------------

@pytest.fixture
def store_dir(tmp_path):
    """The directory a store is opened on, for this test alone.

    It exists already, so a test can plant a file in it before opening; a test
    about a directory the store has to create names one of its own.
    """
    folder = tmp_path / "data"
    folder.mkdir()
    return folder


@pytest.fixture
def file_of():
    """The file the store of `cls` lands on inside `directory`."""
    from pytypehint import struct_of

    from pytypehintstore.fingerprint import fingerprint

    def path_of(cls, directory):
        return Path(directory) / f"{cls.__name__}.{fingerprint(struct_of(cls))}.json"

    return path_of


@pytest.fixture
def open_store():
    """Open stores and close every one of them when the test ends.

    A store that stays open holds its lockfile, and the next one on the same
    file would fail for a reason that has nothing to do with what is under
    test. The teardown swallows what a store legitimately raises on the way
    down — a last dump that cannot land — and nothing else.
    """
    from pytypehintstore import StoreError

    opened = []

    def open(cls, directory, **options):
        from pytypehintstore import store_of

        store = store_of(cls, str(directory), **options)
        opened.append(store)
        return store

    yield open

    for store in opened:
        try:
            store.close()
        except (StoreError, OSError):
            pass


@pytest.fixture
def lock_of():
    """The lockfile that belongs to a store file."""
    def lock(path):
        return Path(str(path) + ".lock")

    return lock


# ---- the file --------------------------------------------------------------

@pytest.fixture
def payload_of():
    """The whole file, parsed."""
    def read(path):
        return json.loads(Path(path).read_text(encoding="utf-8"))

    return read


@pytest.fixture
def rows_of(payload_of):
    """The rows of the file, keyed by their id as text."""
    def read(path):
        return payload_of(path)["rows"]

    return read


@pytest.fixture
def write_payload():
    """Write a whole file by hand, the way a person editing it would."""
    def write(path, payload):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    return write


@pytest.fixture
def by_hand(write_payload, file_of):
    """Write the file `cls` will open in `directory`, carrying these rows."""
    def write(cls, directory, rows, *, next_id=None, version=1):
        keyed = ({str(i): row for i, row in enumerate(rows, 1)}
                 if type(rows) is list else rows)
        payload = {"v": version, "rows": keyed}
        payload["next_id"] = (max((int(k) for k in keyed), default=0) + 1
                              if next_id is None else next_id)
        write_payload(file_of(cls, directory), payload)

    return write


# ---- waiting ---------------------------------------------------------------

@pytest.fixture
def until():
    """Poll `predicate` until it is true, or say it never was.

    The writer answers in milliseconds; a sleep long enough to be safe would be
    long enough to dominate the suite.
    """
    def wait(predicate, *, timeout=2.0, step=0.005):
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(step)

        return predicate()

    return wait


# ---- the dump --------------------------------------------------------------

class _Attempts:
    """What the store asked os.replace to do, and what it was allowed to do."""

    def __init__(self):
        self.asked = []
        self.landed = []

    def __len__(self):
        return len(self.landed)


class _ShimOs:
    """The store's view of `os`, with `replace` under the test's control.

    The module's own name is swapped, not the attribute of the real `os`: a
    patched os.replace would reach every other thread in the process.
    """

    def __init__(self, real, before, after):
        self._real = real
        self._before = before
        self._after = after

    def __getattr__(self, name):
        return getattr(self._real, name)

    def replace(self, src, dst):
        self._before(src, dst)
        result = self._real.replace(src, dst)
        self._after(dst)
        return result


@pytest.fixture
def replaces(monkeypatch):
    """Count the dumps that reach the file, and choose which ones fail.

    An arrival is recorded once the file is in place, but the store prunes its
    rotated copies afterwards and a reader on Windows can still meet the
    replace itself: poll for what you expect rather than read once.
    """
    from pytypehintstore import store as store_module

    def install(*, fail_first=0, fail_forever=False, error=None):
        attempts = _Attempts()
        refusal = error if error is not None else OSError("replace refused")

        def before(src, dst):
            attempts.asked.append(str(dst))

            if fail_forever or len(attempts.asked) <= fail_first:
                raise refusal

        def after(dst):
            attempts.landed.append(str(dst))

        monkeypatch.setattr(store_module, "os", _ShimOs(os, before, after))
        return attempts

    return install


# ---- other processes -------------------------------------------------------

@pytest.fixture
def run_python():
    """Run a script in a separate process, with the library importable."""
    def run(source, *, timeout=60, check=True):
        script = f"import sys\nsys.path.insert(0, {str(SRC)!r})\n{source}"
        done = subprocess.run([sys.executable, "-c", script],
                              capture_output=True, text=True, timeout=timeout)
        if check and done.returncode != 0:
            raise AssertionError(
                f"child failed ({done.returncode}):\n{done.stdout}\n{done.stderr}")
        return done

    return run


@pytest.fixture
def spawn_python():
    """Start a script in a separate process and leave it running.

    Whatever is left in the pipes is drained on the way out, so a talkative
    child cannot wedge the test on a full buffer.
    """
    started = []

    def spawn(source):
        script = f"import sys\nsys.path.insert(0, {str(SRC)!r})\n{source}"
        child = subprocess.Popen([sys.executable, "-c", script],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, bufsize=1)
        started.append(child)
        return child

    yield spawn

    for child in started:
        if child.poll() is None:
            child.kill()

        try:
            child.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate(timeout=30)
