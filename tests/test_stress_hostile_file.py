"""What a hand, an editor or a crashed process can leave in the file, and what
the store does with it at the next `store_of`.

The file is the store's only durable state and the one thing anybody else can
touch. Between a `close()` and the next open, a person edits it, an editor saves
it in another encoding, a crash leaves a `.part` beside it, a copy of another
class's database lands on its name. The invariant every test here defends is the
same one:

    a hostile file either loads into the rows it describes, or the open raises
    StoreLoadError. It never opens and serves a row the file did not carry.

So every case is asserted on its exact words: the store's own messages by
equality with the text `store.py` produces, and the core's by equality with what
`struct_of(cls).build(...)` says live — copied from nobody, taken in person.

Two places where the store is measurably more permissive than the core it defers
to are marked DIVERGENCE and documented rather than fixed; they are the only
cases in this module where the row served is not literally the row on disk. Two
more are the normalising reach of `date.fromisoformat`, and the last key of a
duplicated JSON key winning inside `json.loads` before the store ever sees it —
a limit with no defence short of a parser of one's own.

What is not here: the transport of each type (test_codec), the load errors of a
well-formed file (test_persistence), the lockfile as a mechanism (test_lockfile).
"""

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import date
from enum import Enum

import pytest

from pytypehint import struct_of
from pytypehintstore import StoreLoadError, StoreLockedError
from pytypehintstore.codec import decode
from shared import Bag

ROWS = 10_000
BROKEN_ROW = 9_999


# ---- shapes -----------------------------------------------------------------


class Colour(Enum):
    LOW = "low"
    HIGH = "high"


class Size(Enum):
    """Another enum of the same class's world, to borrow a member name from."""

    SMALL = "small"
    BIG = "big"


@dataclass
class Cell:
    """One row with a field of every type a hand edit can cross."""

    n: int
    flag: bool
    day: date
    colour: Colour


@dataclass
class Wrapped:
    """Two options that collide on the wire AND in Python, so the core itself
    routes on `$type` and the wrapper survives decode."""

    values: list[str] | list[int]


@dataclass
class Widened:
    """Two options that collide on the wire alone: the wrapper is decode's to
    read and to consume, and the core never sees it."""

    when: str | date


@dataclass
class Counter:
    """One small field, for the file that carries ten thousand rows."""

    n: int


# ---- helpers ----------------------------------------------------------------


def cell_row(**edits):
    """A whole Cell in transport form, then whatever the hand changed."""
    row = {"n": 1, "flag": True, "day": "2026-07-30", "colour": "HIGH"}
    row.update(edits)
    return row


def a_cell(**edits):
    values = dict(n=1, flag=True, day=date(2026, 7, 30), colour=Colour.HIGH)
    values.update(edits)
    return Cell(**values)


def load_failure(open_store, cls, directory) -> str:
    with pytest.raises(StoreLoadError) as failure:
        open_store(cls, directory)

    return str(failure.value)


def core_says(cls, row) -> str:
    """What the core says about this row, taken live rather than copied by hand.

    The row travels the way the store sends it: through `decode` first.
    """
    schema = struct_of(cls)

    with pytest.raises((TypeError, ValueError)) as failure:
        schema.build(decode(schema, row))

    return str(failure.value)


def core_says_raw(cls, row) -> str:
    """What the core says about the row exactly as the file carries it.

    Used only where the two answers differ — that difference is the finding.
    """
    schema = struct_of(cls)

    with pytest.raises((TypeError, ValueError)) as failure:
        schema.build(row)

    return str(failure.value)


def rotated(path):
    """The rotated copies of `path`, oldest stamp first."""
    pattern = re.compile(rf"^{re.escape(path.stem)}\.(\d+){re.escape(path.suffix)}$")
    found = [(int(match.group(1)), copy) for copy in path.parent.iterdir()
             if (match := pattern.match(copy.name))]

    return [copy for _, copy in sorted(found)]


@pytest.fixture
def cell_file(file_of, store_dir):
    return file_of(Cell, store_dir)


@pytest.fixture
def wrapped_file(file_of, store_dir):
    return file_of(Wrapped, store_dir)


@pytest.fixture
def widened_file(file_of, store_dir):
    return file_of(Widened, store_dir)


@pytest.fixture
def counter_file(file_of, store_dir):
    return file_of(Counter, store_dir)


# ---- the wrapper: $type and $value -----------------------------------------


def test_a_wrapper_naming_an_option_that_does_not_exist_offers_the_ones_that_do(
        open_store, store_dir, by_hand, wrapped_file):
    """decode cannot match the name, so the wrapper travels intact and the core
    names it, the key it sat under, and every option it could have been."""
    row = {"values": {"$type": "list[float]", "$value": []}}
    by_hand(Wrapped, store_dir, [row])

    message = load_failure(open_store, Wrapped, store_dir)

    assert message == f"{wrapped_file}: row 1: {core_says(Wrapped, row)}"
    assert message.endswith(
        "values: $type: not a choice: 'list[float]', "
        "expected one of ('list[str]', 'list[int]')")


def test_a_wrapper_whose_value_belongs_to_the_other_branch_is_judged_as_the_branch_it_named(
        open_store, store_dir, by_hand, wrapped_file):
    """`$type` is not a hint the loader may overrule: it names list[int], so the
    strings inside are wrong there rather than right somewhere else."""
    row = {"values": {"$type": "list[int]", "$value": ["a", "b"]}}
    by_hand(Wrapped, store_dir, [row])

    message = load_failure(open_store, Wrapped, store_dir)

    assert message == f"{wrapped_file}: row 1: {core_says(Wrapped, row)}"
    assert message.endswith("values: $value: [0]: expected int, got str")


def test_a_type_without_its_value_names_the_key_that_is_missing(
        open_store, store_dir, by_hand, wrapped_file):
    row = {"values": {"$type": "list[int]"}}
    by_hand(Wrapped, store_dir, [row])

    message = load_failure(open_store, Wrapped, store_dir)

    assert message == f"{wrapped_file}: row 1: {core_says(Wrapped, row)}"
    assert message.endswith("values: missing key(s): $value")


def test_a_value_without_its_type_asks_for_the_discriminator_and_lists_the_options(
        open_store, store_dir, by_hand, wrapped_file):
    row = {"values": {"$value": ["a"]}}
    by_hand(Wrapped, store_dir, [row])

    message = load_failure(open_store, Wrapped, store_dir)

    assert message == f"{wrapped_file}: row 1: {core_says(Wrapped, row)}"
    assert "field accepts list[str] | list[int]" in message
    assert '{"$type": ..., "$value": ...}' in message


def test_a_bare_value_where_the_wrapper_is_required_is_refused_rather_than_guessed(
        open_store, store_dir, by_hand, wrapped_file):
    """A list of ints could be list[int] or an empty-ish list[str] the reader
    means differently; the core reads no contents to invent a branch."""
    row = {"values": [1, 2]}
    by_hand(Wrapped, store_dir, [row])

    message = load_failure(open_store, Wrapped, store_dir)

    assert message == f"{wrapped_file}: row 1: {core_says(Wrapped, row)}"
    assert "ambiguous list: field accepts list[str] | list[int]" in message


def test_a_wrapper_the_core_never_sees_still_routes_on_the_type_it_names(
        open_store, store_dir, by_hand):
    """`str | date` collide on the wire and not in Python, so decode consumes the
    wrapper — and what it hands over is the branch `$type` asked for, not the
    one the text would have fallen into on its own."""
    by_hand(Widened, store_dir, {"1": {"when": {"$type": "str",
                                                "$value": "2026-07-30"}},
                                 "2": {"when": {"$type": "date",
                                                "$value": "2026-07-30"}}})
    store = open_store(Widened, store_dir)

    assert store.all() == [(1, Widened(when="2026-07-30")),
                           (2, Widened(when=date(2026, 7, 30)))]


def test_a_wrapper_whose_text_is_not_the_option_it_names_is_never_filed_under_str(
        open_store, store_dir, by_hand, widened_file):
    """The wrapper is what says the text was meant as a date. Consuming it would
    hand a broken date to the str branch and call the row good."""
    row = {"when": {"$type": "date", "$value": "2026-13-01"}}
    by_hand(Widened, store_dir, [row])

    message = load_failure(open_store, Widened, store_dir)

    assert message == f"{widened_file}: row 1: {core_says(Widened, row)}"
    assert message.endswith("when: expected str | date, got dict")


def test_a_key_beside_the_wrapper_travels_intact_for_the_core_to_refuse(
        open_store, store_dir, by_hand):
    """A wrapper is those two keys and nothing else.

    A third key beside them is a hand edit, and reading the wrapper anyway would
    drop it before the core could refuse it — the store would open on a file its
    own core rejects, and the next dump would erase the edit without a word. The
    dict travels intact instead, and the load fails with the core's own line.
    """
    row = {"values": {"$type": "list[int]", "$value": [1], "junk": "typed by hand"}}
    by_hand(Wrapped, store_dir, [row])

    assert load_failure(open_store, Wrapped, store_dir).endswith(
        core_says_raw(Wrapped, row))


def test_a_wrapper_nested_inside_another_wrapper_is_refused(
        open_store, store_dir, by_hand):
    """The payload of a wrapper is data; a wrapper there is a broken file.

    Unwrapping it a second time would hand the core a clean list the file never
    carried, and the row would load as something nobody wrote. It travels intact
    and the core refuses it.
    """
    row = {"values": {"$type": "list[int]",
                      "$value": {"$type": "list[int]", "$value": [1]}}}
    by_hand(Wrapped, store_dir, [row])

    assert load_failure(open_store, Wrapped, store_dir).endswith(
        core_says_raw(Wrapped, row))


def test_a_nested_wrapper_over_a_consumed_one_is_refused_too(
        open_store, store_dir, by_hand):
    """The same rule where decode owns the wrapper end to end."""
    row = {"when": {"$type": "date",
                    "$value": {"$type": "date", "$value": "2026-07-30"}}}
    by_hand(Widened, store_dir, [row])

    assert load_failure(open_store, Widened, store_dir).endswith(
        core_says_raw(Widened, row))


# ---- what shape a row is ----------------------------------------------------


@pytest.mark.parametrize("row, found", [
    ([{"n": 1}], "list"),
    (None, "NoneType"),
    ("n=1, flag=true", "str"),
    (7, "int"),
], ids=["an array", "null", "a string", "a number"])
def test_a_row_that_is_not_an_object_names_what_it_is(
        open_store, store_dir, by_hand, row, found, cell_file):
    """decode hands a non-dict straight through — converting a row is the core's
    to refuse, not the codec's to attempt."""
    by_hand(Cell, store_dir, {"1": row})

    message = load_failure(open_store, Cell, store_dir)

    assert message == f"{cell_file}: row 1: {core_says(Cell, row)}"
    assert message == f"{cell_file}: row 1: expected dict, got {found}"


def test_the_one_broken_row_among_ten_thousand_is_named_by_its_own_id(
        open_store, store_dir, write_payload, counter_file):
    """The id in the message is the key the file used, not the position the
    loader reached: a reader greps for `row 9999` and finds the line to fix.

    Ten thousand rows load in about a tenth of a second, so this stays an
    ordinary test rather than a slow one.
    """
    rows = {str(i): {"n": i} for i in range(1, ROWS + 1)}
    rows[str(BROKEN_ROW)] = {"n": "not a number"}
    write_payload(counter_file, {"v": 1, "next_id": ROWS + 1, "rows": rows})

    started = time.monotonic()
    message = load_failure(open_store, Counter, store_dir)
    elapsed = time.monotonic() - started

    assert message == (f"{counter_file}: row {BROKEN_ROW}: "
                       f"n: expected int, got str")
    assert elapsed < 5.0, f"{ROWS} rows took {elapsed:.1f}s to refuse"


def test_a_file_of_ten_thousand_sound_rows_loads_whole(
        open_store, store_dir, write_payload, counter_file):
    """The counterpart: the size is not what the refusal above was about."""
    write_payload(counter_file, {"v": 1, "next_id": ROWS + 1,
                                 "rows": {str(i): {"n": i}
                                          for i in range(1, ROWS + 1)}})
    store = open_store(Counter, store_dir)

    assert len(store) == ROWS
    assert store.get(BROKEN_ROW) == Counter(n=BROKEN_ROW)
    assert store.add(Counter(n=0)) == ROWS + 1


# ---- a value of the wrong type ----------------------------------------------


@pytest.mark.parametrize("edit, found", [
    ({"n": "42"}, "n: expected int, got str"),
    ({"n": 1.0}, "n: expected int, got float"),
    ({"n": True}, "n: expected int, got bool"),
    ({"flag": 1}, "flag: expected bool, got int"),
    ({"flag": "true"}, "flag: expected bool, got str"),
], ids=["a number as text", "a float in an int", "true in an int",
        "one in a bool", "text in a bool"])
def test_a_value_of_the_neighbouring_type_is_refused_and_never_coerced(
        open_store, store_dir, by_hand, edit, found, cell_file):
    """Nothing on this path converts: `"42"` is not 42, `1.0` is not 1, and the
    bool guard in `_decode_options` keeps a JSON `true` out of an int — bool
    subclasses int, and `type(value) is int` is the question that says so."""
    row = cell_row(**edit)
    by_hand(Cell, store_dir, [row])

    message = load_failure(open_store, Cell, store_dir)

    assert message == f"{cell_file}: row 1: {core_says(Cell, row)}"
    assert message == f"{cell_file}: row 1: {found}"


# ---- a date that is not one -------------------------------------------------


@pytest.mark.parametrize("text", [
    "2026-13-01",
    "2026-02-30",
    "2026-1-1",
    "2026-07-30T09:05:00",
    "30/07/2026",
    "",
], ids=["month 13", "february 30", "no leading zero", "an ISO datetime",
        "a local format", "empty"])
def test_text_that_is_not_a_date_stays_text_and_the_core_refuses_the_row(
        open_store, store_dir, by_hand, text, cell_file):
    """`_convert` swallows the ValueError and returns the string untouched, so
    the refusal is the core's and reads as a type error: the field wanted a date
    and what survived decode was a str."""
    row = cell_row(day=text)
    by_hand(Cell, store_dir, [row])

    message = load_failure(open_store, Cell, store_dir)

    assert message == f"{cell_file}: row 1: {core_says(Cell, row)}"
    assert message == f"{cell_file}: row 1: day: expected date, got str"


@pytest.mark.parametrize("text, means", [
    ("20260101", date(2026, 1, 1)),
    ("2026-W01-1", date(2025, 12, 29)),
], ids=["the basic format", "an ISO week date"])
def test_a_date_python_can_read_loads_and_is_rewritten_in_the_extended_format(
        open_store, store_dir, by_hand, until, rows_of, text, means, cell_file):
    """A known limit, not a defect of the store: `date.fromisoformat` has parsed
    the whole of ISO 8601 since 3.11, so these are dates and the row is sound.

    The transport is narrower than the parser, so the next dump writes the day
    back in the one form the store emits — a week date silently becomes the
    Monday it names, in another year. Worth knowing before hand-editing dates.
    """
    by_hand(Cell, store_dir, [cell_row(day=text)])
    store = open_store(Cell, store_dir, debounce=0.0)

    assert store.all() == [(1, a_cell(day=means))]

    store.add(a_cell(n=2))

    assert until(lambda: rows_of(cell_file)["1"]["day"] == means.isoformat()), (
        "the day was expected to come back in the extended format")


# ---- an enum member that is not one -----------------------------------------


@pytest.mark.parametrize("written", [
    Colour.LOW.value,
    Colour.HIGH.value,
    Colour.LOW.name.title(),
    Size.SMALL.name,
    "MEDIUM",
], ids=["the value not the name", "the other value", "the name in title case",
        "a member of another enum", "a name nobody has"])
def test_only_the_member_name_names_a_member(
        open_store, store_dir, by_hand, written, cell_file):
    """The file carries the NAME, and `__members__` is an exact lookup: the
    VALUE of a member is not a second spelling of it, and neither is its name in
    another case. A KeyError leaves the string as it was for the core."""
    row = cell_row(colour=written)
    by_hand(Cell, store_dir, [row])

    message = load_failure(open_store, Cell, store_dir)

    assert message == f"{cell_file}: row 1: {core_says(Cell, row)}"
    assert message == f"{cell_file}: row 1: colour: expected Colour, got str"


def test_the_member_name_is_what_loads(open_store, store_dir, by_hand):
    """The other half: LOW is a member of Colour and the row is sound."""
    by_hand(Cell, store_dir, [cell_row(colour="LOW")])

    assert open_store(Cell, store_dir).get(1) == a_cell(colour=Colour.LOW)


# ---- the file as a whole ----------------------------------------------------


def test_a_utf8_bom_in_front_of_the_json_is_a_load_error_and_not_a_silent_read(
        open_store, store_dir, cell_file):
    """Documented behaviour, taken from the parser in person: `read_text` with
    "utf-8" keeps the BOM as U+FEFF, and `json.loads` refuses it — and says which
    encoding would have eaten it. An editor that saves "UTF-8 with BOM" is
    therefore a refusal at the door, never a half-read file."""
    body = '{"v": 1, "next_id": 2, "rows": {"1": ' + json.dumps(cell_row()) + "}}"
    cell_file.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))

    with pytest.raises(json.JSONDecodeError) as refusal:
        json.loads("﻿" + body)

    message = load_failure(open_store, Cell, store_dir)

    assert message == f"{cell_file}: not valid JSON: {refusal.value}"
    assert "utf-8-sig" in message, "the message no longer says what would read it"


def test_a_file_saved_as_utf16_is_refused_before_it_is_parsed(
        open_store, store_dir, cell_file):
    raw = ('{"v": 1, "next_id": 2, "rows": {"1": '
           + json.dumps(cell_row()) + "}}").encode("utf-16")
    cell_file.write_bytes(raw)

    with pytest.raises(UnicodeDecodeError) as refusal:
        raw.decode("utf-8")

    assert load_failure(open_store, Cell, store_dir) == (
        f"{cell_file}: not valid UTF-8: {refusal.value}")


def test_a_sound_file_of_another_class_fails_row_by_row_on_the_keys(
        open_store, store_dir, by_hand, cell_file):
    """A copy of another database renamed onto this one's path. The fingerprint
    in the name is what normally keeps the two apart, so what is left is the
    rows: every one of them is a whole row of the wrong shape.

    The message names the file, the row and the keys that do not belong. It does
    not say "this looks like another class's database" — the keys are the
    evidence a reader has to draw that from, and for a wide dataclass they are
    the first key alphabetically and nothing else.
    """
    row = {"tags": [{"name": "milk"}], "note": "written by Bag"}
    by_hand(Bag, store_dir, [row])
    by_hand(Cell, store_dir, [row])

    message = load_failure(open_store, Cell, store_dir)

    assert message == f"{cell_file}: row 1: {core_says(Cell, row)}"
    assert message == f"{cell_file}: row 1: unexpected key(s): note, tags"


def test_a_row_of_this_class_with_a_hole_in_it_names_every_key_it_wants(
        open_store, store_dir, by_hand, cell_file):
    """The other half of a class mismatch: keys that overlap and stop short."""
    row = {"n": 1}
    by_hand(Cell, store_dir, [row])

    message = load_failure(open_store, Cell, store_dir)

    assert message == f"{cell_file}: row 1: {core_says(Cell, row)}"
    assert message == f"{cell_file}: row 1: missing key(s): colour, day, flag"


@pytest.mark.parametrize("version", [2, 0, "1", 1.0, None], ids=[
    "a version from the future", "a version from before", "the version as text",
    "the version as a float", "no version at all"])
def test_any_version_but_the_one_it_writes_is_refused_before_a_row_is_read(
        open_store, store_dir, write_payload, version, cell_file):
    """The type is half the question.

    `1.0 == 1` and `True == 1` in Python, so an equality alone would read either
    as version 1 and hand the rows to a reader that never wrote them. The guard
    asks `type(...) is int` first, the same way `next_id` does below.
    """
    payload = {"next_id": 2, "rows": {"1": cell_row()}}

    if version is not None:
        payload["v"] = version

    write_payload(cell_file, payload)

    assert load_failure(open_store, Cell, store_dir) == (
        f"{cell_file}: unknown format version {version!r}, expected 1")


def test_the_version_it_writes_is_the_version_it_reads(
        open_store, store_dir, write_payload, cell_file):
    """The other side of the guard: a plain int 1 loads."""
    write_payload(cell_file, {"v": 1, "next_id": 2, "rows": {"1": cell_row()}})

    assert open_store(Cell, store_dir).get(1) == a_cell()


@pytest.mark.parametrize("next_id", ["3", None, 3.0, True], ids=[
    "as text", "as null", "as a float", "as a bool"])
def test_a_next_id_that_is_not_an_int_is_ignored_and_the_rows_decide(
        open_store, store_dir, write_payload, next_id, cell_file):
    """`type(stored) is int` is the guard, so a JSON `true` is not 1 here either;
    what is left is `highest + 1`, which is the only value that cannot collide."""
    write_payload(cell_file, {"v": 1, "next_id": next_id,
                              "rows": {"4": cell_row()}})
    store = open_store(Cell, store_dir)

    assert store.add(a_cell(n=2)) == 5


def test_a_negative_next_id_cannot_pull_an_id_below_the_rows_present(
        open_store, store_dir, write_payload, cell_file):
    """`max(highest + 1, stored)` — a negative would otherwise hand out an id a
    row already holds, and the next dump would write the survivor alone."""
    write_payload(cell_file, {"v": 1, "next_id": -5, "rows": {"4": cell_row()}})
    store = open_store(Cell, store_dir)

    assert store.add(a_cell(n=2)) == 5


def test_an_enormous_next_id_is_honoured_whole_and_survives_the_round_trip(
        open_store, store_dir, write_payload, rows_of, payload_of, cell_file):
    """Python has no int ceiling and JSON does not either, so a hand-typed
    absurdity is a legal id: it is written as text, read back by `int()`, and
    the store keeps counting from there. Nothing overflows and no row is lost —
    the only cost is a key no reader will enjoy.
    """
    absurd = 2 ** 70
    write_payload(cell_file, {"v": 1, "next_id": absurd,
                              "rows": {"1": cell_row()}})
    store = open_store(Cell, store_dir)

    assert store.add(a_cell(n=2)) == absurd

    store.close()

    assert sorted(rows_of(cell_file)) == ["1", str(absurd)]
    assert payload_of(cell_file)["next_id"] == absurd + 1
    assert [row_id for row_id, _ in open_store(Cell, store_dir).all()] == [1, absurd]


def test_two_identical_row_keys_are_settled_by_json_loads_and_the_last_one_wins(
        open_store, store_dir, cell_file):
    """A known limit, written down rather than defended.

    `{"1": ..., "1": ...}` is legal JSON and `json.loads` keeps the last of the
    pair without a word; the store is handed a dict of one entry and cannot know
    there ever were two. The guard that catches `"1"` against `"01"` works
    because those are two different strings — this pair is not.

    Detecting it needs `object_pairs_hook` or a parser of one's own. Until there
    is one, the row a reader sees is the LAST of the duplicates, and a hand edit
    made to the first is invisible.
    """
    first = json.dumps(cell_row(n=11))
    second = json.dumps(cell_row(n=22))
    cell_file.write_text(
        f'{{"v": 1, "next_id": 2, "rows": {{"1": {first}, "1": {second}}}}}',
        encoding="utf-8")

    assert json.loads(f'{{"1": {first}, "1": {second}}}')["1"]["n"] == 22

    store = open_store(Cell, store_dir)

    assert store.all() == [(1, a_cell(n=22))]


# ---- what else is lying in the directory ------------------------------------


def test_an_orphan_part_file_from_a_crash_is_ignored_and_then_overwritten(
        open_store, store_dir, by_hand, until, rows_of, cell_file):
    """A process killed between the write and the replace leaves `<file>.part`
    behind. The load reads its own path and nothing else, so the orphan is
    invisible; the next dump opens the same name with "w", which truncates, and
    `os.replace` carries it away. Nothing of the old content survives.
    """
    orphan = cell_file.with_name(cell_file.name + ".part")
    orphan.write_text('{"v": 1, "next_id": 999, "rows": {"5": half a row',
                      encoding="utf-8")
    by_hand(Cell, store_dir, [cell_row()])

    store = open_store(Cell, store_dir, debounce=0.0)

    assert store.all() == [(1, a_cell())], "the orphan reached the rows"

    store.add(a_cell(n=2))

    assert until(lambda: sorted(rows_of(cell_file)) == ["1", "2"]), (
        "the dump never landed")
    assert until(lambda: not orphan.exists()), (
        "the orphan .part outlived the dump that reused its name")


def test_a_corrupt_rotated_copy_is_listed_for_the_prune_and_never_read(
        open_store, store_dir, by_hand, until, cell_file):
    """A copy is a name and a timestamp as far as the store is concerned; its
    contents are never opened, so garbage inside one cannot fail a load. It is
    counted by the prune like any other, and pruned when its turn comes."""
    stale = cell_file.with_name(f"{cell_file.stem}.1{cell_file.suffix}")
    stale.write_text("this was never JSON", encoding="utf-8")
    by_hand(Cell, store_dir, [cell_row()])

    store = open_store(Cell, store_dir, debounce=0.0, keep=1)

    assert store.all() == [(1, a_cell())], "the corrupt copy reached the load"

    store.add(a_cell(n=2))

    # The stale copy is already the one copy in the directory, so a count of one
    # is true before the dump as well: what is waited for is the corrupt name
    # going away, which only the prune can do.
    assert until(lambda: not stale.exists()), (
        "the oldest copy survived a keep of 1")

    surviving = rotated(cell_file)

    assert len(surviving) == 1, [copy.name for copy in surviving]
    assert json.loads(surviving[0].read_text(encoding="utf-8"))["v"] == 1
    assert store.all() == [(1, a_cell()), (2, a_cell(n=2))]


def test_a_corrupt_copy_stamped_in_the_future_is_kept_as_the_newest(
        open_store, store_dir, by_hand, until, cell_file):
    """The order the prune keeps is the stamp in the name, and a hand-written one
    outranks every real dump. Documented, not defended: a copy is opaque, and a
    directory somebody has been typing in is not the store's to police."""
    forever = cell_file.with_name(f"{cell_file.stem}.{2 ** 63}{cell_file.suffix}")
    forever.write_text("this was never JSON", encoding="utf-8")
    by_hand(Cell, store_dir, [cell_row()])

    store = open_store(Cell, store_dir, debounce=0.0, keep=1)

    for n in range(2, 5):
        store.add(a_cell(n=n))

    assert until(lambda: rotated(cell_file) == [forever]), (
        f"the prune kept {[p.name for p in rotated(cell_file)]}")


@pytest.mark.skipif(os.name != "nt",
                    reason="on POSIX the flock is the evidence, not the pid: "
                           "see the twin below")
def test_a_lockfile_carrying_this_very_process_id_refuses_the_open(
        store_dir, cell_file, lock_of, open_store):
    """A crash, then a pid the operating system handed out again — to us. On
    Windows there is nothing in the file to tell that apart from a second live
    worker, and the store does the only safe thing: it names the owner and
    refuses.

    Documented rather than judged. The accusation is correct as far as the
    evidence goes, it just happens to point at the reader; the message already
    says what to do about it, and deleting the lockfile by hand is the way out.
    """
    lock = lock_of(cell_file)
    lock.write_text(str(os.getpid()), encoding="utf-8")

    with pytest.raises(StoreLockedError) as refusal:
        open_store(Cell, store_dir)

    assert str(refusal.value) == (
        f"{cell_file} is owned by process {os.getpid()}. A store belongs to one "
        f"process; you are probably running several workers. Run a single "
        f"worker, or reach for a database server — this is not one.")
    assert lock.exists(), "a lock the store did not take was removed anyway"
    assert lock.read_text(encoding="utf-8") == str(os.getpid())


@pytest.mark.skipif(os.name == "nt",
                    reason="the twin above: on Windows the pid is the evidence")
def test_a_lockfile_nobody_holds_is_taken_over_whatever_pid_it_names(
        store_dir, cell_file, lock_of, open_store):
    """On POSIX the lock is the `flock`, and a pid is only what the message
    quotes. A lockfile naming a live process that is not holding it — a crash
    plus a recycled pid, or a file written by hand — is an orphan, and taking
    it over is right: the process it names is demonstrably not the owner.

    This is where the two platforms part, and POSIX has the better half of it:
    a recycled pid cannot lock a path out here.
    """
    lock = lock_of(cell_file)
    lock.write_text(str(os.getpid()), encoding="utf-8")

    store = open_store(Cell, store_dir)

    assert store.add(a_cell()) == 1
    assert lock.read_text(encoding="utf-8") == str(os.getpid())
