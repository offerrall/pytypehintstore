"""The transport: what the file carries, and what comes back out of it.

`pytypehintstore/codec.py` writes a row the way an HTTP client would send it —
ISO text for a date or a time, the member NAME for an enum, an object for a
nested dataclass — and on the way back converts only where the shape is the one
possible reading. Anything ambiguous travels intact so the core rejects it in the
core's own words.

The contract is the file, so every test here goes through a store: add, close,
read the JSON with `json.loads`, reopen, compare the row. `encode`/`decode` are
imported only by `_core_says`, which asks the core in person what it would say
about a row, and every test that does so declares it.

The `{"$type", "$value"}` wrapper is emitted when several options collide on the
WIRE type, and kept after decode only where the core still needs it — that is,
where they also collide on the Python type, as `list[str] | list[int]` does.
"""

from dataclasses import dataclass, field, replace
from datetime import date, time
from enum import Enum, StrEnum
from typing import Annotated, Literal

import pytest

from pytypehint import (
    Description, Extra, IsPassword, Label, Max, Min, OptionalToggle,
    Placeholder, Rows, SchemaTypeError, SchemaValueError, Slider, Step,
    struct_of,
)
from pytypehintstore import StoreLoadError
from pytypehintstore.codec import decode, encode


# ------------------------------------------------------------------- shapes


class Priority(Enum):
    LOW = "low"
    HIGH = "high"
    # Same value as HIGH, so Python makes it an alias rather than a member of
    # its own: Priority.URGENT is Priority.HIGH.
    URGENT = "high"


class Rank(Enum):
    FIRST = 1
    SECOND = 2


class Hue(StrEnum):
    """A str-mixin enum: `Hue[name]` would reach `str.__getitem__`, not the member."""
    RED = "red"
    CRIMSON = "red"


@dataclass
class Scalars:
    n: int
    ratio: float
    name: str
    active: bool
    day: date
    at: time


@dataclass
class Ranked:
    priority: Priority


@dataclass
class Hued:
    hue: Hue


@dataclass
class Maybe:
    note: str | None = None


@dataclass
class Tags:
    names: list[str]


@dataclass
class Calendar:
    days: list[date]


@dataclass
class Matrix:
    rows: list[list[int]]


@dataclass
class Holes:
    values: list[int | None]


@dataclass
class Floats:
    values: list[float]


@dataclass
class Point:
    x: int
    y: int


@dataclass
class Line:
    start: Point
    end: Point


@dataclass
class Track:
    points: list[Point]


@dataclass
class Envelope:
    inner: Point | None = None


@dataclass
class Node:
    name: str
    kids: "list[Node]" = field(default_factory=list)


@dataclass
class Job:
    mode: Literal["fast", "safe"]
    level: Literal[1, 2, 3]


@dataclass
class Mixed:
    value: int | str


@dataclass
class Amount:
    value: int | float


@dataclass
class Ratio:
    value: float


@dataclass
class Stamp:
    when: str | date


@dataclass
class Day:
    when: date


@dataclass
class Circle:
    radius: float


@dataclass
class Square:
    side: float


@dataclass
class Figure:
    shape: Circle | Square


# The int option is written first so that a stored list of str proves the branch
# came from the items and not from the order the options were declared in.
@dataclass
class Query:
    terms: list[int] | list[str]


@dataclass
class Loose:
    terms: list[str | int]


@dataclass
class Shelf:
    figures: list[Circle | Square]


@dataclass
class Batch:
    figures: list[Circle] | list[Square]


@dataclass
class Log:
    entries: list[str | date]


@dataclass
class Grade:
    value: Priority | Rank


@dataclass
class Moment:
    value: date | time


@dataclass
class Narrow:
    # Two list options that share their item type and differ only in a limit.
    items: list[Annotated[str, Max(1)]] | list[str | int]


# A dataclass and an enum are allowed to carry one name: the core keeps a
# namespace per kind. The dataclass is renamed after the fact because two
# classes cannot hold one name in a module.
@dataclass
class _StatusRecord:
    note: str


_StatusRecord.__name__ = _StatusRecord.__qualname__ = "Status"


class Status(Enum):
    OK = "ok"


@dataclass
class Report:
    state: _StatusRecord | Status | str


@dataclass
class Noted:
    name: Annotated[str, Label("Name"), Description("Full name"),
                    Placeholder("Ada"), Rows(2), Extra("store.hint", "wide")]
    secret: Annotated[str, IsPassword()]
    size: Annotated[int, Min(0), Max(10), Step(2), Slider(show_value=True)]
    note: Annotated[str | None, OptionalToggle(True)] = None


# The same fields with the same limits and none of the notation: whatever the
# atoms are worth to a wrapper, these two must reach the file identically.
@dataclass
class Plain:
    name: str
    secret: str
    size: Annotated[int, Min(0), Max(10)]
    note: str | None = None


SCALARS = Scalars(n=7, ratio=1.5, name="note", active=True,
                  day=date(2024, 3, 1), at=time(9, 30))


# ------------------------------------------------------------------ helpers


def _stored(open_store, cls, path, *values):
    """Write `values`, close, reopen: what comes back was read off the file."""
    store = open_store(cls, path, debounce=0)

    for value in values:
        store.add(value)

    store.close()
    return open_store(cls, path, debounce=0)


def _rows(store):
    return [row for _, row in store.all()]


def _load_failure(open_store, cls, path):
    with pytest.raises(StoreLoadError) as error:
        open_store(cls, path, debounce=0)

    return str(error.value)


def _core_says(cls, row):
    """What the core itself says about this row, asked in person.

    The only place the codec is called directly: a store cannot hand a test the
    core's bare message, and quoting one by hand would freeze wording the core
    owns.
    """
    struct = struct_of(cls)

    with pytest.raises((SchemaTypeError, SchemaValueError)) as error:
        struct.build(decode(struct, row))

    return str(error.value)


# ------------------------------------------------------------ scalar atoms


@pytest.mark.parametrize("name, value, written", [
    ("n", 7, 7),
    ("ratio", 1.5, 1.5),
    ("name", "a name with ñ and 東", "a name with ñ and 東"),
    ("active", True, True),
    ("active", False, False),
    ("day", date(2024, 3, 1), "2024-03-01"),
    ("at", time(9, 30), "09:30:00"),
    ("at", time(23, 59, 59), "23:59:59"),
])


def test_a_scalar_survives_the_file_in_its_own_json_form(
        name, value, written, open_store, store_dir, rows_of):
    original = replace(SCALARS, **{name: value})

    store = _stored(open_store, Scalars, store_dir, original)

    assert store.get(1) == original
    assert type(getattr(store.get(1), name)) is type(value)
    assert rows_of(store.path)["1"][name] == written
    assert type(rows_of(store.path)["1"][name]) is type(written)


def test_a_literal_travels_as_the_text_or_number_it_names(
        open_store, store_dir, rows_of):
    """`Literal` compiles to Choices over str or int; the wire sees only those."""
    store = _stored(open_store, Job, store_dir, Job("safe", 2))

    assert store.get(1) == Job("safe", 2)
    assert rows_of(store.path)["1"] == {"mode": "safe", "level": 2}


# -------------------------------------------------------------------- enums


def test_an_enum_member_survives_the_file_as_its_name(
        open_store, store_dir, rows_of):
    store = _stored(open_store, Ranked, store_dir, Ranked(Priority.LOW))

    assert store.get(1) == Ranked(Priority.LOW)
    assert rows_of(store.path)["1"]["priority"] == "LOW"


def test_an_enum_alias_is_written_under_its_canonical_name(
        open_store, store_dir, rows_of):
    """Python resolves the alias before the store sees it; the file records HIGH."""
    store = _stored(open_store, Ranked, store_dir, Ranked(Priority.URGENT))

    assert store.get(1) == Ranked(Priority.HIGH)
    assert rows_of(store.path)["1"]["priority"] == "HIGH"


def test_an_enum_alias_written_by_hand_resolves_to_the_canonical_member(
        open_store, store_dir, by_hand):
    by_hand(Ranked, store_dir, [{"priority": "URGENT"}])

    store = open_store(Ranked, store_dir, debounce=0)

    assert store.get(1) == Ranked(Priority.HIGH)
    assert store.get(1).priority.name == "HIGH"


def test_a_name_the_enum_does_not_carry_travels_intact_and_the_core_rejects_it(
        open_store, store_dir, file_of, by_hand):
    by_hand(Ranked, store_dir, [{"priority": "NOPE"}])

    assert _load_failure(open_store, Ranked, store_dir) == (
        f"{file_of(Ranked, store_dir)}: row 1: priority: expected Priority, got str")


def test_a_str_enum_survives_the_file_as_a_member(
        open_store, store_dir, rows_of):
    """A StrEnum inherits `str.__getitem__`, so the member lookup has to go
    through `__members__`; reading `cls[name]` raised TypeError instead."""
    store = _stored(open_store, Hued, store_dir, Hued(Hue.RED))

    assert store.get(1) == Hued(Hue.RED)
    assert type(store.get(1).hue) is Hue
    assert rows_of(store.path)["1"]["hue"] == "RED"


def test_a_str_enum_alias_written_by_hand_resolves_to_the_canonical_member(
        open_store, store_dir, by_hand):
    """`__members__` carries the aliases too, which is the other half of the
    reason to read the member through it rather than through `cls[name]`."""
    by_hand(Hued, store_dir, [{"hue": "CRIMSON"}])

    store = open_store(Hued, store_dir, debounce=0)

    assert store.get(1) == Hued(Hue.RED)
    assert store.get(1).hue.name == "RED"


# ------------------------------------------------------ `None` and containers


def test_a_none_inside_an_optional_survives_the_file_as_json_null(
        open_store, store_dir, rows_of):
    store = _stored(open_store, Maybe, store_dir, Maybe(None), Maybe("here"))

    assert _rows(store) == [Maybe(None), Maybe("here")]
    assert rows_of(store.path)["1"]["note"] is None


@pytest.mark.parametrize("names", [["red", "green", "blue"], []])
def test_a_list_of_str_survives_the_file_as_a_json_list(
        names, open_store, store_dir, rows_of):
    store = _stored(open_store, Tags, store_dir, Tags(names))

    assert store.get(1) == Tags(names)
    assert rows_of(store.path)["1"]["names"] == names


def test_a_list_of_dates_survives_as_a_list_of_iso_text(
        open_store, store_dir, rows_of):
    original = Calendar([date(2024, 1, 1), date(2024, 12, 31)])

    store = _stored(open_store, Calendar, store_dir, original)

    assert store.get(1) == original
    assert rows_of(store.path)["1"]["days"] == ["2024-01-01", "2024-12-31"]


def test_a_list_of_lists_keeps_its_nesting(open_store, store_dir, rows_of):
    original = Matrix([[1, 2], [3], []])

    store = _stored(open_store, Matrix, store_dir, original)

    assert store.get(1) == original
    assert rows_of(store.path)["1"]["rows"] == [[1, 2], [3], []]


def test_a_list_of_int_or_none_keeps_its_holes(open_store, store_dir, rows_of):
    original = Holes([1, None, 3, None])

    store = _stored(open_store, Holes, store_dir, original)

    assert store.get(1) == original
    assert rows_of(store.path)["1"]["values"] == [1, None, 3, None]


# ------------------------------------------------------------- nested objects


def test_a_nested_dataclass_survives_the_file_as_a_json_object(
        open_store, store_dir, rows_of):
    original = Line(Point(0, 0), Point(3, 4))

    store = _stored(open_store, Line, store_dir, original)

    assert store.get(1) == original
    assert rows_of(store.path)["1"] == {"start": {"x": 0, "y": 0},
                                        "end": {"x": 3, "y": 4}}


def test_a_dataclass_inside_a_list_survives_as_a_list_of_objects(
        open_store, store_dir, rows_of):
    original = Track([Point(1, 2), Point(3, 4)])

    store = _stored(open_store, Track, store_dir, original)

    assert store.get(1) == original
    assert rows_of(store.path)["1"]["points"] == [{"x": 1, "y": 2},
                                                  {"x": 3, "y": 4}]


def test_an_optional_dataclass_survives_present_and_absent(
        open_store, store_dir, rows_of):
    store = _stored(open_store, Envelope, store_dir,
                    Envelope(Point(1, 2)), Envelope(None))

    assert _rows(store) == [Envelope(Point(1, 2)), Envelope(None)]
    assert rows_of(store.path)["1"]["inner"] == {"x": 1, "y": 2}
    assert rows_of(store.path)["2"]["inner"] is None


def test_a_self_referencing_dataclass_survives_the_file_at_every_depth(
        open_store, store_dir, rows_of):
    original = Node("root", [Node("a"), Node("b", [Node("deep")])])

    store = _stored(open_store, Node, store_dir, original)

    assert store.get(1) == original
    assert rows_of(store.path)["1"] == {
        "name": "root",
        "kids": [{"name": "a", "kids": []},
                 {"name": "b", "kids": [{"name": "deep", "kids": []}]}]}


# ------------------------------------------------------ unions without a wrapper


@pytest.mark.parametrize("value", [42, "forty two"])
def test_an_int_or_str_travels_without_a_wrapper(
        value, open_store, store_dir, rows_of):
    """The two options do not share a JSON type, so the file already says which."""
    store = _stored(open_store, Mixed, store_dir, Mixed(value))

    assert store.get(1) == Mixed(value)
    assert rows_of(store.path)["1"]["value"] == value


def test_an_int_or_float_keeps_its_python_type_without_a_wrapper(
        open_store, store_dir, rows_of):
    store = _stored(open_store, Amount, store_dir, Amount(3), Amount(3.0))

    assert [type(row.value) for row in _rows(store)] == [int, float]
    assert type(rows_of(store.path)["1"]["value"]) is int
    assert type(rows_of(store.path)["2"]["value"]) is float


def test_a_hand_written_int_under_int_or_float_stays_an_int(
        open_store, store_dir, by_hand):
    """Both options are numeric, so there is no single reading: the JSON int
    travels intact and the core routes it by its exact type."""
    by_hand(Amount, store_dir, [{"value": 3}])

    store = open_store(Amount, store_dir, debounce=0)

    assert store.get(1) == Amount(3)
    assert type(store.get(1).value) is int


def test_a_hand_written_int_under_a_lone_float_is_read_as_a_float(
        open_store, store_dir, by_hand):
    """`Float` alone is the single numeric reading, so decode coerces."""
    by_hand(Ratio, store_dir, [{"value": 3}])

    store = open_store(Ratio, store_dir, debounce=0)

    assert store.get(1) == Ratio(3.0)
    assert type(store.get(1).value) is float


# ------------------------------------------------------- union: `str | date`


def test_a_date_under_str_or_date_names_its_option_in_the_file(
        open_store, store_dir, rows_of):
    store = _stored(open_store, Stamp, store_dir, Stamp(date(2024, 5, 4)))

    assert store.get(1) == Stamp(date(2024, 5, 4))
    assert type(store.get(1).when) is date
    assert rows_of(store.path)["1"]["when"] == {"$type": "date",
                                                "$value": "2024-05-04"}


def test_a_plain_str_under_str_or_date_names_its_option_too(
        open_store, store_dir, rows_of):
    """A str and a date are both JSON text: the wrapper goes on BOTH or neither.
    (The brief for this module said the plain str carries none; the file says
    otherwise, and it has to — without it the two are indistinguishable.)"""
    store = _stored(open_store, Stamp, store_dir, Stamp("2024-05-04"))

    assert store.get(1) == Stamp("2024-05-04")
    assert type(store.get(1).when) is str
    assert rows_of(store.path)["1"]["when"] == {"$type": "str",
                                                "$value": "2024-05-04"}


def test_a_date_and_an_iso_looking_string_stay_apart_in_one_file(
        open_store, store_dir):
    """The classic trap: the same ten characters, two different rows."""
    store = _stored(open_store, Stamp, store_dir,
                    Stamp(date(2024, 5, 4)), Stamp("2024-05-04"))

    assert _rows(store) == [Stamp(date(2024, 5, 4)), Stamp("2024-05-04")]
    assert [type(row.when) for row in _rows(store)] == [date, str]


def test_the_wrapper_of_str_or_date_is_consumed_before_the_core_sees_it(
        open_store, store_dir, by_hand):
    """The collision was the wire's alone — the core keeps a str and a date
    apart by Python type — so decode resolves it and hands over a bare value."""
    by_hand(Stamp, store_dir, [{"when": {"$type": "date", "$value": "2024-05-04"}},
                         {"when": {"$type": "str", "$value": "2024-05-04"}}])

    store = open_store(Stamp, store_dir, debounce=0)

    assert _rows(store) == [Stamp(date(2024, 5, 4)), Stamp("2024-05-04")]


# -------------------------------------------------- unions of dataclasses


@pytest.mark.parametrize("shape, written", [
    (Circle(1.5), {"$type": "Circle", "radius": 1.5}),
    (Square(2.0), {"$type": "Square", "side": 2.0}),
])
def test_a_union_of_dataclasses_names_the_class_inside_the_object(
        shape, written, open_store, store_dir, rows_of):
    """A dataclass never takes the wrapper: the core reads its `$type` inline."""
    store = _stored(open_store, Figure, store_dir, Figure(shape))

    assert store.get(1) == Figure(shape)
    assert rows_of(store.path)["1"]["shape"] == written


def test_a_list_of_dataclass_options_discriminates_every_element(
        open_store, store_dir, rows_of):
    original = Shelf([Circle(1.5), Square(2.0)])

    store = _stored(open_store, Shelf, store_dir, original)

    assert store.get(1) == original
    assert rows_of(store.path)["1"]["figures"] == [
        {"$type": "Circle", "radius": 1.5}, {"$type": "Square", "side": 2.0}]


# -------------------------------------------------- union: `list[X] | list[Y]`


@pytest.mark.parametrize("terms, option", [
    (["a", "b"], "list[str]"),
    ([1, 2], "list[int]"),
])
def test_a_union_of_lists_wraps_the_whole_list_and_names_its_option(
        terms, option, open_store, store_dir, rows_of):
    store = _stored(open_store, Query, store_dir, Query(terms))

    assert store.get(1) == Query(terms)
    assert rows_of(store.path)["1"]["terms"] == {"$type": option, "$value": terms}


def test_the_branch_of_a_union_of_lists_comes_from_the_items_not_the_declaration(
        open_store, store_dir, rows_of):
    """`Query.terms` declares list[int] first; a list of str must still say so."""
    store = _stored(open_store, Query, store_dir, Query(["a"]))

    assert rows_of(store.path)["1"]["terms"]["$type"] == "list[str]"


def test_the_wrapper_of_a_union_of_lists_is_kept_for_the_core(
        open_store, store_dir, by_hand):
    """Both options are `list` to the core too, so the discriminator is still
    the only thing that tells them apart after decode has run."""
    by_hand(Query, store_dir, [{"terms": {"$type": "list[str]", "$value": ["a"]}},
                         {"terms": {"$type": "list[int]", "$value": [1]}}])

    store = open_store(Query, store_dir, debounce=0)

    assert _rows(store) == [Query(["a"]), Query([1])]


def test_an_empty_list_under_a_union_of_lists_falls_back_to_the_declared_order(
        open_store, store_dir, rows_of):
    """An empty list is evidence of nothing, so the first option names it. The
    value is the same either way, and that is what the round trip has to keep."""
    store = _stored(open_store, Query, store_dir, Query([]))

    assert store.get(1) == Query([])
    assert rows_of(store.path)["1"]["terms"] == {"$type": "list[int]", "$value": []}


def test_a_bare_list_under_a_union_of_lists_travels_intact_and_the_core_asks_for_the_wrapper(
        open_store, store_dir, file_of, by_hand):
    row = {"terms": ["a"]}
    by_hand(Query, store_dir, [row])

    assert _load_failure(open_store, Query, store_dir) == (
        f"{file_of(Query, store_dir)}: row 1: {_core_says(Query, row)}")


def test_a_union_of_lists_of_dataclasses_wraps_the_whole_list_instead(
        open_store, store_dir, rows_of):
    """list[Circle | Square] discriminates each element; list[Circle] |
    list[Square] wraps the list, and the elements then go plain."""
    original = Batch([Circle(1.5), Circle(2.5)])

    store = _stored(open_store, Batch, store_dir, original)

    assert store.get(1) == original
    assert rows_of(store.path)["1"]["figures"] == {
        "$type": "list[Circle]", "$value": [{"radius": 1.5}, {"radius": 2.5}]}


def test_a_list_of_mixed_items_travels_bare_and_routes_every_element(
        open_store, store_dir, rows_of):
    """`list[str | int]` is one list, not a choice of lists: nothing is named."""
    original = Loose(["a", 1, "b", 2])

    store = _stored(open_store, Loose, store_dir, original)

    assert store.get(1) == original
    assert [type(item) for item in store.get(1).terms] == [str, int, str, int]
    assert rows_of(store.path)["1"]["terms"] == ["a", 1, "b", 2]


def test_mixed_items_and_a_union_of_lists_do_not_share_a_transport(
        open_store, store_dir, rows_of):
    """The same two values, written two ways by two different hints.

    One directory: the two hints are two schemas, so they are two files.
    """
    loose = _stored(open_store, Loose, store_dir, Loose(["a", "b"]))
    query = _stored(open_store, Query, store_dir, Query(["a", "b"]))

    assert rows_of(loose.path)["1"]["terms"] == ["a", "b"]
    assert rows_of(query.path)["1"]["terms"] == {"$type": "list[str]",
                                                 "$value": ["a", "b"]}


# ---------------------------------------------- options colliding on the wire


def test_list_items_that_collide_only_on_the_wire_name_their_option(
        open_store, store_dir, rows_of):
    """A str and a date are both text in JSON, inside a list as much as outside."""
    original = Log(["2024-01-01", date(2024, 1, 1)])

    store = _stored(open_store, Log, store_dir, original)

    assert store.get(1) == original
    assert [type(item) for item in store.get(1).entries] == [str, date]
    assert rows_of(store.path)["1"]["entries"] == [
        {"$type": "str", "$value": "2024-01-01"},
        {"$type": "date", "$value": "2024-01-01"}]


@pytest.mark.parametrize("member, written", [
    (Priority.LOW, {"$type": "Priority", "$value": "LOW"}),
    (Rank.SECOND, {"$type": "Rank", "$value": "SECOND"}),
])
def test_a_union_of_two_enums_names_its_option_on_the_wire(
        member, written, open_store, store_dir, rows_of):
    """Both write a member name; the core keeps them apart by class, so the
    wrapper is the wire's business alone and decode consumes it."""
    store = _stored(open_store, Grade, store_dir, Grade(member))

    assert store.get(1) == Grade(member)
    assert type(store.get(1).value) is type(member)
    assert rows_of(store.path)["1"]["value"] == written


@pytest.mark.parametrize("value, written", [
    (date(2024, 5, 4), {"$type": "date", "$value": "2024-05-04"}),
    (time(9, 30), {"$type": "time", "$value": "09:30:00"}),
])
def test_a_date_or_time_union_names_its_option_and_comes_back_typed(
        value, written, open_store, store_dir, rows_of):
    store = _stored(open_store, Moment, store_dir, Moment(value))

    assert store.get(1) == Moment(value)
    assert type(store.get(1).value) is type(value)
    assert rows_of(store.path)["1"]["value"] == written


def test_an_enum_sharing_its_name_with_a_dataclass_survives_as_a_member(
        open_store, store_dir, rows_of):
    """A Struct and an Enum may carry one name — the core guards a namespace per
    kind — and both answer `option_id()` with it. Only the enum can be behind a
    wrapper, because a Struct is never written that way, so that is the only
    kind decode may look at; searching all of them handed the text to the
    dataclass branch and the member came back a plain str."""
    store = _stored(open_store, Report, store_dir, Report(Status.OK))

    assert store.get(1) == Report(Status.OK)
    assert type(store.get(1).state) is Status
    assert rows_of(store.path)["1"]["state"] == {"$type": "Status",
                                                 "$value": "OK"}


# ------------------------------------------------ what decode refuses to read


@pytest.mark.parametrize("cls, row, leaf", [
    (Ratio, {"value": True}, "value: expected float, got bool"),
    (Floats, {"values": [True]}, "values: [0]: expected float, got bool"),
    (Query, {"terms": {"$type": "list[int]", "$value": [True]}},
     "terms: $value: [0]: expected int, got bool"),
])
def test_a_json_true_is_never_read_as_a_number(
        cls, row, leaf, open_store, store_dir, file_of, by_hand):
    """`bool` subclasses `int` in Python but is its own type to the core, and
    decode's numeric branch tests `type(value) is int`, which excludes it — bare,
    inside a list, and inside a `$value` alike."""
    by_hand(cls, store_dir, [row])

    assert _load_failure(open_store, cls, store_dir) == (
        f"{file_of(cls, store_dir)}: row 1: {leaf}")


def test_an_unknown_key_in_a_row_travels_intact_and_the_core_rejects_it(
        open_store, store_dir, file_of, by_hand):
    row = {"x": 1, "y": 2, "z": 3}
    by_hand(Point, store_dir, [row])

    message = _load_failure(open_store, Point, store_dir)

    assert message == f"{file_of(Point, store_dir)}: row 1: {_core_says(Point, row)}"
    assert "unexpected" in message


def test_an_unknown_option_in_a_wrapper_travels_intact_and_the_core_rejects_it(
        open_store, store_dir, file_of, by_hand):
    row = {"terms": {"$type": "list[nope]", "$value": ["a"]}}
    by_hand(Query, store_dir, [row])

    assert _load_failure(open_store, Query, store_dir) == (
        f"{file_of(Query, store_dir)}: row 1: {_core_says(Query, row)}")


def test_an_unknown_dataclass_name_travels_intact_and_the_core_rejects_it(
        open_store, store_dir, file_of, by_hand):
    row = {"shape": {"$type": "Blob", "radius": 1.0}}
    by_hand(Figure, store_dir, [row])

    assert _load_failure(open_store, Figure, store_dir) == (
        f"{file_of(Figure, store_dir)}: row 1: {_core_says(Figure, row)}")


def test_text_that_is_not_a_date_travels_intact_and_the_core_names_the_field(
        open_store, store_dir, file_of, by_hand):
    """`fromisoformat` raising is not an answer a reader could act on; the codec
    keeps the text and the core reports it against the field."""
    by_hand(Day, store_dir, [{"when": "2024-13-99"}])

    message = _load_failure(open_store, Day, store_dir)

    assert message == f"{file_of(Day, store_dir)}: row 1: when: expected date, got str"
    assert "fromisoformat" not in message


def test_a_value_the_named_option_cannot_read_keeps_its_wrapper(
        open_store, store_dir, file_of, by_hand):
    """The file says `date` and the text is not one. Consuming the wrapper here
    would file the value under whatever else the union takes — `str` — and the
    row would load with a type nobody wrote. The wrapper stays on so the core
    judges the value against the option the file actually named."""
    row = {"when": {"$type": "date", "$value": "2024-13-99"}}
    by_hand(Stamp, store_dir, [row])

    assert _load_failure(open_store, Stamp, store_dir) == (
        f"{file_of(Stamp, store_dir)}: row 1: {_core_says(Stamp, row)}")


# ------------------------------------------------------------ a declared limit


def test_a_union_of_lists_that_differ_only_in_a_limit_can_name_the_wrong_branch(
        open_store, store_dir, by_hand):
    """A limit, not a type, separates `list[Annotated[str, Max(1)]]` from
    `list[str | int]`, and the codec picks the branch by reading item TYPES
    alone: `["ab"]` is a value the schema accepts on the second option, but the
    file names the first and the core then refuses it. Asking the right question
    would mean calling the core's private value validator — the core publishes
    none — so this is a limit of the transport, fixed here on purpose. The
    evidence that the value is legal: the same list loads when the file names
    the other option by hand."""
    store = open_store(Narrow, store_dir, debounce=0)

    with pytest.raises(SchemaValueError, match=r"too long: 2 chars, maximum 1"):
        store.add(Narrow(["ab"]))

    store.close()
    by_hand(Narrow, store_dir, [{"items": {"$type": "list[str | int]",
                                    "$value": ["ab"]}}])

    assert open_store(Narrow, store_dir, debounce=0).get(1) == Narrow(["ab"])


# ------------------------------------------------------------ notation atoms


def test_the_transport_carries_no_trace_of_notation_atoms(
        open_store, store_dir, rows_of):
    """Label, Description, Placeholder, Step, Slider, IsPassword, Rows, Extra
    and OptionalToggle describe a form, not a file."""
    values = ("Ada Lovelace", "hunter2", 4, "first")

    noted = _stored(open_store, Noted, store_dir, Noted(*values))
    plain = _stored(open_store, Plain, store_dir, Plain(*values))

    assert rows_of(noted.path) == rows_of(plain.path)


def test_a_file_written_for_the_plain_dataclass_loads_under_the_notation_atoms(
        open_store, store_dir, by_hand):
    """Notation asks nothing of the wire, so a row laid out without a thought
    for it is a complete row."""
    by_hand(Noted, store_dir, [{"name": "Ada", "secret": "hunter2", "size": 4,
                          "note": None}])

    store = open_store(Noted, store_dir, debounce=0)

    assert _rows(store) == [Noted(name="Ada", secret="hunter2", size=4)]
