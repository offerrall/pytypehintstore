"""A dict with a shadow on disk.

Rows live in memory and every read answers from there; a writer thread carries
them to a JSON file named after the class and the fingerprint of its schema.
"""

import atexit
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

from pytypehint import List, SchemaTypeError, SchemaValueError, Struct, struct_of

from pytypehintstore.codec import decode, encode
from pytypehintstore.errors import StoreError, StoreLoadError
from pytypehintstore.fingerprint import fingerprint
from pytypehintstore.lockfile import acquire, release

_VERSION = 1

# str.isdigit() is not the question int() answers: it says yes to a superscript
# and nothing about where a "-" sits.
_ROW_ID = re.compile(r"-?[0-9]+")

# A debounce is how long a burst is given to settle, which may legitimately be
# zero; a retry gap of zero is a spin.
_MIN_RETRY = 0.1


class Store:
    """Rows of one validated dataclass, kept in memory and mirrored to JSON."""

    def __init__(self, cls, directory, *, debounce: float = 2.0, keep: int = 5):
        self._cls = cls
        self._schema = struct_of(cls)

        name = f"{cls.__name__}.{fingerprint(self._schema)}.json"
        folder = Path(directory).expanduser().resolve()

        if folder.exists() and not folder.is_dir():
            raise StoreError(f"{directory}: not a directory")

        self._path = folder / name
        self._name = str(Path(directory) / name)
        self._ambiguous()
        self._debounce = float(debounce)
        self._keep = int(keep)

        self._rows: dict[int, object] = {}
        self._next_id = 1

        self._lock = threading.Lock()
        self._pending = threading.Condition(self._lock)
        self._dirty = False
        self._deadline = 0.0
        self._closed = False
        self._shut = threading.Event()
        self._down_error: BaseException | None = None

        folder.mkdir(parents=True, exist_ok=True)
        self._held = acquire(self._path, self._name)

        try:
            self._load()
            self._writer = threading.Thread(
                target=self._run, name=f"pytypehintstore:{name}", daemon=True)
            self._writer.start()
        except BaseException:
            # A store that never opened must not leave its file locked.
            release(self._held)
            raise

        atexit.register(self.close)

    # ---- the dict ----------------------------------------------------------

    def add(self, obj) -> int:
        with self._lock:
            self._open()
            row = self._accepted(obj)
            row_id = self._next_id
            self._next_id += 1
            self._rows[row_id] = row
            self._touch()
        return row_id

    # `get` and `all` hand back the store's own instance, not a copy: a read is
    # a dict lookup and stays one. Mutating what comes back reaches inside the
    # store, unvalidated and unmarked. Build a fresh instance and `put` it.
    def get(self, row_id: int):
        with self._lock:
            self._open()
            return self._rows[row_id]

    def put(self, row_id: int, obj) -> None:
        with self._lock:
            self._open()
            if row_id not in self._rows:
                raise KeyError(row_id)
            row = self._accepted(obj)
            self._rows[row_id] = row
            self._touch()

    def remove(self, row_id: int) -> None:
        with self._lock:
            self._open()
            del self._rows[row_id]
            self._touch()

    def all(self) -> list[tuple[int, object]]:
        with self._lock:
            self._open()
            return [(row_id, self._rows[row_id]) for row_id in sorted(self._rows)]

    def __len__(self) -> int:
        with self._lock:
            self._open()
            return len(self._rows)

    def __contains__(self, row_id) -> bool:
        with self._lock:
            self._open()
            return row_id in self._rows

    @property
    def path(self) -> Path:
        return self._path

    # ---- closing -----------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            first = not self._closed
            self._closed = True
            self._pending.notify_all()

        # A close already under way owns the teardown; a second caller waits for
        # it, and shares its failure rather than reporting a file that is not
        # there.
        if not first:
            self._shut.wait()

            if self._down_error is not None:
                raise self._down_error

            return

        try:
            self._writer.join()
            self._flush()
        except BaseException as e:
            self._down_error = e
            raise
        finally:
            # A lock kept back here would own the path for the rest of the
            # process, and the atexit hook could no longer let it go.
            release(self._held)
            atexit.unregister(self.close)
            self._shut.set()

    def _open(self) -> None:
        if self._closed:
            raise StoreError(f"{self._name}: store is closed")

    # Two options of one union that answer to the same transport name are
    # indistinguishable in the file: the wrapper names an option and the reader
    # takes the first that answers. The core allows the pair — an enum class
    # called `date` competes with `date` itself, in different namespaces to it —
    # so the store refuses at the door rather than routing a value to the wrong
    # branch and handing back something nobody stored.
    def _ambiguous(self) -> None:
        found = _shared_name((self._schema,), (), set())

        if found is None:
            return

        path, shared = found
        where = ": ".join(path)
        raise StoreError(
            f"{self._name}: {where}: two options of a union share the transport "
            f"name {shared!r}: rename one of the classes")

    # A row is accepted by making the round trip it will make anyway. What comes
    # back is what the store keeps; what build() refuses travels out in the
    # core's own words.
    def _accepted(self, obj):
        if type(obj) is not self._cls:
            raise SchemaTypeError(
                f"expected {self._cls.__name__}, got {type(obj).__name__}")

        row = encode(self._schema, obj)
        built = self._schema.build(decode(self._schema, row))

        # The core has accepted the row; what remains is whether the file can
        # carry it. A lone surrogate is a str Python allows and no encoder can
        # write, and such a row would freeze every later dump — a dump is the
        # whole file or nothing — surfacing only at close(), as an error from
        # another library. Asked after build(), so the core still speaks first.
        try:
            json.dumps(row, ensure_ascii=False).encode("utf-8")
        except UnicodeEncodeError as e:
            raise SchemaValueError(f"cannot be written as UTF-8: {e}") from e

        return built

    # ---- the shadow --------------------------------------------------------

    # Called with the lock held. Every write pushes the deadline back, so a
    # burst costs one dump.
    def _touch(self) -> None:
        self._dirty = True
        self._deadline = time.monotonic() + self._debounce
        self._pending.notify_all()

    def _run(self) -> None:
        while True:
            with self._lock:
                while not self._dirty and not self._closed:
                    self._pending.wait()

                if self._closed:
                    return

                while not self._closed:
                    remaining = self._deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._pending.wait(remaining)

                if self._closed:
                    return

            try:
                self._flush()
            except BaseException:
                # A writer that died here would take every later dump of the
                # process with it, in silence, and the rows would live on in
                # memory as if they were on disk.
                with self._lock:
                    self._deadline = time.monotonic() + max(self._debounce, _MIN_RETRY)

    def _flush(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            # Snapshot under the lock so it is one coherent state; the disk is
            # slow and is touched outside it.
            text = self._serialize()
            self._dirty = False

        try:
            self._write(text)
        except BaseException:
            with self._lock:
                self._dirty = True
            raise

    def _serialize(self) -> str:
        rows = {str(row_id): encode(self._schema, self._rows[row_id])
                for row_id in sorted(self._rows)}
        payload = {"v": _VERSION, "next_id": self._next_id, "rows": rows}
        return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"

    def _write(self, text: str) -> None:
        part = self._path.with_name(self._path.name + ".part")

        try:
            with open(part, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())

            if self._path.exists():
                self._rotate()

            os.replace(part, self._path)
        except BaseException:
            # A dump that did not make it leaves nothing of itself behind.
            part.unlink(missing_ok=True)
            raise

        self._prune()

    def _rotate(self) -> None:
        # Nanoseconds: two dumps inside one second would share a name and the
        # second copy would land on the first.
        stamp = time.time_ns()
        keep = self._path.with_name(f"{self._path.stem}.{stamp}{self._path.suffix}")
        shutil.copy2(self._path, keep)

    # The stem carries the fingerprint, so each database prunes its own copies
    # and never counts another's.
    def _prune(self) -> None:
        pattern = re.compile(
            rf"^{re.escape(self._path.stem)}\.(\d+){re.escape(self._path.suffix)}$")
        old = sorted(
            ((int(m.group(1)), p) for p in self._path.parent.iterdir()
             if (m := pattern.match(p.name))),
            reverse=True)

        for _, p in old[self._keep:]:
            try:
                p.unlink()
            except OSError:
                pass

    # ---- reading it back ---------------------------------------------------

    def _load(self) -> None:
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except UnicodeDecodeError as e:
            # A ValueError, not an OSError: the clause below would let it out
            # as itself.
            raise StoreLoadError(f"{self._name}: not valid UTF-8: {e}") from e
        except OSError as e:
            raise StoreLoadError(f"{self._name}: cannot be read: {e}") from e

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as e:
            raise StoreLoadError(f"{self._name}: not valid JSON: {e}") from e

        if type(payload) is not dict:
            raise StoreLoadError(
                f"{self._name}: expected an object, got {type(payload).__name__}")

        # The type is part of the question: `True == 1` and `1.0 == 1` in
        # Python, so an equality alone would read either as version 1.
        if type(payload.get("v")) is not int or payload["v"] != _VERSION:
            raise StoreLoadError(
                f"{self._name}: unknown format version {payload.get('v')!r}, "
                f"expected {_VERSION}")

        rows = payload.get("rows", {})

        if type(rows) is not dict:
            raise StoreLoadError(
                f"{self._name}: rows must be an object, got {type(rows).__name__}")

        for key, row in rows.items():
            if not _ROW_ID.fullmatch(key):
                raise StoreLoadError(f"{self._name}: row id {key!r} is not an integer")

            row_id = int(key)

            # "1" and "01" are different text and the same id; one row would
            # take the other's place and the next dump would write the survivor
            # alone.
            if row_id in self._rows:
                raise StoreLoadError(
                    f"{self._name}: row id {key!r} is already taken by another key")

            self._rows[row_id] = self._build(row_id, row)

        highest = max(self._rows, default=0)
        stored = payload.get("next_id")
        # A hand-edited next_id can name an id a row already holds.
        self._next_id = max(highest + 1, stored if type(stored) is int else 1)

    def _build(self, row_id: int, row):
        try:
            return self._schema.build(decode(self._schema, row))
        except (TypeError, ValueError) as e:
            raise StoreLoadError(f"{self._name}: row {row_id}: {e}") from e


# The first pair of options sharing a transport name, as (path, name), or None.
# `seen` keeps a recursive schema from being walked forever.
def _shared_name(shapes, path, seen):
    names = set()

    for shape in shapes:
        # A Struct carries its $type inside the object and never competes for
        # the wrapper's, so a dataclass and an enum may share one name — the
        # core allows exactly that, and the codec keeps them apart.
        if type(shape) is Struct:
            continue

        name = shape.option_id()

        if name in names:
            return path, name

        names.add(name)

    for shape in shapes:
        if type(shape) is Struct:
            if id(shape) in seen:
                continue

            seen.add(id(shape))

            for field in shape.fields:
                found = _shared_name(field.shape, (*path, field.name), seen)

                if found is not None:
                    return found

        elif type(shape) is List:
            found = _shared_name(shape.item, path, seen)

            if found is not None:
                return found

    return None


def store_of(cls, directory, *, debounce: float = 2.0, keep: int = 5) -> Store:
    """Open the store of `cls` inside `directory`.

    The file is named for the class and the fingerprint of its schema, so a
    changed class is a new database and the old file stays beside it.
    """
    return Store(cls, directory, debounce=debounce, keep=keep)
