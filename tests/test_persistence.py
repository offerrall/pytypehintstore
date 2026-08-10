"""The shadow on disk: what is dumped, what comes back, and how a load fails.

The file is the store's only durable state and a contract a person can open and
edit, so what is asserted here is the raw JSON — read with `json.loads`, never
through the codec — and the exact words of every refusal to read it back.

Four areas: the round trip through `close()` and a reopen; the load errors, one
per test and quoted whole; the atomic dump and the rotated copies it leaves; and
the path a row carries, which used to be the one declared exception to "a row
that went in comes back out" and is no longer: the core validates the extension
of the text and never looks at the file, so the row comes back whatever the
filesystem has done since.

What is not here: the dict in memory (test_store), the transport of each type
(test_codec), the writer thread and shutdown (test_writer), the lockfile
(test_lockfile). The debounce as a mechanism is test_writer's; its result on
disk is this module's.
"""

import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, time as clock
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal

import pytest

from pytypehint import Choices, FileHint, Max, Min, MultipleOf, Pattern, struct_of
from pytypehintstore import StoreLoadError
from pytypehintstore.lockfile import alive
from shared import Task

TESTS = Path(__file__).resolve().parent

DAY = date(2026, 7, 30)
AT = clock(9, 5)

# An accent, a punctuation dash and a snowman: the three things ensure_ascii
# would have escaped.
UNICODE_TITLE = "café — naïve ☃"


# ---- shapes -----------------------------------------------------------------


class Colour(Enum):
    LOW = "low"
    HIGH = "high"


@dataclass
class Stamp:
    day: date
    at: clock


@dataclass
class Entry:
    """One row carrying every kind of value a hand edit can put out of range."""

    label: Annotated[str, Min(1), Max(8)]
    count: Annotated[int, Min(1), Max(10), MultipleOf(5)]
    code: Annotated[str, Pattern(r"[A-Z]{2}-[0-9]{3}")]
    pick: Annotated[str, Choices(values=("draft", "final"))]
    mode: Literal["fast", "safe"]
    colour: Colour
    stamp: Stamp
    tags: list[str] = field(default_factory=list)


@dataclass
class Attachment:
    path: Annotated[str, FileHint(extensions=(".txt",), max_size=8)]


# ---- helpers ----------------------------------------------------------------


def an_entry(**edits):
    values = dict(label="milk", count=5, code="AB-123", pick="draft", mode="fast",
                  colour=Colour.HIGH, stamp=Stamp(DAY, AT), tags=["dairy"])
    values.update(edits)
    return Entry(**values)


def entry_row(**edits):
    """The same row in transport form, the way a person types it into the file."""
    row = {"label": "milk", "count": 5, "code": "AB-123", "pick": "draft",
           "mode": "fast", "colour": "HIGH",
           "stamp": {"day": "2026-07-30", "at": "09:05:00"}, "tags": ["dairy"]}
    row.update(edits)
    return row


def load_failure(open_store, cls, path) -> str:
    with pytest.raises(StoreLoadError) as failure:
        open_store(cls, path)

    return str(failure.value)


def core_message(cls, row) -> str:
    """What the core says about this row, taken live rather than copied by hand."""
    schema = struct_of(cls)

    with pytest.raises((TypeError, ValueError)) as failure:
        schema.build(schema.decode(row))

    return str(failure.value)


def rotated(path):
    """The rotated copies of `path`, oldest stamp first.

    The pattern is derived from the store's own path, so a file named anything
    else in the same directory is not mistaken for a copy.
    """
    pattern = re.compile(
        rf"^{re.escape(path.stem)}\.(\d+){re.escape(path.suffix)}$")
    found = [(int(match.group(1)), copy) for copy in path.parent.iterdir()
             if (match := pattern.match(copy.name))]

    return [copy for _, copy in sorted(found)]


def rows_in(copy) -> int:
    return len(json.loads(copy.read_text(encoding="utf-8"))["rows"])


def keys_on_disk(path):
    """The row ids the file carries, or None while a dump is landing on it.

    A dump is counted at `os.replace`, which is the call the arrival depends on
    and not the instant it is over: on Windows a read that catches the replace
    mid-flight is refused. None says "not yet", so a poll can wait for it.
    """
    try:
        return sorted(json.loads(Path(path).read_text(encoding="utf-8"))["rows"])
    except (OSError, ValueError):
        return None


# ---- close, reopen, and the ids -------------------------------------------


@pytest.fixture
def store_file(file_of, store_dir):
    """The file `Entry`, this module's main class, lands on."""
    return file_of(Entry, store_dir)


@pytest.fixture
def task_file(file_of, store_dir):
    """The file `Task` lands on, in that same directory."""
    return file_of(Task, store_dir)


@pytest.fixture
def attachment_file(file_of, store_dir):
    """The file `Attachment` lands on, in that same directory."""
    return file_of(Attachment, store_dir)


def test_close_dumps_the_rows_and_reopening_recovers_them_with_their_next_id(
        open_store, store_dir, payload_of):
    store = open_store(Entry, store_dir)
    written = [(store.add(an_entry(label=label)), an_entry(label=label))
               for label in ("a", "b", "c")]
    store.close()

    assert payload_of(store.path)["next_id"] == 4

    reopened = open_store(Entry, store_dir)

    assert reopened.all() == written
    assert reopened.add(an_entry(label="d")) == 4


def test_the_next_id_survives_the_removal_of_the_highest_row(
        open_store, store_dir):
    """Ids are spent, not recycled: neither the dict nor the file hands one back."""
    store = open_store(Entry, store_dir)

    for label in ("a", "b", "c"):
        store.add(an_entry(label=label))

    store.remove(3)
    assert store.add(an_entry(label="d")) == 4
    store.close()

    reopened = open_store(Entry, store_dir)
    reopened.remove(4)

    assert [row_id for row_id, _ in reopened.all()] == [1, 2]
    assert reopened.add(an_entry(label="e")) == 5


def test_an_absent_file_is_an_empty_store_not_an_error(open_store, store_dir, store_file):
    assert not store_file.exists()
    store = open_store(Entry, store_dir)

    assert store.all() == []
    assert store.add(an_entry()) == 1


def test_a_directory_that_does_not_exist_yet_is_created(
        open_store, tmp_path, file_of):
    directory = tmp_path / "state" / "deeper"
    store = open_store(Entry, directory)
    store.add(an_entry())
    store.close()

    assert file_of(Entry, directory).is_file()


def test_a_file_where_the_directory_should_be_is_refused_at_open(
        open_store, tmp_path):
    """The store is opened on a directory; anything else is a mistake to catch
    at the door rather than a file to write into."""
    from pytypehintstore import StoreError

    occupied = tmp_path / "data"
    occupied.write_text("not a directory", encoding="utf-8")

    with pytest.raises(StoreError) as refusal:
        open_store(Entry, occupied)

    assert str(refusal.value) == f"{occupied}: not a directory"


# ---- a file that cannot be read --------------------------------------------


def test_a_file_that_is_not_json_quotes_the_parser_that_refused_it(
        open_store, store_dir, store_file):
    text = "this was never JSON\n"
    store_file.write_text(text, encoding="utf-8")

    with pytest.raises(json.JSONDecodeError) as refusal:
        json.loads(text)

    assert load_failure(open_store, Entry, store_dir) == (
        f"{store_file}: not valid JSON: {refusal.value}")


@pytest.mark.parametrize("raw", [
    '{"v": 1, "next_id": 1, "rows": {}}'.encode("utf-16"),
    b'{"v": 1, "next_id": 2, "rows": {"1": {"title": "caf\xe9"}}}',
], ids=["utf-16", "a stray byte"])
def test_a_file_that_is_not_utf8_is_a_load_error_and_not_a_decode_error(
        open_store, store_dir, raw, task_file):
    """An editor that saved in another encoding is a way the file can fail to be
    read, and every one of those is the store's to report."""
    task_file.write_bytes(raw)

    with pytest.raises(UnicodeDecodeError) as refusal:
        raw.decode("utf-8")

    assert load_failure(open_store, Task, store_dir) == (
        f"{task_file}: not valid UTF-8: {refusal.value}")


def test_a_root_that_is_not_an_object_names_what_it_found(open_store, store_dir, store_file):
    store_file.write_text('[{"v": 1}]', encoding="utf-8")

    assert load_failure(open_store, Entry, store_dir) == (
        f"{store_file}: expected an object, got list")


def test_an_unknown_version_names_the_one_it_can_read(
        open_store, store_dir, write_payload, store_file):
    write_payload(store_file, {"v": 2, "next_id": 1, "rows": {}})

    assert load_failure(open_store, Entry, store_dir) == (
        f"{store_file}: unknown format version 2, expected 1")


def test_rows_that_are_not_an_object_name_what_they_are(
        open_store, store_dir, write_payload, store_file):
    write_payload(store_file, {"v": 1, "next_id": 1, "rows": [entry_row()]})

    assert load_failure(open_store, Entry, store_dir) == (
        f"{store_file}: rows must be an object, got list")


def test_a_row_id_with_two_signs_is_rejected_before_it_is_converted(
        open_store, store_dir, by_hand, store_file):
    """`int("--5")` raises; the guard has to be the one that says no."""
    by_hand(Entry, store_dir, {"--5": entry_row()}, next_id=1)

    assert load_failure(open_store, Entry, store_dir) == (
        f"{store_file}: row id '--5' is not an integer")


def test_a_row_id_written_as_a_superscript_digit_is_rejected(
        open_store, store_dir, by_hand, store_file):
    """`str.isdigit()` accepts it and `int()` does not, so isdigit is not the guard."""
    by_hand(Entry, store_dir, {"⁵": entry_row()}, next_id=1)

    assert load_failure(open_store, Entry, store_dir) == (
        f"{store_file}: row id '⁵' is not an integer")


def test_two_row_keys_that_are_the_same_integer_fail_the_load(
        open_store, store_dir, write_payload, store_file):
    """"1" and "01" are two keys and one id: loading the second over the first
    would drop a row, and the next dump would write the survivor alone."""
    write_payload(store_file, {"v": 1, "next_id": 3,
                               "rows": {"1": entry_row(label="first"),
                                        "01": entry_row(label="second")}})

    assert load_failure(open_store, Entry, store_dir) == (
        f"{store_file}: row id '01' is already taken by another key")


# ---- a row that does not validate ------------------------------------------


@pytest.mark.parametrize("edit", [
    {"label": "much too long"},
    {"count": 7},
    {"count": 99},
    {"code": "ab-123"},
    {"pick": "sketch"},
    {"mode": "quick"},
    {"colour": "NOPE"},
], ids=["max", "multiple of", "min", "pattern", "choices", "literal", "enum"])
def test_a_row_that_does_not_validate_names_the_file_the_row_and_the_core_error(
        open_store, store_dir, by_hand, edit, store_file):
    """One contract, whatever the mark: the load stops and the core's own words
    arrive whole, behind the path and the row the file gave them."""
    row = entry_row(**edit)
    by_hand(Entry, store_dir, [row])

    assert load_failure(open_store, Entry, store_dir) == (
        f"{store_file}: row 1: {core_message(Entry, row)}")


def test_a_row_missing_a_field_fails_the_load_in_the_cores_bare_words(
        open_store, store_dir, by_hand, task_file):
    """A hole in a row is a hand edit, not a schema that moved on: the file
    belongs to this exact schema or it is another file. So the core's own line
    is the whole message — no advice to give the field a default, which would
    fork the database rather than fix the row."""
    by_hand(Task, store_dir, [{"done": False}])

    message = load_failure(open_store, Task, store_dir)

    assert message == f"{task_file}: row 1: missing key(s): title"
    assert "default in the dataclass" not in message


def test_a_key_missing_inside_a_child_carries_the_whole_path(
        open_store, store_dir, by_hand, store_file):
    """The core names where the hole is, however deep."""
    row = entry_row()
    del row["stamp"]["day"]
    by_hand(Entry, store_dir, [row])

    assert load_failure(open_store, Entry, store_dir) == (
        f"{store_file}: row 1: stamp: missing key(s): day")


def test_the_highest_row_present_beats_a_next_id_that_would_collide(
        open_store, store_dir, by_hand):
    by_hand(Entry, store_dir, {"7": entry_row()}, next_id=1)
    store = open_store(Entry, store_dir)

    assert store.add(an_entry()) == 8


# ---- the file as a contract a person can read and author --------------------


def test_the_file_is_readable_text_carrying_the_version_the_next_id_and_the_rows(
        open_store, store_dir, payload_of, store_file):
    """Broken over lines and keyed by name — the order of the keys and the width
    of the indent are not the promise; being editable by hand is."""
    store = open_store(Entry, store_dir)
    store.add(an_entry())
    store.close()

    text = store_file.read_text(encoding="utf-8")

    assert "\n" in text.strip()
    assert text.endswith("\n")

    payload = payload_of(store.path)

    assert set(payload) == {"v", "next_id", "rows"}
    assert payload["v"] == 1
    assert list(payload["rows"]) == ["1"]


def test_the_rows_on_disk_carry_the_transport_form_and_not_python_values(
        open_store, store_dir, rows_of):
    """Read with json.loads and never through the codec: a date is ISO text, an
    enum is its name, a nested dataclass is an object."""
    store = open_store(Entry, store_dir)
    store.add(an_entry())
    store.close()

    assert rows_of(store.path) == {"1": entry_row()}


def test_a_non_ascii_value_survives_the_file_verbatim(open_store, store_dir, task_file):
    store = open_store(Task, store_dir)
    store.add(Task(title=UNICODE_TITLE))
    store.close()

    text = task_file.read_text(encoding="utf-8")

    assert UNICODE_TITLE in text
    assert "\\u" not in text
    assert open_store(Task, store_dir).get(1).title == UNICODE_TITLE


def test_a_file_written_by_hand_loads_as_the_instances_it_describes(
        open_store, store_dir, by_hand):
    by_hand(Entry, store_dir, {"1": entry_row(label="milk"),
                         "4": entry_row(label="oats", count=10,
                                        stamp={"day": "2026-01-02",
                                               "at": "23:59:59"})},
            next_id=5)
    store = open_store(Entry, store_dir)

    assert store.all() == [
        (1, an_entry(label="milk")),
        (4, an_entry(label="oats", count=10,
                     stamp=Stamp(date(2026, 1, 2), clock(23, 59, 59)))),
    ]
    assert store.add(an_entry()) == 5


# ---- the dump that cannot land ----------------------------------------------


def test_a_dump_that_cannot_land_leaves_the_previous_file_whole_and_writes_later(
        open_store, store_dir, until, replaces, task_file):
    """The four halves of an atomic write: the file that was there is untouched,
    the part file is gone, the state is still unwritten, and the next dump that
    is allowed to land carries it."""
    store = open_store(Task, store_dir, debounce=0.05)
    store.add(Task(title="first"))
    store.close()

    refused = replaces(fail_forever=True)
    second = open_store(Task, store_dir, debounce=0.05)
    second.add(Task(title="second"))

    assert until(lambda: refused.asked), "the dump never even tried"
    assert refused.landed == []
    assert keys_on_disk(task_file) == ["1"], "the file moved on anyway"
    assert until(lambda: not list(task_file.parent.glob("*.part"))), (
        "a dump that failed left its part file behind")

    allowed = replaces()

    assert until(lambda: allowed.landed), "the retry never reached the disk"
    assert until(lambda: keys_on_disk(task_file) == ["1", "2"]), (
        "the row that never landed was not still pending")


# ---- rotation ---------------------------------------------------------------


@pytest.mark.parametrize("keep", [0, 1, 3])
def test_keep_decides_how_many_rotated_copies_survive_and_they_are_the_newest(
        open_store, store_dir, until, replaces, keep, task_file):
    """Real dumps, one per add, waited for one at a time. The first found no file
    to rotate, so `keep + 2` dumps make `keep + 1` copies of 1..keep+1 rows and
    the prune leaves the newest `keep` of them."""
    landed = replaces()
    store = open_store(Task, store_dir, debounce=0.0, keep=keep)

    for n in range(keep + 2):
        store.add(Task(title=f"task {n}"))
        assert until(lambda wanted=n + 1: len(landed.landed) == wanted), (
            f"dump {n + 1} never landed")

    # The prune follows the replace the dump was counted at, so the count is
    # waited for rather than read the instant the last dump was allowed through.
    assert until(lambda: len(rotated(task_file)) == keep), (
        f"{len(rotated(task_file))} copies survived {keep + 2} dumps, not {keep}")
    assert [rows_in(copy) for copy in rotated(task_file)] == list(range(2, keep + 2))


def test_a_rotated_copy_can_outlive_a_dump_that_never_landed(
        open_store, store_dir, until, replaces, task_file):
    """A decision, not a defect: the copy is taken before the replace, which is
    what keeps the file present at every instant. The number of copies is
    therefore not the number of dumps that arrived."""
    landed = replaces()
    store = open_store(Task, store_dir, debounce=0.05, keep=5)
    store.add(Task(title="first"))

    assert until(lambda: landed.landed), "the first dump never landed"
    assert rotated(task_file) == [], "the first dump found a file to rotate"

    refused = replaces(fail_forever=True)
    store.add(Task(title="second"))

    assert until(lambda: rotated(task_file)), "the failed dump rotated nothing"
    assert refused.landed == []
    assert keys_on_disk(task_file) == ["1"]


# ---- a process taken away mid-write -----------------------------------------


WRITING_CHILD = """
import sys
import time

sys.path.insert(0, {tests!r})

from shared import Task
from pytypehintstore import store_of

store = store_of(Task, {path!r}, debounce=0.01)
n = 0

while True:
    store.add(Task(title="row %d" % n))
    n += 1
    time.sleep(0.02)
"""


@pytest.mark.parametrize("moment", [0.0, 0.07])
def test_a_child_killed_mid_write_leaves_a_whole_file(
        store_dir, spawn_python, until, payload_of, lock_of, moment, task_file):
    """The file goes from one whole JSON to the next, so what a kill leaves is
    one of the two states and never a piece of both."""
    lock = lock_of(task_file)
    killed = spawn_python(WRITING_CHILD.format(tests=str(TESTS),
                                               path=str(store_dir)))

    assert until(lock.exists, timeout=30), "the child never took the lock"
    assert until(task_file.exists, timeout=30), "the child never dumped the file"
    time.sleep(moment)

    killed.kill()
    assert killed.wait(timeout=30) is not None
    assert until(lambda: not alive(killed.pid), timeout=30)

    payload = payload_of(task_file)

    assert payload["v"] == 1
    assert payload["rows"], "the file a kill left carries no row at all"

    for key, row in payload["rows"].items():
        assert set(row) == {"title", "priority", "done"}, f"row {key} is half a row"
        assert row["title"].startswith("row ")


# ---- the path a row carries -------------------------------------------------


def test_a_file_path_is_stored_as_written_and_never_resolved(
        open_store, store_dir, tmp_path, monkeypatch, rows_of):
    """The store resolves the path of its own file; a path inside a row is a
    value, and it reaches the file as the caller wrote it."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "note.txt").write_text("hi", encoding="utf-8")

    store = open_store(Attachment, "tasks.json")
    store.add(Attachment(path="note.txt"))
    store.close()

    assert rows_of(store.path) == {"1": {"path": "note.txt"}}


@pytest.mark.parametrize("damage", ["gone", "grown", "never existed"])
def test_a_row_survives_whatever_the_filesystem_did_to_its_file(
        open_store, store_dir, tmp_path, by_hand, damage):
    """The exception that used to live here is gone with the core's `stat()`.

    A size and an existence are facts about one machine at one instant, and the
    core stopped asking for them in 1.0.0. A path is text that names a file, the
    extension is the part of it the text settles, and the row comes back exactly
    as it was stored no matter what happened to the file in between."""
    note = tmp_path / "note.txt"

    if damage == "never existed":
        stored = str(tmp_path / "was-never-here.txt")
    else:
        note.write_text("12345678", encoding="utf-8")
        stored = str(note)

    by_hand(Attachment, store_dir, [{"path": stored}])

    if damage == "gone":
        note.unlink()
    elif damage == "grown":
        note.write_text("123456789012", encoding="utf-8")

    store = open_store(Attachment, store_dir)
    assert store.all() == [(1, Attachment(path=stored))]


def test_a_path_whose_extension_is_wrong_still_fails_the_load(
        open_store, store_dir, tmp_path, by_hand, attachment_file):
    """What survives of the file contract is the half the text answers.

    No file is written anywhere in this test: the extension is spelled in the
    value, so the refusal needs nothing but the row."""
    by_hand(Attachment, store_dir, [{"path": str(tmp_path / "readme.md")}])

    message = load_failure(open_store, Attachment, store_dir)

    assert message.startswith(f"{attachment_file}: row 1: path: ")
    assert "not an accepted file type" in message


# ---- the file is the shadow, never the source of truth ----------------------


def test_an_edit_made_under_a_running_store_is_neither_read_nor_kept(
        open_store, store_dir, write_payload, until, task_file):
    """The file is read once, at startup, and rewritten whole from memory.

    Both halves of "the shadow" in one test: a hand edit made while the store
    runs is invisible to it — no read goes to the disk after startup — and the
    next dump replaces the whole file rather than adding to it, so the edit is
    gone. This is also why the store is not for millions of rows: every dump
    writes all of them.
    """
    store = open_store(Task, store_dir, debounce=0)
    store.add(Task(title="mine"))

    assert until(lambda: keys_on_disk(task_file) == ["1"]), "the first dump never landed"

    write_payload(task_file, {"v": 1, "next_id": 9,
                               "rows": {"7": {"title": "yours"}}})

    assert [row for _, row in store.all()] == [Task(title="mine")], (
        "the store answered with something it could only have re-read")

    store.add(Task(title="second"))

    assert until(lambda: keys_on_disk(task_file) == ["1", "2"]), (
        "the dump never replaced the hand-edited file")


# ---- the path the store itself keeps ----------------------------------------


def test_path_is_the_resolved_file_the_class_lands_on(open_store, store_dir,
                                                      task_file):
    store = open_store(Task, store_dir)

    assert type(store.path) is type(Path())
    assert store.path == task_file.resolve()


def test_path_of_a_relative_store_resolves_against_the_directory_it_opened_in(
        open_store, tmp_path, monkeypatch, file_of):
    """A later chdir cannot move the file underfoot."""
    monkeypatch.chdir(tmp_path)
    store = open_store(Task, "data")

    assert store.path == file_of(Task, tmp_path / "data").resolve()
