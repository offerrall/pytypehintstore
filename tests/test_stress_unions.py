"""Stress on the codec's union routing: what the wrapper names, and who reads it.

`codec.py` decides with two notions. `_wire_type` is the JSON type a value is
written as — a str, a date, a time and an enum member all arrive as text — and
it decides whether an option must be NAMED on the wire, in the
`{"$type", "$value"}` wrapper. `_core_type` is the Python type the core routes
on once decode has turned that text back into values, and it decides whether the
wrapper survives decode: it does only where two options still collide as Python
types (`list[str] | list[int]`), or where the text did not read as the option the
file names.

Every test here asks for the four properties by name:

  P1  add -> get returns == obj
  P2  close + reopen returns the same
  P3  the file is legible transport, and a hand-written row is either understood
      or refused out loud at open
  P4  build(decode(json.loads(json.dumps(encode(schema, obj))))) == obj

`1 == 1.0` and `True == 1` in Python, so a dataclass comparison can pass with the
type swapped underneath it. Wherever the Python type is the point, these tests
assert `type(...) is ...` on top of the equality.

Nothing here touches src/. Where a test documents a defect it is
`xfail(strict=True)` and asserts the behaviour the properties ask for, so it
turns green the day the defect goes.
"""

import json
import math
from dataclasses import dataclass, field
from datetime import date, time
from enum import Enum
from pathlib import Path

import pytest

from pytypehint import SchemaTypeError, struct_of
from pytypehintstore import StoreError, StoreLoadError
from pytypehintstore.codec import decode, encode

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def wire_of(cls, obj):
    """What the file would carry for `obj`, after a trip through real JSON."""
    return json.loads(json.dumps(encode(struct_of(cls), obj)))


def p4(cls, obj):
    """P4: build(decode(json.loads(json.dumps(encode(schema, obj)))))."""
    schema = struct_of(cls)
    return schema.build(decode(schema, wire_of(cls, obj)))


def cycle(open_store, store_dir, cls, objs):
    """P1 and P2 in one pass, plus the rows the file ended up carrying.

    Every object is added to one store, read straight back (P1), the store is
    closed so the dump lands, the file is read as text, and a second store on the
    same directory reads every row again (P2). `debounce=0` and `close()` keep
    the writer out of the way; nothing here sleeps.
    """
    store = open_store(cls, store_dir, debounce=0)
    ids = [store.add(obj) for obj in objs]
    added = [store.get(i) for i in ids]
    path = store.path
    store.close()

    rows = json.loads(Path(path).read_text(encoding="utf-8"))["rows"]

    again = open_store(cls, store_dir, debounce=0)
    reopened = [again.get(i) for i in ids]
    again.close()

    return added, reopened, [rows[str(i)] for i in ids]


# --------------------------------------------------------------------------
# shapes
# --------------------------------------------------------------------------


class Kind(Enum):
    LOW = "low"
    HIGH = "high"


@dataclass
class FourText:
    """Four options that are all text on the wire, so all four get named."""
    v: str | date | time | Kind


@dataclass
class FourTextItems:
    v: list[str | date | time | Kind]


class MemberNames(Enum):
    """Members named after the option_id of every sibling branch."""
    str = "s"
    date = "d"
    time = "t"
    list = "l"


@dataclass
class MemberClash:
    v: str | date | time | MemberNames


# The functional API is the only way in: `class E(Enum): 2026-01-15 = 1` is a
# SyntaxError, but Enum(...) never asks a member name to be an identifier.
IsoNamed = Enum("IsoNamed", ["2026-01-15", "09:30:00", "OTHER"])


@dataclass
class IsoNamedBox:
    v: str | date | time | IsoNamed


class Alpha(Enum):
    LOW = "low"
    HIGH = "high"


class Beta(Enum):
    """The same member NAMES as Alpha, and the same values."""
    LOW = "low"
    HIGH = "high"


@dataclass
class TwoEnums:
    v: Alpha | Beta


@dataclass
class TwoLists:
    v: list[str] | list[int]


@dataclass
class TextLists:
    """Both branches hold items that are text on the wire."""
    v: list[str] | list[date]


@dataclass
class TextItems:
    v: list[str | date]


@dataclass
class Shaped:
    """Every field name of Twinned, and one field typed differently."""
    x: int
    y: str


@dataclass
class Twinned:
    x: str
    y: str


@dataclass
class TwinLists:
    v: list[Shaped] | list[Twinned]


@dataclass
class Narrow:
    x: int


@dataclass
class Wide:
    """Narrow's fields plus one, and the extra one has a default."""
    x: int
    extra: str = "d"


@dataclass
class WideFirst:
    v: Wide | Narrow


@dataclass
class NarrowFirst:
    v: Narrow | Wide


@dataclass
class Branch:
    name: str
    child: "Branch | Tip | None" = None


@dataclass
class Tip:
    name: str


@dataclass
class Recursive:
    v: Branch | Tip


@dataclass
class Holes:
    """Optional[list[Optional[A | B]]] — every hole a union of dataclasses has."""
    v: list[Shaped | Twinned | None] | None = None


@dataclass
class Numbers:
    v: int | float


@dataclass
class NumberLists:
    v: list[int] | list[float]


def _dataclass_named(name, hint):
    """A dataclass whose public identity is `name`, whatever its scope."""
    @dataclass
    class Thing:
        x: hint

    Thing.__name__ = name
    Thing.__qualname__ = name
    return Thing


def _enum_class_named_str():
    """An enum whose class name really is "str" — no reflection involved.

    Shadowing the builtin inside a function is legal Python and leaves the
    builtin alone everywhere else. `EnumShape.option_id()` is the bare class
    name, so this enum's public identity is "str", exactly the Str option's.
    """
    class str(Enum):  # noqa: A001 - the point of the fixture
        LOW = "low"

    return str


ShadowStr = _enum_class_named_str()

# An enum whose class name is "date" AND whose member is named like an ISO date:
# both halves of the wrapper collide with the Date option's.
ShadowDate = Enum("date", ["2026-01-15", "OTHER"])  # type: ignore[misc]


@dataclass
class StrThenEnum:
    v: str | ShadowStr  # type: ignore[valid-type]


@dataclass
class EnumThenDate:
    v: ShadowDate | date


@dataclass
class DateThenEnum:
    v: date | ShadowDate


# --------------------------------------------------------------------------
# 1. str | date | time | Enum: four text options on one wire type
# --------------------------------------------------------------------------


def test_four_text_options_each_come_back_with_their_own_python_type(
        open_store, store_dir):
    # The str is deliberately an ISO date: nothing but the wrapper tells it from
    # the date option beside it.
    values = ["hello", "2026-01-15", "09:30:00",
              date(2026, 1, 15), time(9, 30), Kind.LOW]
    objs = [FourText(v=v) for v in values]

    added, reopened, _ = cycle(open_store, store_dir, FourText, objs)

    for obj, got, back in zip(objs, added, reopened):
        assert got == obj and back == obj                       # P1, P2
        assert type(got.v) is type(obj.v)
        assert type(back.v) is type(obj.v)
        assert p4(FourText, obj) == obj                         # P4
        assert type(p4(FourText, obj).v) is type(obj.v)


def test_four_text_options_are_each_named_by_their_option_id_on_the_wire(
        open_store, store_dir):
    values = ["2026-01-15", date(2026, 1, 15), time(9, 30), Kind.LOW]
    objs = [FourText(v=v) for v in values]

    _, _, rows = cycle(open_store, store_dir, FourText, objs)

    # P3: four options share the wire type str, so every one of them travels
    # named, and the file says which reading it wants.
    assert [row["v"] for row in rows] == [
        {"$type": "str", "$value": "2026-01-15"},
        {"$type": "date", "$value": "2026-01-15"},
        {"$type": "time", "$value": "09:30:00"},
        {"$type": "Kind", "$value": "LOW"},
    ]


def test_an_enum_member_named_like_an_iso_date_needs_the_functional_api():
    # The perverse case the wrapper has to survive is constructible, but not
    # with class syntax: a member name there is an identifier or nothing.
    with pytest.raises(SyntaxError):
        compile("class E(Enum):\n    2026-01-15 = 1\n", "<test>", "exec")

    # Enum(...) never asks, so the member exists and carries a name that reads
    # as a date and as a time.
    assert [m.name for m in IsoNamed] == ["2026-01-15", "09:30:00", "OTHER"]


def test_enum_members_named_like_an_iso_date_still_route_to_the_enum(
        open_store, store_dir):
    values = ["2026-01-15", date(2026, 1, 15), time(9, 30),
              IsoNamed["2026-01-15"], IsoNamed["09:30:00"], IsoNamed.OTHER]
    objs = [IsoNamedBox(v=v) for v in values]

    added, reopened, rows = cycle(open_store, store_dir, IsoNamedBox, objs)

    for obj, got, back in zip(objs, added, reopened):
        assert got == obj and back == obj
        assert type(got.v) is type(obj.v)
        assert type(back.v) is type(obj.v)
        assert p4(IsoNamedBox, obj) == obj

    # The member name and the ISO text are the same characters; only "$type"
    # keeps the enum apart from the date and from the plain str.
    assert rows[0]["v"] == {"$type": "str", "$value": "2026-01-15"}
    assert rows[1]["v"] == {"$type": "date", "$value": "2026-01-15"}
    assert rows[3]["v"] == {"$type": "IsoNamed", "$value": "2026-01-15"}


def test_four_text_options_inside_a_list_wrap_every_item(open_store, store_dir):
    values = ["2026-01-15", date(2026, 1, 15), time(9, 30), Kind.LOW]
    obj = FourTextItems(v=values)

    added, reopened, rows = cycle(open_store, store_dir, FourTextItems, [obj])

    assert added[0] == obj and reopened[0] == obj
    assert [type(x) for x in reopened[0].v] == [str, date, time, Kind]
    # The list itself is the single list option, so it travels bare; the items
    # collide with each other and each carries its own wrapper.
    assert rows[0]["v"] == [
        {"$type": "str", "$value": "2026-01-15"},
        {"$type": "date", "$value": "2026-01-15"},
        {"$type": "time", "$value": "09:30:00"},
        {"$type": "Kind", "$value": "LOW"},
    ]


# --------------------------------------------------------------------------
# 2. an enum member named like a sibling branch's option_id
# --------------------------------------------------------------------------


def test_enum_members_named_after_sibling_option_ids_never_capture_them(
        open_store, store_dir):
    # MemberNames.str is written as $value "str" under $type "MemberNames"; the
    # plain string "str" is written as $value "str" under $type "str". The two
    # halves of the wrapper live in different namespaces, so neither reading
    # reaches the other.
    values = ["str", "date", "time", "list",
              date(2026, 1, 15), time(1, 2),
              MemberNames.str, MemberNames.date, MemberNames.time,
              MemberNames.list]
    objs = [MemberClash(v=v) for v in values]

    added, reopened, rows = cycle(open_store, store_dir, MemberClash, objs)

    for obj, got, back in zip(objs, added, reopened):
        assert got == obj and back == obj
        assert type(got.v) is type(obj.v)
        assert type(back.v) is type(obj.v)
        assert p4(MemberClash, obj) == obj

    assert rows[0]["v"] == {"$type": "str", "$value": "str"}
    assert rows[6]["v"] == {"$type": "MemberNames", "$value": "str"}


# --------------------------------------------------------------------------
# 3. two different enums whose members share names
# --------------------------------------------------------------------------


def test_two_enums_sharing_member_names_and_values_keep_their_own_class(
        open_store, store_dir):
    objs = [TwoEnums(v=v) for v in (Alpha.LOW, Beta.LOW, Alpha.HIGH, Beta.HIGH)]

    added, reopened, rows = cycle(open_store, store_dir, TwoEnums, objs)

    for obj, got, back in zip(objs, added, reopened):
        # Alpha.LOW != Beta.LOW, but both render as "LOW": only $type says which.
        assert got == obj and back == obj
        assert type(got.v) is type(obj.v)
        assert type(back.v) is type(obj.v)
        assert p4(TwoEnums, obj) == obj

    assert [row["v"] for row in rows] == [
        {"$type": "Alpha", "$value": "LOW"},
        {"$type": "Beta", "$value": "LOW"},
        {"$type": "Alpha", "$value": "HIGH"},
        {"$type": "Beta", "$value": "HIGH"},
    ]


def test_a_bare_member_name_for_two_enums_is_refused_at_open(
        by_hand, open_store, store_dir):
    # P3: a person who drops the wrapper leaves a row that names no reading.
    # The core does not guess between two enums, and the store fails at open
    # rather than filing the row under one of them.
    by_hand(TwoEnums, store_dir, [{"v": "LOW"}])

    with pytest.raises(StoreLoadError, match="expected Alpha . Beta, got str"):
        open_store(TwoEnums, store_dir, debounce=0)


def test_two_enums_sharing_a_class_name_are_refused_when_the_schema_compiles():
    # The other half of the same concern: two enums named alike would compile to
    # one "$type" and the file could not say which. The core refuses to build
    # the schema at all, so the store never opens on it.
    twin = Enum("Alpha", ["LOW"])

    @dataclass
    class Ambiguous:
        v: Alpha | twin

    with pytest.raises(ValueError, match="duplicate discriminator name"):
        struct_of(Ambiguous)


# --------------------------------------------------------------------------
# 4. list[str] | list[int]
# --------------------------------------------------------------------------


def test_list_of_str_and_list_of_int_are_settled_by_reading_their_items(
        open_store, store_dir):
    objs = [TwoLists(v=[]), TwoLists(v=["1"]), TwoLists(v=[1]),
            TwoLists(v=["a", "b"]), TwoLists(v=[1, 2, 3])]

    added, reopened, rows = cycle(open_store, store_dir, TwoLists, objs)

    for obj, got, back in zip(objs, added, reopened):
        assert got == obj and back == obj
        assert [type(x) for x in got.v] == [type(x) for x in obj.v]
        assert [type(x) for x in back.v] == [type(x) for x in obj.v]
        assert p4(TwoLists, obj) == obj

    # Two lists share the Python type as well as the wire type, so the wrapper
    # is what the core still needs and it survives decode.
    assert rows[1]["v"] == {"$type": "list[str]", "$value": ["1"]}
    assert rows[2]["v"] == {"$type": "list[int]", "$value": [1]}


def test_an_empty_list_takes_the_first_list_option(open_store, store_dir):
    # Nothing in [] can choose a branch. The encoder names the first option that
    # accepts it, and the empty list that comes back is equal to the one that
    # went in either way — the decision is visible in the file, not in the value.
    _, reopened, rows = cycle(open_store, store_dir, TwoLists, [TwoLists(v=[])])

    assert reopened[0] == TwoLists(v=[])
    assert rows[0]["v"] == {"$type": "list[str]", "$value": []}


def test_a_thousand_item_list_round_trips_on_both_branches(
        open_store, store_dir):
    ints = list(range(1000))
    texts = [str(i) for i in range(1000)]
    objs = [TwoLists(v=ints), TwoLists(v=texts)]

    added, reopened, rows = cycle(open_store, store_dir, TwoLists, objs)

    assert added == objs and reopened == objs
    assert {type(x) for x in reopened[0].v} == {int}
    assert {type(x) for x in reopened[1].v} == {str}
    assert rows[0]["v"]["$type"] == "list[int]"
    assert rows[1]["v"]["$type"] == "list[str]"
    assert p4(TwoLists, objs[0]) == objs[0]


def test_a_bare_list_in_the_file_is_refused_at_open(
        by_hand, open_store, store_dir):
    # P3: with two list options a bare list names no reading, and the core says
    # so instead of filing it under the first one.
    by_hand(TwoLists, store_dir, [{"v": ["a"]}])

    with pytest.raises(StoreLoadError, match="ambiguous list"):
        open_store(TwoLists, store_dir, debounce=0)


def test_a_wrapper_naming_the_wrong_list_option_is_refused_at_open(
        by_hand, open_store, store_dir):
    by_hand(TwoLists, store_dir, [{"v": {"$type": "list[int]", "$value": ["a"]}}])

    with pytest.raises(StoreLoadError, match="expected int, got str"):
        open_store(TwoLists, store_dir, debounce=0)


def test_a_wrapper_naming_an_option_that_does_not_exist_is_refused_at_open(
        by_hand, open_store, store_dir):
    by_hand(TwoLists, store_dir, [{"v": {"$type": "list[float]", "$value": [1.0]}}])

    with pytest.raises(StoreLoadError, match="not a choice"):
        open_store(TwoLists, store_dir, debounce=0)


# --------------------------------------------------------------------------
# 5. list[str] | list[date]: the items are text on the wire too
# --------------------------------------------------------------------------


def test_iso_text_meant_as_str_survives_a_union_with_a_list_of_dates(
        open_store, store_dir):
    # The candidate for a silent type loss: ["2026-01-15"] wanting list[str],
    # beside a list[date] whose items would be written with exactly the same
    # characters. The outer wrapper names list[str], and decode reads the items
    # under that branch alone, where Str is the one reading — so the item is
    # never offered to date.fromisoformat.
    as_text = TextLists(v=["2026-01-15"])
    as_dates = TextLists(v=[date(2026, 1, 15)])

    added, reopened, rows = cycle(
        open_store, store_dir, TextLists, [as_text, as_dates])

    assert added[0] == as_text and reopened[0] == as_text
    assert type(reopened[0].v[0]) is str
    assert added[1] == as_dates and reopened[1] == as_dates
    assert type(reopened[1].v[0]) is date
    assert p4(TextLists, as_text) == as_text
    assert type(p4(TextLists, as_text).v[0]) is str

    # The two rows carry the very same $value; the outer $type is the whole
    # difference, and no item carries a wrapper of its own.
    assert rows[0]["v"] == {"$type": "list[str]", "$value": ["2026-01-15"]}
    assert rows[1]["v"] == {"$type": "list[date]", "$value": ["2026-01-15"]}


def test_a_list_of_str_or_date_items_wraps_each_item_instead(
        open_store, store_dir):
    # The same collision one level down: here the two readings meet inside one
    # list, no outer wrapper can name them, and each item is named on its own.
    obj = TextItems(v=["2026-01-15", date(2026, 1, 15)])

    added, reopened, rows = cycle(open_store, store_dir, TextItems, [obj])

    assert added[0] == obj and reopened[0] == obj
    assert [type(x) for x in reopened[0].v] == [str, date]
    assert rows[0]["v"] == [
        {"$type": "str", "$value": "2026-01-15"},
        {"$type": "date", "$value": "2026-01-15"},
    ]


def test_a_list_of_dates_naming_unreadable_text_is_refused_at_open(
        by_hand, open_store, store_dir):
    by_hand(TextLists, store_dir,
            [{"v": {"$type": "list[date]", "$value": ["hello"]}}])

    with pytest.raises(StoreLoadError, match="expected date, got str"):
        open_store(TextLists, store_dir, debounce=0)


# --------------------------------------------------------------------------
# 6. list[A] | list[B] where A and B share every field name
# --------------------------------------------------------------------------


def test_two_dataclasses_with_the_same_field_names_route_by_exact_type(
        open_store, store_dir):
    # Shaped(x=1, y="a") and Twinned(x="1", y="a") differ in one field type and
    # in nothing else. Routing reads the Python type of the instance, never the
    # contents, so neither branch can absorb the other.
    objs = [TwinLists(v=[]),
            TwinLists(v=[Shaped(1, "a")]),
            TwinLists(v=[Twinned("1", "a")]),
            TwinLists(v=[Shaped(1, "a"), Shaped(2, "b")])]

    added, reopened, rows = cycle(open_store, store_dir, TwinLists, objs)

    for obj, got, back in zip(objs, added, reopened):
        assert got == obj and back == obj
        assert [type(x) for x in back.v] == [type(x) for x in obj.v]
        assert p4(TwinLists, obj) == obj

    assert rows[1]["v"] == {"$type": "list[Shaped]", "$value": [{"x": 1, "y": "a"}]}
    assert rows[2]["v"] == {"$type": "list[Twinned]", "$value": [{"x": "1", "y": "a"}]}
    # One branch per list, so the items inside need no name of their own.
    assert "$type" not in rows[1]["v"]["$value"][0]


def test_a_list_mixing_both_dataclasses_is_refused_by_add(open_store, store_dir):
    # No branch holds both, so the encoder names the first and the row does not
    # validate. add() makes the round trip before it keeps anything, so the
    # refusal happens in front of the caller and nothing reaches the file.
    store = open_store(TwinLists, store_dir, debounce=0)

    with pytest.raises(SchemaTypeError, match="expected Shaped, got Twinned"):
        store.add(TwinLists(v=[Shaped(1, "a"), Twinned("1", "b")]))

    assert len(store) == 0


# --------------------------------------------------------------------------
# 7. A | B where A carries every field of B and one more
# --------------------------------------------------------------------------


def test_the_wider_dataclass_never_swallows_the_narrower_one(
        open_store, store_dir):
    # Narrow(x=1)'s payload {"x": 1} would validate as Wide too, since Wide's
    # extra field has a default. The variant is named inside the object, so the
    # reading never has to guess — in either union order.
    for cls in (WideFirst, NarrowFirst):
        objs = [cls(v=Narrow(1)), cls(v=Wide(1)), cls(v=Wide(1, "z"))]
        folder = store_dir / cls.__name__
        folder.mkdir()

        added, reopened, rows = cycle(open_store, folder, cls, objs)

        for obj, got, back in zip(objs, added, reopened):
            assert got == obj and back == obj
            assert type(got.v) is type(obj.v)
            assert type(back.v) is type(obj.v)
            assert p4(cls, obj) == obj

        assert rows[0]["v"] == {"$type": "Narrow", "x": 1}
        assert rows[1]["v"] == {"$type": "Wide", "x": 1, "extra": "d"}


def test_the_narrow_payload_named_as_the_wide_one_takes_the_wide_defaults(
        by_hand, open_store, store_dir):
    # P3, and the price of a discriminator: "$type" is authoritative, so a
    # hand-written row that names Wide gets a Wide, its missing field filled
    # from the default. The file said so; nothing was guessed.
    by_hand(WideFirst, store_dir, [{"v": {"$type": "Wide", "x": 1}}])
    store = open_store(WideFirst, store_dir, debounce=0)

    assert store.get(1) == WideFirst(v=Wide(1, "d"))
    assert type(store.get(1).v) is Wide


# --------------------------------------------------------------------------
# 8. a union that contains itself
# --------------------------------------------------------------------------


def test_a_recursive_union_names_its_variant_at_every_depth(
        open_store, store_dir):
    objs = [Recursive(v=Tip("t")),
            Recursive(v=Branch("b")),
            Recursive(v=Branch("b", Tip("t"))),
            Recursive(v=Branch("a", Branch("b", Branch("c", Tip("d")))))]

    added, reopened, rows = cycle(open_store, store_dir, Recursive, objs)

    for obj, got, back in zip(objs, added, reopened):
        assert got == obj and back == obj
        assert type(got.v) is type(obj.v)
        assert type(back.v) is type(obj.v)
        assert p4(Recursive, obj) == obj

    assert rows[2]["v"] == {"$type": "Branch", "name": "b",
                            "child": {"$type": "Tip", "name": "t"}}
    # Four levels down the discriminator is still there, and the None that ends
    # the chain is a value, not a missing key.
    deepest = rows[3]["v"]["child"]["child"]["child"]
    assert deepest == {"$type": "Tip", "name": "d"}
    assert rows[1]["v"] == {"$type": "Branch", "name": "b", "child": None}


# --------------------------------------------------------------------------
# 9. Optional[list[Optional[A | B]]]
# --------------------------------------------------------------------------


def test_every_hole_of_an_optional_list_of_an_optional_union_round_trips(
        open_store, store_dir):
    objs = [Holes(v=None),
            Holes(v=[]),
            Holes(v=[None]),
            Holes(v=[Shaped(1, "a"), None, Twinned("1", "b")]),
            Holes(v=[None, None])]

    added, reopened, rows = cycle(open_store, store_dir, Holes, objs)

    for obj, got, back in zip(objs, added, reopened):
        assert got == obj and back == obj
        assert p4(Holes, obj) == obj

    assert type(reopened[3].v[0]) is Shaped
    assert reopened[3].v[1] is None
    assert type(reopened[3].v[2]) is Twinned

    # None is alone on its wire type at both depths, so it is written bare and
    # never confused with an absent key.
    assert rows[0]["v"] is None
    assert rows[2]["v"] == [None]
    assert rows[3]["v"] == [{"$type": "Shaped", "x": 1, "y": "a"}, None,
                            {"$type": "Twinned", "x": "1", "y": "b"}]


def test_a_list_item_without_its_variant_name_is_refused_at_open(
        by_hand, open_store, store_dir):
    by_hand(Holes, store_dir, [{"v": [{"x": 1, "y": "a"}]}])

    with pytest.raises(StoreLoadError, match="ambiguous dict"):
        open_store(Holes, store_dir, debounce=0)


# --------------------------------------------------------------------------
# 10. two options that share one public identity
# --------------------------------------------------------------------------


def test_two_dataclasses_sharing_a_name_are_refused_when_the_schema_compiles():
    # The rejection is loud and happens before any store exists: two Structs
    # named alike would compile to one "$type" and the file could not name them
    # apart.
    first = _dataclass_named("Same", int)
    second = _dataclass_named("Same", str)

    @dataclass
    class Clash:
        v: first | second

    with pytest.raises(ValueError, match="duplicate discriminator name"):
        struct_of(Clash)


def test_two_same_named_dataclasses_in_two_list_options_are_refused_too():
    first = _dataclass_named("Same", int)
    second = _dataclass_named("Same", str)

    @dataclass
    class ClashInLists:
        v: list[first] | list[second]

    # list[Same] and list[Same] are one option written twice.
    with pytest.raises(ValueError, match="duplicate option types"):
        struct_of(ClashInLists)


def test_a_same_named_dataclass_in_a_list_and_alone_is_admissible(
        open_store, store_dir):
    # list[Same] and Same never compete for a "$type": one arrives as a list,
    # the other as an object. The schema compiles and the store routes both.
    first = _dataclass_named("Same", int)
    second = _dataclass_named("Same", str)

    @dataclass
    class Mixed:
        v: list[first] | second

    objs = [Mixed(v=[first(1)]), Mixed(v=second("a"))]
    added, reopened, rows = cycle(open_store, store_dir, Mixed, objs)

    assert added == objs and reopened == objs
    assert type(reopened[0].v[0]) is first
    assert type(reopened[1].v) is second
    assert [row["v"] for row in rows] == [[{"x": 1}], {"x": "a"}]


def test_an_enum_named_str_and_a_str_option_compile_to_one_wire_identity():
    # Not an assertion about the store yet: the ground the next two tests stand
    # on. EnumShape.option_id() is the bare class name, so this enum and the Str
    # beside it both answer "str", and the core lets the schema through — its
    # duplicate check keys on the Python type, and its discriminator check
    # guards Struct against Struct and Enum against Enum, never Enum against a
    # scalar.
    schema = struct_of(StrThenEnum)
    ids = [s.option_id() for s in schema.fields[0].shape]

    assert ids == ["str", "str"]
    assert ShadowStr.__name__ == "str" and ShadowStr is not str


def test_an_enum_sharing_a_transport_name_with_a_scalar_is_refused_at_open(
        open_store, store_dir, file_of):
    """The collision cannot lose a row, because there is no store to put one in.

    An enum class named `str` answers to the same wrapper name as the `Str`
    beside it, so the file could not say which option a row belongs to. This
    used to store a member and hand back a plain string; now the schema is
    refused at the door.
    """
    with pytest.raises(StoreError) as refusal:
        open_store(StrThenEnum, store_dir)

    assert str(refusal.value) == (
        f"{file_of(StrThenEnum, store_dir)}: v: two options of a union share "
        f"the transport name 'str': rename one of the classes")


@pytest.mark.parametrize("cls", [EnumThenDate, DateThenEnum],
                         ids=["the enum written first", "the date written first"])
def test_a_date_beside_an_enum_named_date_is_refused_either_way_round(
        open_store, store_dir, cls):
    """Both directions of one collision: a stored date came back as a member,
    and a stored member came back as a date, depending only on which option the
    union declared first."""
    with pytest.raises(StoreError, match="share the transport name 'date'"):
        open_store(cls, store_dir)


def test_a_refused_schema_leaves_nothing_behind(open_store, store_dir, file_of,
                                                lock_of):
    """The guard runs before the lock is taken, so a refusal costs nothing."""
    with pytest.raises(StoreError):
        open_store(StrThenEnum, store_dir)

    assert not lock_of(file_of(StrThenEnum, store_dir)).exists()
    assert list(store_dir.iterdir()) == []


def test_an_enum_named_like_a_scalar_it_never_meets_is_fine(open_store,
                                                            store_dir):
    """The guard looks for collisions, not for forbidden names.

    The same enum class named `str`, in a field with no `str` option beside it,
    names nothing twice and stores its members like any other enum.
    """
    @dataclass
    class Alone:
        v: ShadowStr

    store = open_store(Alone, store_dir, debounce=0)
    store.add(Alone(v=ShadowStr.LOW))

    assert store.get(1).v is ShadowStr.LOW


# --------------------------------------------------------------------------
# 11. int | float
# --------------------------------------------------------------------------


def test_int_and_float_keep_their_exact_python_type_through_the_store(
        open_store, store_dir):
    # 0 == 0.0 and 0.0 == -0.0, so every check here is on the type, not only on
    # the value. -0 is written -0 but Python has no negative zero int: it is 0.
    values = [0, -0, 0.0, -0.0, 1, 1.0, 10 ** 15, 1e15,
              float(2 ** 53), 2 ** 53 + 1, 2.0]
    objs = [Numbers(v=v) for v in values]

    added, reopened, rows = cycle(open_store, store_dir, Numbers, objs)

    for obj, got, back in zip(objs, added, reopened):
        assert got == obj and back == obj                       # P1, P2
        assert type(got.v) is type(obj.v)
        assert type(back.v) is type(obj.v)
        result = p4(Numbers, obj)                               # P4
        assert result == obj and type(result.v) is type(obj.v)

    # The sign of a negative zero is data too, and json keeps it.
    minus_zero = reopened[3].v
    assert type(minus_zero) is float and math.copysign(1.0, minus_zero) == -1.0
    assert rows[3]["v"] == -0.0 and math.copysign(1.0, rows[3]["v"]) == -1.0
    # -0 is the int 0 and travels as one; the store invents no float for it.
    assert type(reopened[1].v) is int and rows[1]["v"] == 0

    # An int option beside the float one means no wire collision at all: the
    # numbers travel bare, and json's own spelling is the discriminator.
    assert rows[0]["v"] == 0 and rows[2]["v"] == 0.0
    assert "$type" not in json.dumps(rows[5])


def test_a_hand_written_whole_number_reads_as_int_when_int_is_an_option(
        by_hand, open_store, store_dir):
    # P3, and the price of writing numbers bare: 3 and 3.0 are different text
    # and the file is the whole evidence. With Int among the options, decode
    # coerces nothing and 3 is an int.
    by_hand(Numbers, store_dir, [{"v": 3}, {"v": 3.0}])
    store = open_store(Numbers, store_dir, debounce=0)

    assert type(store.get(1).v) is int
    assert type(store.get(2).v) is float


def test_list_of_int_and_list_of_float_are_settled_by_their_items(
        open_store, store_dir):
    # The same pair one level down, where they do collide on the wire and the
    # wrapper comes back.
    objs = [NumberLists(v=[1]), NumberLists(v=[1.0]), NumberLists(v=[0.0])]

    added, reopened, rows = cycle(open_store, store_dir, NumberLists, objs)

    for obj, got, back in zip(objs, added, reopened):
        assert got == obj and back == obj
        assert [type(x) for x in back.v] == [type(x) for x in obj.v]
        assert p4(NumberLists, obj) == obj

    assert rows[0]["v"] == {"$type": "list[int]", "$value": [1]}
    assert rows[1]["v"] == {"$type": "list[float]", "$value": [1.0]}


def test_a_list_mixing_int_and_float_is_refused_by_add(open_store, store_dir):
    store = open_store(NumberLists, store_dir, debounce=0)

    with pytest.raises(SchemaTypeError, match="expected int, got float"):
        store.add(NumberLists(v=[1, 2.0]))

    assert len(store) == 0
