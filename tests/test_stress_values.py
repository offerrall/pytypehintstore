"""Atomic values at their corners, and what the file does with them.

One dataclass per shape, taken to the edge of what the core compiles and
accepts: the non-finite floats first, then the strings, the integers, the
calendar, the clock, the enums, the lists, and the field names that spell the
payload's own keys.

Three questions are asked of each value, and the third is the one that matters:

  P1  what `add` took, `get` gives back.
  P4  `build(decode(json.loads(json.dumps(encode(schema, obj))))) == obj`.
  P2  a real file, closed and reopened, still holds it.

P4 is the transport in miniature and costs nothing, so everything gets it. P2
costs a thread and a disk, so it goes to whatever P4 leaves in doubt — and to
everything the file is asked to spell in a way a human would read.

Equality is not asked to carry more than it can: `1 == 1.0` and `True == 1`, so
where the type is the claim the test asserts `type(x) is T`; `nan != nan`, so
that value is settled with `math.isnan` and never with `==`.
"""

import json
import math
from dataclasses import dataclass
from datetime import date, time
from enum import Enum, Flag, IntEnum, StrEnum, auto

import pytest
from pytypehint import SchemaTypeError, SchemaValueError, struct_of

from pytypehintstore import StoreLoadError, store_of
from pytypehintstore.codec import encode


# ---- the shapes ------------------------------------------------------------

@dataclass
class Measure:
    value: float


@dataclass
class Text:
    text: str


@dataclass
class Count:
    n: int


@dataclass
class Flag8:
    b: bool


@dataclass
class Day:
    d: date


@dataclass
class Clock:
    t: time


@dataclass
class Either:
    v: bool | int


@dataclass
class Items:
    xs: list[bool | int]


@dataclass
class Flat:
    xs: list[int]


@dataclass
class Grid:
    cells: list[list[list[list[int]]]]


@dataclass
class Six:
    a: int
    b: str
    c: float
    d: bool
    e: date
    f: str


@dataclass
class Sheet:
    rows: list[Six]


class Sentinel(Enum):
    MISSING = "missing"
    OTHER = "other"


# A member name is not an identifier the way a field name is, so the two keys
# the transport reserves are perfectly legal here. Built through the functional
# API because `$type = 1` is not syntax.
Reserved = Enum("Reserved", {"$type": 1, "$value": 2, "plain": 3})


class Nums(IntEnum):
    ONE = 1
    TWO = 2


class Words(StrEnum):
    A = "a"
    B = "b"


class Perm(Flag):
    READ = auto()
    WRITE = auto()


class Colour(Enum):
    RED = "red"
    BLUE = "blue"


@dataclass
class Marked:
    e: Sentinel


@dataclass
class Odd:
    e: Reserved


@dataclass
class Numbered:
    e: Nums


@dataclass
class Worded:
    e: Words


@dataclass
class Permitted:
    e: Perm


@dataclass
class Named:
    v: str | Colour


# The three keys of the store's own payload, as the field names of a row.
@dataclass
class Payloadish:
    v: int
    next_id: int
    rows: str


# ---- the round trip in miniature -------------------------------------------

def trip(cls, obj):
    """P4: through encode, JSON text, decode and back through the schema."""
    schema = struct_of(cls)
    return schema.build(schema.decode(json.loads(json.dumps(encode(schema, obj)))))


def strict(text):
    """Parse as a reader with no extensions would: Infinity and NaN are errors."""
    def refuse(constant):
        raise AssertionError(f"non-standard JSON constant in the file: {constant!r}")

    return json.loads(text, parse_constant=refuse)


NON_FINITE = (float("inf"), float("-inf"), float("nan"))


# ---- the non-finite floats -------------------------------------------------
#
# json.dumps writes them as Infinity, -Infinity and NaN, which no JSON standard
# defines; Python's own json.loads reads them back, so a file carrying them
# would open here and nowhere else. The core settles it before the question is
# asked: Float refuses anything math.isfinite refuses, at every gate.

@pytest.mark.parametrize("value", NON_FINITE, ids=["inf", "-inf", "nan"])
def test_a_non_finite_float_is_refused_by_add(store_dir, open_store, value):
    store = open_store(Measure, store_dir, debounce=0.0)

    with pytest.raises(SchemaValueError) as caught:
        store.add(Measure(value=value))

    assert "not finite" in str(caught.value)
    assert len(store) == 0


@pytest.mark.parametrize("value", NON_FINITE, ids=["inf", "-inf", "nan"])
def test_a_refused_non_finite_spends_no_id(store_dir, open_store, value):
    """The row is judged before an id is taken, so the next add still gets 1."""
    store = open_store(Measure, store_dir, debounce=0.0)

    with pytest.raises(SchemaValueError):
        store.add(Measure(value=value))

    assert store.add(Measure(value=1.5)) == 1


@pytest.mark.parametrize("value", NON_FINITE, ids=["inf", "-inf", "nan"])
def test_a_non_finite_float_fails_the_round_trip(value):
    with pytest.raises(SchemaValueError) as caught:
        trip(Measure, Measure(value=value))

    assert "not finite" in str(caught.value)


@pytest.mark.parametrize("value", NON_FINITE, ids=["inf", "-inf", "nan"])
def test_encode_alone_is_not_the_gate(value):
    """encode passes a non-finite float through — nothing there validates.

    Written down because it names where the guarantee lives: not in the codec,
    which would hand json.dumps a token no standard reader accepts, but in the
    build every add and every load makes.
    """
    written = encode(struct_of(Measure), Measure(value=value))
    assert not math.isfinite(written["value"])

    text = json.dumps(written)
    assert any(token in text for token in ("Infinity", "-Infinity", "NaN"))
    assert json.loads(text)["value"] is not None  # Python reads it back

    with pytest.raises(AssertionError, match="non-standard JSON constant"):
        strict(text)


def test_a_non_finite_default_never_compiles():
    @dataclass
    class Broken:
        x: float = float("inf")

    with pytest.raises(SchemaValueError) as caught:
        struct_of(Broken)

    assert "not finite" in str(caught.value)


@pytest.mark.parametrize("value", NON_FINITE, ids=["inf", "-inf", "nan"])
def test_a_hand_written_non_finite_is_refused_loudly(store_dir, by_hand, file_of,
                                                     value):
    """The one door left: a file edited by hand, or written by Python's json.

    json.dumps put the bare token in the file and json.loads read it back, so
    the store meets a genuine float('inf') at load. It refuses the whole file
    rather than open on a row it could not have written.
    """
    by_hand(Measure, store_dir, [{"value": value}])
    path = file_of(Measure, store_dir)

    assert any(token in path.read_text(encoding="utf-8")
               for token in ("Infinity", "NaN"))

    with pytest.raises(StoreLoadError) as caught:
        store_of(Measure, str(store_dir))

    assert "not finite" in str(caught.value)


def test_the_file_never_carries_a_json_extension_constant(store_dir, open_store,
                                                          payload_of):
    """P2 and (d) together: what the store writes, a strict reader reads.

    Finite extremes and a string that spells the tokens out. `Infinity` and
    `NaN` appear in the file only inside quotes, where they are five and three
    characters of a str and nothing else.
    """
    store = open_store(Measure, store_dir, debounce=0.0)
    for value in (0.0, -0.0, 1e308, 5e-324, 1.7976931348623157e308):
        store.add(Measure(value=value))
    store.close()

    text = store.path.read_text(encoding="utf-8")
    assert strict(text) == payload_of(store.path)

    reopened = store_of(Measure, str(store_dir))
    try:
        assert [row.value for _, row in reopened.all()] == [
            0.0, -0.0, 1e308, 5e-324, 1.7976931348623157e308]
    finally:
        reopened.close()


def test_nan_inequality_is_python_and_not_the_store():
    """`nan != nan`, so P1 could never be asserted with `==` for it.

    The distinction the suite has to keep: a NaN that came back unequal would
    say nothing about the store. It is moot here — the core refuses NaN, so no
    store ever holds one — and the check that would have been used is written
    down beside the proof of why it was needed.
    """
    nan = float("nan")
    assert nan != nan
    assert math.isnan(nan)
    assert [nan] == [nan]          # by identity, not by value

    with pytest.raises(SchemaValueError):
        struct_of(Measure).build({"value": nan})


# ---- floats that are finite ------------------------------------------------

@pytest.mark.parametrize("value", [0.0, -0.0, 1e308, 5e-324,
                                   1.7976931348623157e308, 2.5, 1.0])
def test_a_finite_float_survives_the_round_trip(value):
    got = trip(Measure, Measure(value=value))
    assert type(got.value) is float
    assert got.value == value
    assert repr(got.value) == repr(value)


def test_the_sign_of_negative_zero_survives_the_file(store_dir, open_store,
                                                     rows_of):
    """-0.0 == 0.0, so equality cannot see the bit that matters. copysign can."""
    store = open_store(Measure, store_dir, debounce=0.0)
    row = store.add(Measure(value=-0.0))
    store.close()

    assert rows_of(store.path)[str(row)] == {"value": -0.0}
    assert '"value": -0.0' in store.path.read_text(encoding="utf-8")

    reopened = store_of(Measure, str(store_dir))
    try:
        assert math.copysign(1.0, reopened.get(row).value) == -1.0
    finally:
        reopened.close()


# ---- strings ---------------------------------------------------------------

STRINGS = {
    "empty": "",
    "spaces": "  ",
    "newline": "\n",
    "tab": "\t",
    "nul": "\0",
    "crlf": "a\r\nb",
    "emoji": "\U0001f600\U0001f469‍\U0001f4bb",
    "combining": "é́",
    "rtl": "العربية ‮RTL‬",
    "type_key": "$type",
    "value_key": "$value",
    "nan_word": "nan",
    "NaN_word": "NaN",
    "Infinity_word": "Infinity",
    "null_word": "null",
    "true_word": "true",
    "minus_five": "-5",
    "leading_zero": "01",
    "iso_date": "2024-02-29",
    "iso_time": "23:59:59",
    "escape_text": "\\u0041",
    "quote": '"',
    "brace": "{}",
}


@pytest.mark.parametrize("value", list(STRINGS.values()), ids=list(STRINGS))
def test_a_string_corner_survives_the_round_trip(value):
    got = trip(Text, Text(text=value))
    assert type(got.text) is str
    assert got.text == value


def test_every_string_corner_survives_the_file(store_dir, open_store):
    """P2 for all of them at once: one file, closed, reopened, compared."""
    store = open_store(Text, store_dir, debounce=0.0)
    ids = {name: store.add(Text(text=value)) for name, value in STRINGS.items()}
    store.close()

    text = store.path.read_text(encoding="utf-8")
    # A NUL is not writable as itself and does not have to be: JSON escapes it.
    assert '"text": "\\u0000"' in text
    # ensure_ascii=False, so what a human opens is the character, not \uXXXX.
    assert "\U0001f600" in text
    assert strict(text) is not None

    reopened = store_of(Text, str(store_dir))
    try:
        for name, row in ids.items():
            got = reopened.get(row).text
            assert type(got) is str
            assert got == STRINGS[name], name
    finally:
        reopened.close()


def test_the_reserved_keys_as_a_string_value_are_not_read_as_a_wrapper(
        store_dir, open_store, rows_of):
    """"$type" and "$value" are keys of the transport, never values of one.

    A str field holding them lands as a plain JSON string; the decoder looks for
    them as keys of a dict and never at the text inside a string.
    """
    store = open_store(Text, store_dir, debounce=0.0)
    a = store.add(Text(text="$type"))
    b = store.add(Text(text="$value"))
    store.close()

    rows = rows_of(store.path)
    assert rows[str(a)] == {"text": "$type"}
    assert rows[str(b)] == {"text": "$value"}

    reopened = store_of(Text, str(store_dir))
    try:
        assert reopened.get(a) == Text(text="$type")
        assert reopened.get(b) == Text(text="$value")
    finally:
        reopened.close()


@pytest.mark.parametrize("value", ["2024-02-29", "0001-01-01", "23:59:59",
                                    "00:00:00", "9999-12-31"])
def test_text_that_reads_as_a_date_stays_text(value):
    """A Str option in the field is the reading; decode converts nothing else."""
    got = trip(Text, Text(text=value))
    assert type(got.text) is str
    assert got.text == value


def test_a_megabyte_of_text_survives_the_file(store_dir, open_store):
    million = "é" * (1024 * 1024)

    assert trip(Text, Text(text=million)).text == million

    store = open_store(Text, store_dir, debounce=0.0)
    row = store.add(Text(text=million))
    store.close()

    reopened = store_of(Text, str(store_dir))
    try:
        assert reopened.get(row).text == million
    finally:
        reopened.close()


# ---- the lone surrogate ----------------------------------------------------
#
# "\ud800" is a legal Python str and an illegal UTF-8 character. Str validates
# type and length and has nothing to say about it, so the round trip inside add
# passes and the row is kept — but the file is opened with encoding="utf-8" and
# the dump can never land. These tests do not use `open_store`: the store they
# leave behind raises from close(), and every later close() replays it.

SURROGATE = "\ud800"


def abandon(store):
    """Let a store go however it can.

    A store whose dump cannot be encoded raises from close() and raises the same
    thing from every close() after it, so the teardown that would normally take
    it — `open_store` — cannot. Closing is still what releases the lockfile and
    unregisters the atexit hook, so it is called anyway and its failure dropped.
    """
    try:
        store.close()
    except BaseException:
        pass


def test_a_lone_surrogate_is_refused_by_add(store_dir):
    """A str Python allows and no encoder can write is refused at the door.

    The core validates it as a str — it is one — and the codec is happy in
    memory, so nothing before the file objects. The store asks the last
    question itself, because the file is its own: a row it could never dump
    would freeze every later dump, and the failure would surface at close() as
    an error from another library.
    """
    store = store_of(Text, str(store_dir), debounce=0.0)

    try:
        with pytest.raises(SchemaValueError) as refusal:
            store.add(Text(text=SURROGATE))

        assert str(refusal.value).startswith("cannot be written as UTF-8: ")
        assert "surrogates not allowed" in str(refusal.value)
    finally:
        abandon(store)


def test_a_refused_surrogate_spends_no_id_and_leaves_the_store_working(
        store_dir, until):
    """The refusal is a refusal, not damage: the id is not spent, the rows
    around it reach the file, and close() is ordinary."""
    store = store_of(Text, str(store_dir), debounce=0.0)

    try:
        first = store.add(Text(text="fine"))

        with pytest.raises(SchemaValueError):
            store.add(Text(text=SURROGATE))

        later = store.add(Text(text="also fine"))
        store.close()
    finally:
        abandon(store)

    assert (first, later) == (1, 2)

    reopened = store_of(Text, str(store_dir))
    try:
        assert [text for _, text in reopened.all()] == [
            Text(text="fine"), Text(text="also fine")]
    finally:
        reopened.close()


def test_a_value_add_accepted_reaches_the_file(store_dir):
    """The promise the refusal above buys: what add keeps survives the file.

    The round trip add makes is encode/decode/build, and none of the three
    touches UTF-8 — so the store asks that question separately. This is the
    test that would have caught the surrogate, and it is what keeps the
    README's claim honest for every str the core accepts.
    """
    store = store_of(Text, str(store_dir), debounce=0.0)
    try:
        row = store.add(Text(text="every str the core takes, the file takes"))
        store.close()
    finally:
        abandon(store)

    reopened = store_of(Text, str(store_dir))
    try:
        assert reopened.get(row).text == "every str the core takes, the file takes"
    finally:
        reopened.close()



# ---- integers --------------------------------------------------------------

INTS = [0, -1, 1, 2**53 - 1, 2**53, 2**63, 2**63 - 1, 2**200, -(2**200)]


@pytest.mark.parametrize("value", INTS, ids=lambda v: str(v)[:24])
def test_an_int_corner_survives_the_round_trip(value):
    got = trip(Count, Count(n=value))
    assert type(got.n) is int
    assert got.n == value


@pytest.mark.parametrize("value", [2**200, -(2**200), 2**63, 2**53 + 1],
                         ids=lambda v: str(v)[:24])
def test_a_big_int_reaches_the_file_digit_for_digit(store_dir, open_store,
                                                    rows_of, value):
    """No float ever stands in for it: the digits are in the text as written."""
    store = open_store(Count, store_dir, debounce=0.0)
    row = store.add(Count(n=value))
    store.close()

    text = store.path.read_text(encoding="utf-8")
    assert str(value) in text
    assert "e+" not in text and "." not in text.split('"n":')[1].split("\n")[0]
    assert rows_of(store.path)[str(row)]["n"] == value

    reopened = store_of(Count, str(store_dir))
    try:
        assert reopened.get(row).n == value
        assert type(reopened.get(row).n) is int
    finally:
        reopened.close()


# ---- bool against int ------------------------------------------------------

def test_a_bool_is_not_an_int_field(store_dir, open_store):
    store = open_store(Count, store_dir, debounce=0.0)

    with pytest.raises(SchemaTypeError, match="expected int, got bool"):
        store.add(Count(n=True))


def test_an_int_is_not_a_bool_field(store_dir, open_store):
    store = open_store(Flag8, store_dir, debounce=0.0)

    with pytest.raises(SchemaTypeError, match="expected bool, got int"):
        store.add(Flag8(b=1))


@pytest.mark.parametrize("value", [True, False, 0, 1, 2, -1],
                         ids=["True", "False", "0", "1", "2", "-1"])
def test_a_union_of_bool_and_int_keeps_each_one_itself(value):
    """`bool` subclasses `int` and `True == 1`, so only `type is` can tell."""
    got = trip(Either, Either(v=value))
    assert type(got.v) is type(value)
    assert got.v == value


def test_the_bool_int_union_is_written_as_json_says_it(store_dir, open_store,
                                                        rows_of):
    store = open_store(Either, store_dir, debounce=0.0)
    yes = store.add(Either(v=True))
    one = store.add(Either(v=1))
    zero = store.add(Either(v=0))
    no = store.add(Either(v=False))
    store.close()

    text = store.path.read_text(encoding="utf-8")
    assert '"v": true' in text
    assert '"v": 1' in text
    rows = rows_of(store.path)
    assert type(rows[str(yes)]["v"]) is bool
    assert type(rows[str(one)]["v"]) is int

    reopened = store_of(Either, str(store_dir))
    try:
        assert type(reopened.get(yes).v) is bool and reopened.get(yes).v is True
        assert type(reopened.get(one).v) is int and reopened.get(one).v == 1
        assert type(reopened.get(zero).v) is int and reopened.get(zero).v == 0
        assert type(reopened.get(no).v) is bool and reopened.get(no).v is False
    finally:
        reopened.close()


def test_a_list_of_bool_or_int_routes_every_item_on_its_own(store_dir,
                                                            open_store):
    source = [True, 1, False, 0, 2, True, -1]

    got = trip(Items, Items(xs=source))
    assert [type(x) for x in got.xs] == [type(x) for x in source]

    store = open_store(Items, store_dir, debounce=0.0)
    row = store.add(Items(xs=source))
    store.close()

    reopened = store_of(Items, str(store_dir))
    try:
        back = reopened.get(row).xs
        assert [type(x) for x in back] == [type(x) for x in source]
        assert back == source
    finally:
        reopened.close()


# ---- the calendar ----------------------------------------------------------

DATES = [date.min, date.max, date(2024, 2, 29), date(2000, 2, 29),
         date(1, 1, 1), date(9999, 12, 31), date(1970, 1, 1)]


@pytest.mark.parametrize("value", DATES, ids=lambda v: v.isoformat())
def test_a_date_corner_survives_the_round_trip(value):
    got = trip(Day, Day(d=value))
    assert type(got.d) is date
    assert got.d == value


def test_the_calendar_edges_reach_the_file_as_iso_text(store_dir, open_store,
                                                       rows_of):
    store = open_store(Day, store_dir, debounce=0.0)
    ids = [store.add(Day(d=value)) for value in DATES]
    store.close()

    rows = rows_of(store.path)
    assert rows[str(ids[0])] == {"d": "0001-01-01"}
    assert rows[str(ids[1])] == {"d": "9999-12-31"}
    assert rows[str(ids[2])] == {"d": "2024-02-29"}

    reopened = store_of(Day, str(store_dir))
    try:
        for row, value in zip(ids, DATES):
            assert reopened.get(row).d == value
    finally:
        reopened.close()


def test_a_date_that_is_not_a_date_is_refused_on_load(store_dir, by_hand):
    """29 February of a year that has none: ISO text the calendar rejects."""
    by_hand(Day, store_dir, [{"d": "2023-02-29"}])

    with pytest.raises(StoreLoadError):
        store_of(Day, str(store_dir))


# ---- the clock -------------------------------------------------------------

TIMES = [time.min, time(0, 0, 0), time(12, 30), time(23, 59, 59), time(9, 5, 1)]


@pytest.mark.parametrize("value", TIMES, ids=lambda v: v.isoformat())
def test_a_time_corner_survives_the_round_trip(value):
    got = trip(Clock, Clock(t=value))
    assert type(got.t) is time
    assert got.t == value


def test_the_clock_edges_reach_the_file_as_iso_text(store_dir, open_store,
                                                    rows_of):
    """isoformat() writes whole seconds even where the value omitted them."""
    store = open_store(Clock, store_dir, debounce=0.0)
    ids = [store.add(Clock(t=value)) for value in TIMES]
    store.close()

    rows = rows_of(store.path)
    assert rows[str(ids[0])] == {"t": "00:00:00"}
    assert rows[str(ids[2])] == {"t": "12:30:00"}
    assert rows[str(ids[3])] == {"t": "23:59:59"}

    reopened = store_of(Clock, str(store_dir))
    try:
        for row, value in zip(ids, TIMES):
            assert reopened.get(row).t == value
    finally:
        reopened.close()


@pytest.mark.parametrize("value", [time.max, time(12, 30, 45, 123456),
                                    time(0, 0, 0, 1)],
                         ids=["time.max", "microseconds", "one-microsecond"])
def test_a_sub_second_time_is_refused(store_dir, open_store, value):
    """The core's precision is whole seconds, so time.max is not a valid time.

    isoformat() would have carried the microseconds and fromisoformat() would
    have read them back — the transport is wider than the type here. The refusal
    is the core's, before the file is ever asked.
    """
    store = open_store(Clock, store_dir, debounce=0.0)

    with pytest.raises(SchemaValueError, match="whole seconds"):
        store.add(Clock(t=value))

    with pytest.raises(SchemaValueError, match="whole seconds"):
        trip(Clock, Clock(t=value))


def test_a_sub_second_time_in_the_file_is_refused_on_load(store_dir, by_hand):
    by_hand(Clock, store_dir, [{"t": "23:59:59.999999"}])

    with pytest.raises(StoreLoadError) as caught:
        store_of(Clock, str(store_dir))

    assert "whole seconds" in str(caught.value)


# ---- enums -----------------------------------------------------------------

def test_a_member_named_missing_is_a_member_like_any_other(store_dir,
                                                           open_store, rows_of):
    """The core's own MISSING sentinel is a different object entirely."""
    assert trip(Marked, Marked(e=Sentinel.MISSING)).e is Sentinel.MISSING

    store = open_store(Marked, store_dir, debounce=0.0)
    row = store.add(Marked(e=Sentinel.MISSING))
    store.close()

    assert rows_of(store.path)[str(row)] == {"e": "MISSING"}

    reopened = store_of(Marked, str(store_dir))
    try:
        assert reopened.get(row).e is Sentinel.MISSING
    finally:
        reopened.close()


@pytest.mark.parametrize("name", ["$type", "$value", "plain"])
def test_a_member_named_like_a_reserved_key_travels_whole(store_dir, open_store,
                                                          rows_of, name):
    """A field name must be an identifier; a member name need not be.

    So `$type` is impossible as a field and perfectly possible as an enum member
    — and it travels as the *value* of a key, never as the key, which is why it
    cannot be mistaken for the wrapper it is named after.
    """
    member = Reserved[name]

    assert trip(Odd, Odd(e=member)).e is member

    store = open_store(Odd, store_dir, debounce=0.0)
    row = store.add(Odd(e=member))
    store.close()

    assert rows_of(store.path)[str(row)] == {"e": name}

    reopened = store_of(Odd, str(store_dir))
    try:
        assert reopened.get(row).e is member
    finally:
        reopened.close()


@pytest.mark.parametrize("cls,member,name", [
    (Numbered, Nums.TWO, "TWO"),
    (Worded, Words.B, "B"),
], ids=["IntEnum", "StrEnum"])
def test_a_mixin_enum_travels_by_name_and_not_by_value(store_dir, open_store,
                                                       rows_of, cls, member,
                                                       name):
    """`Nums.TWO == 2` and `Words.B == "b"`, so `is` is the assertion."""
    assert trip(cls, cls(e=member)).e is member

    store = open_store(cls, store_dir, debounce=0.0)
    row = store.add(cls(e=member))
    store.close()

    assert rows_of(store.path)[str(row)] == {"e": name}

    reopened = store_of(cls, str(store_dir))
    try:
        got = reopened.get(row).e
        assert got is member
        assert type(got) is type(member)
    finally:
        reopened.close()


def test_a_flag_enum_is_refused_at_compile_time():
    """OR-combinable, so not a closed set: the store never gets to see one."""
    with pytest.raises(TypeError, match="Flag enums are not supported"):
        struct_of(Permitted)


def test_a_str_that_spells_a_member_name_stays_a_str(store_dir, open_store,
                                                     rows_of):
    """str and an enum share the wire type, so both options get named.

    "RED" as a str and Colour.RED are the same five characters on the wire; the
    wrapper is what keeps them apart, in both directions.
    """
    store = open_store(Named, store_dir, debounce=0.0)
    text = store.add(Named(v="RED"))
    member = store.add(Named(v=Colour.RED))
    store.close()

    rows = rows_of(store.path)
    assert rows[str(text)] == {"v": {"$type": "str", "$value": "RED"}}
    assert rows[str(member)] == {"v": {"$type": "Colour", "$value": "RED"}}

    reopened = store_of(Named, str(store_dir))
    try:
        assert type(reopened.get(text).v) is str
        assert reopened.get(member).v is Colour.RED
    finally:
        reopened.close()


# ---- lists at size ---------------------------------------------------------

def test_ten_thousand_flat_items(store_dir, open_store):
    source = list(range(10_000))

    assert trip(Flat, Flat(xs=source)).xs == source

    store = open_store(Flat, store_dir, debounce=0.0)
    row = store.add(Flat(xs=source))
    store.close()

    reopened = store_of(Flat, str(store_dir))
    try:
        assert reopened.get(row).xs == source
    finally:
        reopened.close()


def test_four_levels_of_nested_lists(store_dir, open_store, rows_of):
    source = [[[[i * j * k * m for m in range(4)] for k in range(4)]
               for j in range(4)] for i in range(4)]

    assert trip(Grid, Grid(cells=source)).cells == source

    store = open_store(Grid, store_dir, debounce=0.0)
    row = store.add(Grid(cells=source))
    store.close()

    assert rows_of(store.path)[str(row)] == {"cells": source}

    reopened = store_of(Grid, str(store_dir))
    try:
        assert reopened.get(row).cells == source
    finally:
        reopened.close()


def test_a_hundred_dataclasses_of_six_fields(store_dir, open_store):
    source = [Six(a=i, b=f"row {i}", c=i / 7, d=bool(i % 2),
                  e=date(2020, 1, 1) if i % 2 else date.min, f="é" * 10)
              for i in range(100)]

    assert trip(Sheet, Sheet(rows=source)).rows == source

    store = open_store(Sheet, store_dir, debounce=0.0)
    row = store.add(Sheet(rows=source))
    store.close()

    reopened = store_of(Sheet, str(store_dir))
    try:
        got = reopened.get(row).rows
        assert got == source
        assert [type(x.d) for x in got] == [bool] * 100
    finally:
        reopened.close()


# ---- field names that spell the payload's own keys -------------------------

def test_field_names_that_collide_with_the_payload_keys(store_dir, open_store,
                                                        payload_of):
    """`v`, `next_id` and `rows` live one level below the keys they share.

    The payload's keys are the file's; a row's keys are the class's, and the two
    never meet because a row is always the value of an id inside "rows". Read
    the file raw and both levels are there, undisturbed.
    """
    store = open_store(Payloadish, store_dir, debounce=0.0)
    row = store.add(Payloadish(v=7, next_id=99, rows="hello"))
    store.close()

    payload = payload_of(store.path)
    assert payload["v"] == 1
    assert payload["next_id"] == 2
    assert payload["rows"] == {str(row): {"v": 7, "next_id": 99, "rows": "hello"}}

    reopened = store_of(Payloadish, str(store_dir))
    try:
        assert reopened.get(row) == Payloadish(v=7, next_id=99, rows="hello")
        assert reopened.add(Payloadish(v=0, next_id=0, rows="")) == 2
    finally:
        reopened.close()
