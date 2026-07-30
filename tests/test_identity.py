"""The class is the database.

A store is identified by the name of its dataclass and the fingerprint of its
compiled schema, so the file is `{ClassName}.{hash}.json`. Anything that moves
the schema — a type, a limit, a default, the order of the fields, a Label —
moves the hash, and a moved hash is a different database: a new file, with the
old one left beside it, whole and readable.

No exceptions and no small print. That is the whole contract, and it is what
lets there be no migration: if a file opens, every row in it is valid against
this exact schema.
"""

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Annotated

import pytest

from pytypehint import Label, Max, Min, Pattern, struct_of
from pytypehintstore.fingerprint import fingerprint
from shared import Tag, Task


class Priority(Enum):
    LOW = "low"
    HIGH = "high"


@dataclass
class Note:
    body: Annotated[str, Min(1)] = "empty"


@dataclass
class Reference:
    """One class carrying every kind of thing the fingerprint has to render."""

    title: Annotated[str, Min(1), Max(80), Label("Title")]
    priority: Priority
    due: str | date = ""
    note: Note | None = None
    scores: list[list[int]] = field(default_factory=list)


# The pinned value. If this moves, either the class above changed or the way a
# schema is rendered changed — and a rendering that moves renames every database
# in the world on the next release. That is a breaking change to declare, never
# an accident to absorb.
REFERENCE_HASH = '9d63f00a'


def a_reference(**edits):
    values = dict(title="first", priority=Priority.LOW)
    values.update(edits)
    return Reference(**values)


# ---- the fingerprint itself -------------------------------------------------


def test_the_fingerprint_of_the_reference_class_is_pinned():
    assert fingerprint(struct_of(Reference)) == REFERENCE_HASH


def test_the_fingerprint_is_stable_across_compilations():
    """Two compilations of one class are one database.

    `scores` carries a default_factory: if the factory reached the text as an
    object rather than as the value it makes, every compilation would render a
    different address and every run would open a different file.
    """
    assert fingerprint(struct_of(Reference)) == fingerprint(struct_of(Reference))


def test_a_value_with_no_stable_text_is_refused_rather_than_hashed():
    """The core's whole vocabulary has stable text today, so nothing a class can
    declare reaches this. It is the guard for the day the core grows a type this
    does not know: a repr would carry a memory address, and a hash built on one
    renames the database on the next run."""
    with pytest.raises(TypeError) as refusal:
        fingerprint({"odd": object()})

    assert "no stable text" in str(refusal.value)


# ---- what the identity is made of -------------------------------------------


def test_one_changed_character_is_another_database():
    """Two classes alike but for a Max are two schemas, so two databases."""
    @dataclass
    class Room:
        name: Annotated[str, Max(8)]

    narrow = struct_of(Room)

    @dataclass
    class Room:  # noqa: F811 — the same class after one character moved
        name: Annotated[str, Max(9)]

    wide = struct_of(Room)

    assert fingerprint(narrow) != fingerprint(wide)


def test_a_pattern_is_part_of_the_identity():
    """The regex text has to reach the hash through a field the core compares.

    A compiled pattern has no stable text, so the dump leaves it out. If the
    source text lived only there, two classes differing in nothing but their
    regex would share one file — and the atom most likely to be edited would be
    the one the identity could not see.
    """
    @dataclass
    class Code:
        value: Annotated[str, Pattern(r"^[a-z]+$")]

    lower = struct_of(Code)

    @dataclass
    class Code:  # noqa: F811 — the same class with the regex rewritten
        value: Annotated[str, Pattern(r"^[A-Z]+$")]

    upper = struct_of(Code)

    assert fingerprint(lower) != fingerprint(upper)


def test_a_notation_atom_is_part_of_the_identity():
    """A Label changes no value the file can carry, and it is still another
    database. The contract has no small print: everything in the schema counts,
    which is why it fits in one sentence."""
    @dataclass
    class Plain:
        name: str

    bare = struct_of(Plain)

    @dataclass
    class Plain:  # noqa: F811
        name: Annotated[str, Label("Name")]

    labelled = struct_of(Plain)

    assert fingerprint(bare) != fingerprint(labelled)


# ---- what that buys ---------------------------------------------------------


def test_the_old_database_survives_a_schema_change_untouched(
        open_store, store_dir, file_of):
    """The point of the whole design: changing the class cannot lose the rows
    that were written under the old one."""
    @dataclass
    class Ticket:
        title: str

    before = open_store(Ticket, store_dir)
    before.add(Ticket(title="from the old schema"))
    before.close()

    old_file = file_of(Ticket, store_dir)
    old_bytes = old_file.read_bytes()

    @dataclass
    class Ticket:  # noqa: F811 — one field more
        title: str
        note: str = "unfiled"

    after = open_store(Ticket, store_dir)

    assert after.all() == [], "the new schema read rows it cannot vouch for"
    assert file_of(Ticket, store_dir) != old_file
    assert old_file.read_bytes() == old_bytes, "the old database was touched"


def test_two_classes_share_a_directory_without_touching_each_other(
        open_store, store_dir, file_of, lock_of, rows_of):
    tasks = open_store(Task, store_dir, debounce=0)
    tags = open_store(Tag, store_dir, debounce=0)

    tasks.add(Task(title="a task"))
    tags.add(Tag(name="a tag"))

    assert lock_of(file_of(Task, store_dir)).exists()
    assert lock_of(file_of(Tag, store_dir)).exists()

    tasks.close()
    tags.close()

    assert list(rows_of(file_of(Task, store_dir))["1"]) == ["title", "priority", "done"]
    assert list(rows_of(file_of(Tag, store_dir))["1"]) == ["name"]


def test_the_file_is_named_for_the_class_and_its_fingerprint(open_store, store_dir):
    store = open_store(Reference, store_dir)

    assert store.path.name == f"Reference.{REFERENCE_HASH}.json"
    assert store.path.parent == store_dir.resolve()


def test_rows_written_under_one_schema_are_invisible_to_the_next(
        open_store, store_dir):
    """The load either rebuilds every row against this schema or fails; it never
    reads a row that was written for another one."""
    store = open_store(Reference, store_dir, debounce=0)
    store.add(a_reference(title="kept"))
    store.close()

    @dataclass
    class Narrower:
        title: Annotated[str, Min(1), Max(40), Label("Title")]
        priority: Priority
        due: str | date = ""
        note: Note | None = None
        scores: list[list[int]] = field(default_factory=list)

    # The same class name, so only the moved limit can tell the two files
    # apart: the fingerprint is doing the work here, not the name.
    Narrower.__name__ = "Reference"

    assert open_store(Narrower, store_dir).all() == []
    assert store.path.exists(), "the rows written before are gone"
