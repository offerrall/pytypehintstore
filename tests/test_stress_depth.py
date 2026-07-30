"""Depth, width, volume and four threads: the store at its limits.

Nothing here is a unit test of a method. Every test asks the same question from
a different direction — *the core compiled this schema; does the store survive
it, or does it fail where you can hear it?* — and the answers that are numbers
rather than assertions are printed, so `-s` turns this file into a small
benchmark:

    python -m pytest tests/test_stress_depth.py -q -s --durations=10

Three shapes recur.

`chain(depth)` is a spine of `depth` dataclasses, each one carrying a scalar
union and a list of the next: one reference per level, so every recursive walk
in the library — `struct_of`, `fingerprint`, `encode`, `decode`, `build` — costs
`depth` frames and nothing more. It is the honest deep schema.

`fan(depth)` is the same spine with *two* references per level. To the core that
is still `depth` classes and it compiles in milliseconds; to `fingerprint` it is
a DAG walked as a tree, and the cost doubles with every level. See
`test_fingerprint_of_a_shared_struct_is_not_exponential`, which is xfail.

`Node` is the recursive one — `children: list[Node]` — where the schema is a
single cycle and the depth lives in the *data*.

Two limits are bisected at run time instead of pinned to a number, because both
are measured from wherever the current stack already is and pytest's stack is
not a script's. What is asserted is the shape of the failure: a `RecursionError`
and no row, never a silently shortened one. The numbers travel in the printout.
"""

import json
import random
import sys
import threading
import time
from dataclasses import dataclass, field, make_dataclass
from datetime import date, time as clock
from enum import Enum
from typing import Annotated

import pytest
from pytypehint import (Choices, Description, Extra, IsPassword, Label, Max,
                        Min, MultipleOf, Pattern, Placeholder, Rows, Slider,
                        Step, struct_of)

from pytypehintstore import store_of
from pytypehintstore.codec import decode, encode
from pytypehintstore.fingerprint import fingerprint

# Long enough that a thread still inside it is hung rather than slow.
JOIN = 10.0


def note(label, **numbers):
    """A measurement, not an assertion. Visible under `-s`."""
    body = "  ".join(f"{k}={v}" for k, v in numbers.items())
    print(f"\n[stress] {label}: {body}")


# ---- the shapes ------------------------------------------------------------

class Colour(Enum):
    RED = "red"
    GREEN = "green"
    BLUE = "blue"


def chain(depth, tag):
    """`depth` dataclasses, each holding the next inside a list.

    Returns the outermost class and the whole ladder, leaf first. Every level
    below the leaf carries a union (`int | str`) and a list, and exactly one
    reference to the level under it, so the schema is a line and not a tree.
    """
    leaf = make_dataclass(f"Leaf{tag}", [("value", int, field(default=0))])
    ladder = [leaf]
    level = leaf

    for k in range(depth - 1, 0, -1):
        level = make_dataclass(f"Level{k}{tag}", [
            ("name", str),
            ("choice", int | str),
            ("items", list[level], field(default_factory=list)),
        ])
        ladder.append(level)

    return level, ladder


def nested(ladder):
    """One instance of the outermost class of `ladder`, filled to the bottom."""
    obj = ladder[0](value=7)

    for k in range(1, len(ladder)):
        obj = ladder[k](name=f"n{k}", choice=(k if k % 2 else f"s{k}"), items=[obj])

    return obj


def fan(depth, tag):
    """The same ladder, with *two* fields pointing at the level below.

    The core compiles this as fast as `chain` — a class is compiled once and the
    two fields share the compiled `Struct`. Only a walker without a memo sees
    two subtrees where there is one node.
    """
    level = make_dataclass(f"FanLeaf{tag}", [("value", int, field(default=0))])

    for k in range(depth):
        level = make_dataclass(f"Fan{k}{tag}", [
            ("child", level | None, field(default=None)),
            ("items", list[level], field(default_factory=list)),
        ])

    return level


@dataclass
class Node:
    """The recursive one: the schema is a cycle, the depth is in the data."""

    name: str
    children: list["Node"] = field(default_factory=list)


def spine(depth):
    """A `Node` tree that is one long branch: the depth is in the data."""
    node = Node(name="leaf")

    for k in range(depth):
        node = Node(name=f"n{k}", children=[node])

    return node


def sixty_fields(tag, tail=None):
    """A dataclass of 60 fields, cycling through everything a row can hold.

    When `tail` is given the last field carries a list of it, so the class can
    be a level of a ladder as well as a row of its own.
    """
    kinds = [
        ("s", Annotated[str, Min(1), Max(40), Label("S"), Placeholder("type")], "x"),
        ("i", Annotated[int, Min(0), Max(1000), MultipleOf(2), Slider()], 4),
        ("f", Annotated[float, Min(0.0), Max(1.0), Step(0.25)], 0.5),
        ("b", bool, True),
        ("u", int | str, 1),
        ("d", date, date(2026, 7, 30)),
        ("t", clock, clock(12, 30)),
        ("e", Colour, Colour.RED),
        ("l", list[int], None),
        ("p", Annotated[str, IsPassword(), Description("secret")], "hunter2"),
        ("c", Annotated[str, Choices(values=("a", "b")), Rows(3)], "a"),
        ("r", Annotated[str, Pattern(r"^[a-z]+$"), Extra("stress.k", "v")], "ok"),
    ]

    spec = []

    for n in range(60):
        prefix, hint, default = kinds[n % len(kinds)]
        name = f"{prefix}{n}"

        if default is None:
            spec.append((name, hint, field(default_factory=list)))
        else:
            spec.append((name, hint, field(default=default)))

    if tail is not None:
        spec.append(("tail", list[tail], field(default_factory=list)))

    return make_dataclass(f"Wide{tag}", spec)


# ---- 1. twenty levels ------------------------------------------------------

def test_chain_of_twenty_levels_round_trips_through_the_codec():
    """P4: encode/decode/build alone, no store, no file."""
    top, ladder = chain(20, "RT")
    schema = struct_of(top)
    obj = nested(ladder)

    started = time.perf_counter()
    wire = encode(schema, obj)
    back = schema.build(decode(schema, wire))
    elapsed = time.perf_counter() - started

    assert back == obj
    # The transport is a plain object all the way down, with no wrapper: at each
    # level the union is int|str and only one option owns each JSON type.
    text = json.dumps(wire)
    assert "$type" not in text
    note("chain(20) codec round trip", seconds=f"{elapsed:.4f}", bytes=len(text))


def test_chain_of_twenty_levels_survives_a_store(store_dir, open_store, file_of,
                                                 rows_of):
    """P1 and P2 on a schema twenty dataclasses deep."""
    top, ladder = chain(20, "Store")
    obj = nested(ladder)

    store = open_store(top, store_dir, debounce=0.02)
    row_id = store.add(obj)

    assert store.get(row_id) == obj
    assert store.get(row_id) is not obj

    store.close()

    path = file_of(top, store_dir)
    assert path.exists()
    assert list(rows_of(path)) == [str(row_id)]

    again = open_store(top, store_dir, debounce=0.02)
    assert again.all() == [(row_id, obj)]
    # Ids are spent, never recycled: the next one carries on from the file.
    assert again.add(obj) == row_id + 1


def test_the_depth_that_breaks_is_a_recursion_error_and_not_a_short_row():
    """Bisect the ladder until something gives, and check what gave.

    Two ceilings are measured. The core's: the deepest `struct_of` compiles. The
    store's: the deepest `fingerprint` survives, which is the one that matters,
    because `store_of` calls it before anything else and a store that cannot be
    named cannot be opened.

    Whatever the numbers are on this machine, the contract is the same at every
    depth: either the whole round trip is exact, or a `RecursionError` comes out.
    Never a row that is nearly right.
    """
    ceiling = 400

    def compiles(depth):
        try:
            struct_of(chain(depth, f"C{depth}")[0])
            return True
        except RecursionError:
            return False

    def named(depth):
        try:
            fingerprint(struct_of(chain(depth, f"N{depth}")[0]))
            return True
        except RecursionError:
            return False

    def deepest(predicate):
        low, high = 1, ceiling

        while low < high:
            middle = (low + high + 1) // 2

            if predicate(middle):
                low = middle
            else:
                high = middle - 1

        return low

    core = deepest(compiles)
    store = deepest(named)

    assert core < ceiling, "raise the ceiling: the core never gave in"
    note("recursion ceilings", core_struct_of=core, store_fingerprint=store,
         recursionlimit=sys.getrecursionlimit())

    # The loud half of the guarantee. At the deepest depth the store can name,
    # the whole trip is exact; one level past the core's ceiling, the failure is
    # a RecursionError and nothing else.
    top, ladder = chain(store, "Deepest")
    schema = struct_of(top)
    obj = nested(ladder)
    assert schema.build(decode(schema, encode(schema, obj))) == obj

    with pytest.raises(RecursionError):
        fingerprint(struct_of(chain(core + 1, "Past")[0]))


def test_a_schema_the_core_compiles_but_cannot_be_named_fails_at_open(store_dir):
    """The window between the two ceilings, and what the store does inside it.

    `fingerprint` gives in before `struct_of` does — the store is walking a
    schema the core has just finished building, from a stack the core has
    already been down once. So there is a band of depths where the class is a
    perfectly good contract and no store can be opened on it.

    That is allowed: it is loud. What would not be allowed is a half-opened
    store, so the debris is checked too — the fingerprint is read before the
    lock is taken, and nothing at all is left in the directory.
    """
    def opens(depth):
        try:
            fingerprint(struct_of(chain(depth, f"W{depth}")[0]))
            return True
        except RecursionError:
            return False

    low, high = 1, 400

    while low < high:
        middle = (low + high + 1) // 2

        if opens(middle):
            low = middle
        else:
            high = middle - 1

    top = chain(low + 1, "Window")[0]
    # The core is happy with it; only the store is not.
    assert struct_of(top) is not None

    with pytest.raises(RecursionError):
        store_of(top, str(store_dir))

    assert list(store_dir.iterdir()) == []


# ---- 2. a recursive dataclass, deep and wide -------------------------------

def test_recursive_node_with_fifty_levels_of_data(store_dir, open_store):
    """The schema is one cycle; the depth is in the rows."""
    tree = spine(50)
    schema = struct_of(Node)

    assert schema.build(decode(schema, encode(schema, tree))) == tree

    store = open_store(Node, store_dir, debounce=0.02)
    row_id = store.add(tree)
    assert store.get(row_id) == tree
    store.close()

    again = open_store(Node, store_dir, debounce=0.02)
    assert again.all() == [(row_id, tree)]


def test_recursive_node_with_a_thousand_wide(store_dir, open_store, rows_of,
                                             file_of):
    """One root, 999 children: width instead of depth."""
    tree = Node(name="root", children=[Node(name=f"c{n}") for n in range(999)])

    store = open_store(Node, store_dir, debounce=0.02)
    started = time.perf_counter()
    row_id = store.add(tree)
    added = time.perf_counter() - started

    started = time.perf_counter()
    store.close()
    closed = time.perf_counter() - started

    path = file_of(Node, store_dir)
    assert len(rows_of(path)["1"]["children"]) == 999

    started = time.perf_counter()
    again = open_store(Node, store_dir, debounce=0.02)
    reloaded = time.perf_counter() - started

    assert again.all() == [(row_id, tree)]
    note("Node with 1000 nodes wide", add=f"{added:.4f}", close=f"{closed:.4f}",
         reload=f"{reloaded:.4f}", bytes=path.stat().st_size)


def test_the_data_depth_that_breaks_is_loud_and_spends_no_id(store_dir,
                                                             open_store):
    """Bisect the deepest tree `add` accepts, then check both sides of it.

    `add` validates by making the round trip the row will make anyway, so the
    ceiling here is the codec's and the core's together. Below it the row
    persists exactly; above it `add` raises `RecursionError` — and, because the
    row is refused before an id is spent, the store is untouched.
    """
    store = open_store(Node, store_dir, debounce=30.0)

    def accepts(depth):
        try:
            store.remove(store.add(spine(depth)))
            return True
        except RecursionError:
            return False

    low, high = 1, 400

    while low < high:
        middle = (low + high + 1) // 2

        if accepts(middle):
            low = middle
        else:
            high = middle - 1

    note("Node data depth", deepest_add_accepts=low)
    assert low < 400, "raise the ceiling: add never gave in"

    before = len(store)
    with pytest.raises(RecursionError):
        store.add(spine(low + 1))

    # Refused before an id was spent, and before a row was kept.
    assert len(store) == before
    surviving = store.add(spine(low))
    tree = store.get(surviving)
    store.close()

    again = open_store(Node, store_dir, debounce=30.0)
    assert again.get(surviving) == tree


# ---- 3. sixty fields -------------------------------------------------------

def test_sixty_fields_round_trip_and_persist(store_dir, open_store, rows_of,
                                             file_of):
    Wide = sixty_fields("Sixty")
    schema = struct_of(Wide)
    assert len(schema.fields) == 60

    row = Wide()
    assert schema.build(decode(schema, encode(schema, row))) == row

    store = open_store(Wide, store_dir, debounce=0.02)
    row_id = store.add(row)
    store.close()

    written = rows_of(file_of(Wide, store_dir))[str(row_id)]
    assert len(written) == 60
    # The transport form, not a repr: ISO text for a date, the member name for
    # an enum.
    assert written["d5"] == "2026-07-30"
    assert written["e7"] == "RED"

    again = open_store(Wide, store_dir, debounce=0.02)
    assert again.all() == [(row_id, row)]


# ---- 4. thirty stores in one directory -------------------------------------

def test_thirty_stores_share_a_directory(store_dir, open_store, file_of,
                                         lock_of, rows_of, until):
    """Thirty classes, thirty files, thirty locks, one folder.

    Thirty writer threads rotate and prune in the same directory at once, and
    each one's `_prune` reads that whole directory. `keep=2` with a zero
    debounce makes every round a dump, so by the end every family has rotated
    more times than it is allowed to keep — which is what makes the pruning
    real. Each family must come out holding exactly its own two copies: one that
    kept a third would not be pruning, and one that ended with fewer would have
    been pruned by a neighbour.
    """
    families = [make_dataclass(f"Family{n}", [("name", str), ("n", int)])
                for n in range(30)]
    paths = [file_of(cls, store_dir) for cls in families]
    # Thirty classes, thirty distinct fingerprints, thirty distinct files.
    assert len({path.name for path in paths}) == 30

    started = time.perf_counter()
    stores = [open_store(cls, store_dir, debounce=0.0, keep=2)
              for cls in families]

    def counted(path):
        """How many rows the file carries, or -1 while there is no whole one."""
        try:
            return len(rows_of(path))
        except (OSError, ValueError, KeyError):
            return -1

    for row in range(5):
        for index, (cls, store) in enumerate(zip(families, stores)):
            store.add(cls(name=f"{index}-{row}", n=row))

        # Waiting on the dumps, not on the clock: the next round has to be a
        # dump of its own, or nothing rotates.
        assert until(lambda: all(counted(path) >= row + 1 for path in paths),
                     timeout=10.0), f"round {row} never reached every file"

    for store in stores:
        store.close()

    elapsed = time.perf_counter() - started

    for index, (cls, path) in enumerate(zip(families, paths)):
        assert path.exists(), cls.__name__
        assert list(rows_of(path)) == [str(n) for n in range(1, 6)]
        # A lock is released on close, and the file it named is the one the
        # class fingerprinted to.
        assert not lock_of(path).exists()

        copies = sorted(path.parent.glob(f"{path.stem}.*{path.suffix}"))
        assert len(copies) == 2, f"{cls.__name__}: {[p.name for p in copies]}"

        # A copy belongs to the family whose stem it carries, and is whole JSON.
        for copy in copies:
            names = {row["name"] for row in json.loads(
                copy.read_text(encoding="utf-8"))["rows"].values()}
            assert all(name.startswith(f"{index}-") for name in names), copy.name

    reopened = [open_store(cls, store_dir, debounce=0.0, keep=2)
                for cls in families]

    for index, (cls, store) in enumerate(zip(families, reopened)):
        assert store.all() == [(row + 1, cls(name=f"{index}-{row}", n=row))
                               for row in range(5)]

    note("30 stores in one directory", seconds=f"{elapsed:.3f}",
         files=len(list(store_dir.iterdir())))


# ---- 5. five thousand rows -------------------------------------------------

def test_five_thousand_rows(store_dir, open_store, file_of):
    """Volume, and the four numbers it costs.

    The debounce is long on purpose: this measures one dump of the whole state
    at `close()`, not a writer racing the loop. The timings are printed, never
    asserted — a threshold here would only measure the machine.
    """
    Inner = make_dataclass("Inner", [("a", str), ("b", int)])
    Row = make_dataclass("Row5k", [
        ("title", str),
        ("n", int),
        ("ratio", float),
        ("done", bool),
        ("either", int | str),
        ("inner", Inner),
        ("marks", list[int], field(default_factory=list)),
        ("many", list[Inner], field(default_factory=list)),
    ])

    rows = [Row(title=f"r{n}", n=n, ratio=n / 2, done=bool(n % 2),
                either=(n if n % 2 else f"s{n}"), inner=Inner(a=f"i{n}", b=n),
                marks=[n, n + 1], many=[Inner(a="x", b=1)])
            for n in range(5000)]

    store = open_store(Row, store_dir, debounce=30.0)

    started = time.perf_counter()
    ids = [store.add(row) for row in rows]
    added = time.perf_counter() - started

    started = time.perf_counter()
    store.close()
    closed = time.perf_counter() - started

    started = time.perf_counter()
    again = open_store(Row, store_dir, debounce=30.0)
    reloaded = time.perf_counter() - started

    assert ids == list(range(1, 5001))
    assert again.all() == list(zip(ids, rows))

    note("5000 rows of an 8-field nested dataclass",
         add=f"{added:.3f}", close=f"{closed:.3f}", reload=f"{reloaded:.3f}",
         bytes=file_of(Row, store_dir).stat().st_size)


# ---- 6. the fingerprint of a large schema ----------------------------------

def test_fingerprint_of_sixty_fields_and_twenty_levels(store_dir):
    """P5: two compilations of the same contract, one name.

    Sixty fields per level and twenty levels is 1200 fields and every notation
    atom the core has. The two ladders are different class objects — so the
    core's cache is cold for both — carrying the same names, types, atoms and
    defaults. A fingerprint that moved between them would rename the database on
    the next run.
    """
    def ladder(depth):
        level = sixty_fields("Deep0")

        for k in range(1, depth):
            level = sixty_fields(f"Deep{k}", tail=level)

        return level

    first = struct_of(ladder(20))
    second = struct_of(ladder(20))
    assert first is not second

    started = time.perf_counter()
    one = fingerprint(first)
    elapsed = time.perf_counter() - started
    other = fingerprint(second)

    assert one == other
    assert len(one) == 8
    note("fingerprint of 60 fields x 20 levels", seconds=f"{elapsed:.4f}",
         value=one)
    assert elapsed < 0.1, f"fingerprint took {elapsed:.3f}s"


def test_fingerprint_of_a_shared_struct_is_not_exponential():
    """The cost of a level is not a factor.

    The core compiles a class once and shares that object between every field
    naming it, so a schema is a graph. Walking it as a tree used to cost a path
    per branch: fan(10) 0.05s, fan(15) 2.0s, fan(18) 36s, fan(20) ~10 minutes —
    and since `Store.__init__` fingerprints before it takes the lock, opening
    such a store hung with no error and no output. `_plain` now writes each
    dataclass once and refers back to it afterwards.

    Timed rather than counted, and compared against itself rather than against a
    number of seconds, so the assertion says "the curve is exponential" and not
    "this machine is slow". Doubling per level makes four levels a factor of 16;
    walking the graph once makes it about 1.4.
    """
    def cost(depth):
        schema = struct_of(fan(depth, f"X{depth}"))
        started = time.perf_counter()
        fingerprint(schema)
        return time.perf_counter() - started

    shallow = cost(10)
    deeper = cost(14)

    note("fingerprint of a doubly-referenced schema",
         fan10=f"{shallow:.4f}", fan14=f"{deeper:.4f}",
         factor=f"{deeper / shallow:.1f}")

    assert deeper < shallow * 4, (
        f"four more levels cost {deeper / shallow:.1f}x: the walk is "
        f"exponential in the depth")


# ---- 7. the writer under fire ----------------------------------------------

def test_four_threads_and_then_the_file_is_the_dict(store_dir, open_store,
                                                    payload_of, file_of, until,
                                                    replaces):
    """Four threads write for two seconds; the file must be the dict, exactly.

    Each thread owns the ids it created, so `put` and `remove` are only ever
    aimed at rows it added itself — but ids are handed out by the store and two
    threads interleave freely, so a `KeyError` is still possible and is correct
    when it happens. It is counted, not swallowed silently: what is under test
    is that no row is lost and none is invented.

    The 1 ms pause is pacing, not waiting — it keeps the row count in the
    thousands rather than the tens of thousands, so the dumps stay cheap. The
    only *waiting* is `until`, on a dump landing after the fire stops: proof
    that the writer thread came out of it alive.
    """
    Row = make_dataclass("Fire", [("title", str), ("n", int)])
    dumps = replaces()
    store = open_store(Row, store_dir, debounce=0.05)

    deadline = time.monotonic() + 2.0
    refused = []
    broke = []

    def worker(seed):
        dice = random.Random(seed)
        mine = []
        done = 0

        try:
            while time.monotonic() < deadline and done < 1500:
                done += 1
                roll = dice.random()

                try:
                    if roll < 0.5 or not mine:
                        mine.append(store.add(Row(title=f"w{seed}", n=done)))
                    elif roll < 0.8:
                        store.put(dice.choice(mine), Row(title=f"w{seed}u", n=done))
                    else:
                        row_id = mine.pop(dice.randrange(len(mine)))
                        store.remove(row_id)
                except KeyError:
                    # A row this thread owned is gone: correct, and counted.
                    refused.append(seed)

                time.sleep(0.001)
        except BaseException as e:  # noqa: BLE001 - handed back, not swallowed
            broke.append(e)

    threads = [threading.Thread(target=worker, args=(seed,), daemon=True)
               for seed in range(4)]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join(JOIN)
        assert not thread.is_alive()

    assert not broke, broke

    # The fire is over. One more write, with nothing left to push the deadline
    # back, must reach the file within a debounce: that is the writer thread
    # answering for itself, and the only wait in this test.
    landed = len(dumps.landed)
    store.add(Row(title="after the fire", n=0))
    assert until(lambda: len(dumps.landed) > landed, timeout=5.0), (
        "the writer thread did not dump again after the burst")

    memory = store.all()
    assert len(memory) > 1, "the threads removed everything; this proves nothing"
    store.close()

    payload = payload_of(file_of(Row, store_dir))
    ids = [row_id for row_id, _ in memory]
    # Not one row more and not one less, in order, under the ids memory holds.
    assert [int(key) for key in payload["rows"]] == ids
    # Ids are spent, never recycled, so the counter has run past the highest id
    # any surviving row carries.
    assert payload["next_id"] > max(ids)

    again = open_store(Row, store_dir, debounce=0.05)
    assert again.all() == memory
    assert again.add(Row(title="after", n=0)) == payload["next_id"]

    note("four threads for 2s", rows=len(memory), dumps=len(dumps.landed),
         key_errors=len(refused))


def test_four_threads_with_the_writer_dumping_the_whole_time(store_dir,
                                                             open_store,
                                                             payload_of,
                                                             file_of, until,
                                                             replaces):
    """The same fire, with `debounce=0.0` so the dumps land during it.

    The test above measures one dump, because a burst that never pauses longer
    than the debounce keeps pushing the deadline back — the writer's whole
    point, and worth having on the record. This one removes the debounce so
    every write is a dump, and the serialise / rotate / replace / prune cycle
    runs continuously while four threads are mutating the dict underneath it.

    A rotated copy is a whole file or it is nothing: each one is parsed at the
    end, and each must be a payload of this version with integer row ids.
    """
    Row = make_dataclass("Blaze", [("title", str), ("n", int)])
    dumps = replaces()
    store = open_store(Row, store_dir, debounce=0.0, keep=3)

    deadline = time.monotonic() + 1.0
    refused = []
    broke = []

    def worker(seed):
        dice = random.Random(seed)
        mine = []
        done = 0

        try:
            while time.monotonic() < deadline and done < 400:
                done += 1
                roll = dice.random()

                try:
                    if roll < 0.6 or not mine:
                        mine.append(store.add(Row(title=f"w{seed}", n=done)))
                    elif roll < 0.85:
                        store.put(dice.choice(mine), Row(title=f"w{seed}u", n=done))
                    else:
                        store.remove(mine.pop(dice.randrange(len(mine))))
                except KeyError:
                    refused.append(seed)

                time.sleep(0.001)
        except BaseException as e:  # noqa: BLE001 - handed back, not swallowed
            broke.append(e)

    threads = [threading.Thread(target=worker, args=(seed,), daemon=True)
               for seed in range(4)]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join(JOIN)
        assert not thread.is_alive()

    assert not broke, broke

    landed = len(dumps.landed)
    store.add(Row(title="after the fire", n=0))
    assert until(lambda: len(dumps.landed) > landed, timeout=5.0)

    memory = store.all()
    store.close()

    path = file_of(Row, store_dir)
    payload = payload_of(path)
    ids = [row_id for row_id, _ in memory]
    assert [int(key) for key in payload["rows"]] == ids

    again = open_store(Row, store_dir, debounce=0.0, keep=3)
    assert again.all() == memory

    copies = sorted(path.parent.glob(f"{path.stem}.*{path.suffix}"))
    assert len(copies) <= 3, [p.name for p in copies]

    for copy in copies:
        kept = json.loads(copy.read_text(encoding="utf-8"))
        assert kept["v"] == payload["v"]
        assert all(str(int(key)) == key for key in kept["rows"])

    # The dumps really did land while the threads were writing.
    assert len(dumps.landed) > 5, len(dumps.landed)
    note("four threads with debounce=0", rows=len(memory),
         dumps=len(dumps.landed), copies=len(copies),
         key_errors=len(refused))
