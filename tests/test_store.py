"""The dict a store is: ids, rows, and what the seven operations promise.

Nothing here waits for the file. Every store opens with a debounce long enough
that the writer never wakes inside a test, so what these tests read is the
memory the caller just wrote and nothing else — the shadow on disk is another
area's contract.
"""

import threading
from dataclasses import dataclass
from datetime import date

import pytest
from pytypehint import SchemaTypeError, SchemaValueError, struct_of

from pytypehintstore import StoreError
from shared import Bag, Tag, Task

# Longer than any test here takes, so no dump ever lands in the middle of one.
QUIET = 30.0

# A thread that has not finished by now is wedged, not slow.
WITHIN = 10.0


@dataclass
class Delivery:
    """A row with a field whose transport form is wider than its Python type."""

    label: str
    due: date


@pytest.fixture
def store(open_store, store_dir):
    """A Task store at this test's own path, with a writer that never wakes."""
    return open_store(Task, store_dir, debounce=QUIET)


def refused_by_the_core(cls, data):
    """What `Struct.build` says about `data` — the oracle for a store's error.

    `add` and `put` let the core's own exception travel out untouched, so the
    words belong to the core and asking it in the test is the only comparison
    that cannot go stale the day it rewords them.
    """
    with pytest.raises((SchemaTypeError, SchemaValueError)) as info:
        struct_of(cls).build(data)

    return info.value


def _add(store, obj):
    store.add(obj)


def _put(store, obj):
    # The lowest id is a row that is really there, so a failure here can only be
    # the value's and never a missing key.
    store.put(min(row_id for row_id, _ in store.all()), obj)


WRITES = pytest.mark.parametrize("write", [_add, _put], ids=["add", "put"])


# ---- ids -------------------------------------------------------------------


@pytest.fixture
def store_file(file_of, store_dir):
    """The file this module's own class lands on."""
    return file_of(Task, store_dir)


def test_add_hands_out_the_ids_in_order_from_one(store):
    ids = [store.add(Task(title=f"row {n}")) for n in range(3)]

    assert ids == [1, 2, 3]
    assert all(type(row_id) is int for row_id in ids)


def test_the_id_add_returns_is_the_id_the_row_answers_to(store):
    task = Task(title="Buy milk", priority="high")

    row_id = store.add(task)

    # Read back before anything else happens: the answer must not depend on a
    # later write settling the store into agreement with itself.
    assert row_id in store
    assert store.get(row_id) == task
    assert store.all() == [(row_id, task)]


def test_an_id_a_remove_freed_is_never_handed_out_again(store):
    store.add(Task(title="first"))
    middle = store.add(Task(title="second"))
    store.add(Task(title="third"))

    store.remove(middle)

    assert store.add(Task(title="fourth")) == 4
    assert middle not in store


def test_a_rejected_add_does_not_spend_an_id(store):
    broken = Task(title="Buy milk")
    broken.priority = "urgent"

    with pytest.raises(SchemaValueError):
        store.add(broken)

    assert store.add(Task(title="Buy milk")) == 1


def test_threads_never_share_an_id(open_store, store_dir):
    store = open_store(Task, store_dir, debounce=QUIET)

    failures = []
    handed = []

    def work(worker):
        given = []
        mine = []

        try:
            for n in range(25):
                row_id = store.add(Task(title=f"w{worker} n{n}"))
                given.append(row_id)
                mine.append(row_id)
                store.put(row_id, Task(title=f"w{worker} n{n}", done=True))

                # Only ever its own rows, so a KeyError would be a real one.
                if n % 5 == 4:
                    store.remove(mine.pop(0))
        except Exception as error:
            failures.append(error)

        handed.append(given)

    workers = [threading.Thread(target=work, args=(n,), name=f"worker {n}")
               for n in range(8)]

    for worker in workers:
        worker.start()

    for worker in workers:
        worker.join(timeout=WITHIN)
        assert not worker.is_alive(), f"{worker.name} never finished"

    assert failures == []

    ids = [row_id for given in handed for row_id in given]

    assert len(ids) == 200
    assert len(set(ids)) == 200, "an id was handed out twice"
    assert len(store) == 160


# ---- the seven operations --------------------------------------------------


def test_get_answers_with_the_row_kept_under_the_id(store):
    first = store.add(Task(title="Buy milk", priority="high"))
    second = store.add(Task(title="Write the store", done=True))

    assert store.get(first) == Task(title="Buy milk", priority="high")
    assert store.get(second) == Task(title="Write the store", done=True)


def test_put_replaces_the_whole_row_and_leaves_the_id_set_alone(store):
    first = store.add(Task(title="Buy milk", priority="high"))
    second = store.add(Task(title="Write the store", done=True))
    before = [row_id for row_id, _ in store.all()]

    store.put(first, Task(title="Buy oat milk"))

    # Every field of the row is the new one, not a merge with what was there.
    assert store.get(first) == Task(title="Buy oat milk", priority="normal", done=False)
    assert [row_id for row_id, _ in store.all()] == before
    assert store.get(second) == Task(title="Write the store", done=True)


def test_remove_takes_its_row_out_and_leaves_the_others_where_they_are(store):
    first = store.add(Task(title="first"))
    second = store.add(Task(title="second"))

    store.remove(first)

    assert first not in store
    assert store.all() == [(second, Task(title="second"))]


def test_all_returns_id_and_row_pairs_sorted_by_id(store):
    ids = [store.add(Task(title=f"row {n}")) for n in range(3)]

    rows = store.all()

    assert rows == [(ids[0], Task(title="row 0")), (ids[1], Task(title="row 1")),
                    (ids[2], Task(title="row 2"))]
    assert all(type(row) is tuple and len(row) == 2 for row in rows)


def test_all_stays_sorted_by_id_across_a_remove_in_the_middle(store):
    ids = [store.add(Task(title=f"row {n}")) for n in range(4)]

    store.remove(ids[1])

    rows = store.all()

    assert rows == [(1, Task(title="row 0")), (3, Task(title="row 2")),
                    (4, Task(title="row 3"))]
    assert [row_id for row_id, _ in rows] == sorted(row_id for row_id, _ in rows)


def test_all_hands_out_a_fresh_list_the_caller_cannot_write_back_through(store):
    row_id = store.add(Task(title="first"))

    rows = store.all()
    rows.clear()

    assert store.all() == [(row_id, Task(title="first"))]


def test_len_counts_the_rows(store):
    assert len(store) == 0

    first = store.add(Task(title="first"))
    store.add(Task(title="second"))

    assert len(store) == 2

    store.remove(first)

    assert len(store) == 1


def test_in_is_membership_by_id_and_by_nothing_else(store):
    first = store.add(Task(title="first"))

    assert first in store
    assert 2 not in store
    # The id written as text is not the id, and neither is the row itself.
    assert "1" not in store

    store.remove(first)

    assert first not in store


# ---- ids that are not there ------------------------------------------------


def test_get_on_an_absent_id_raises_key_error_carrying_the_id(store):
    with pytest.raises(KeyError) as info:
        store.get(99)

    assert info.value.args == (99,)


def test_put_on_an_absent_id_raises_key_error_carrying_the_id(store):
    store.add(Task(title="first"))

    with pytest.raises(KeyError) as info:
        store.put(99, Task(title="nowhere"))

    assert info.value.args == (99,)
    assert store.all() == [(1, Task(title="first"))]


def test_remove_on_an_absent_id_raises_key_error_carrying_the_id(store):
    with pytest.raises(KeyError) as info:
        store.remove(99)

    assert info.value.args == (99,)


# ---- the row the store keeps -----------------------------------------------


def test_the_row_add_stores_is_equal_to_the_instance_but_not_the_same_object(store):
    task = Task(title="Buy milk")

    stored = store.get(store.add(task))

    assert stored == task
    assert stored is not task


def test_mutating_the_instance_after_add_leaves_the_stored_row_alone(store):
    task = Task(title="Buy milk", priority="high")

    row_id = store.add(task)
    task.title = "Buy oat milk"
    task.done = True

    assert store.get(row_id) == Task(title="Buy milk", priority="high", done=False)


def test_mutating_the_instance_after_put_leaves_the_stored_row_alone(store):
    row_id = store.add(Task(title="first"))
    task = Task(title="Buy milk", priority="high")

    store.put(row_id, task)
    task.title = "Buy oat milk"

    assert store.get(row_id) == Task(title="Buy milk", priority="high")


def test_a_nested_row_is_rebuilt_all_the_way_down(open_store, store_dir):
    store = open_store(Bag, store_dir, debounce=QUIET)
    tag = Tag(name="a")
    bag = Bag(tags=[tag], note="first")

    row_id = store.add(bag)
    # The list and the dataclass inside it must both be the store's own, or a
    # caller still holding the outer instance could reach in and edit a row.
    bag.tags.append(Tag(name="b"))
    tag.name = "edited"
    bag.note = "edited"

    stored = store.get(row_id)

    assert stored == Bag(tags=[Tag(name="a")], note="first")
    assert stored.tags is not bag.tags
    assert stored.tags[0] is not tag


# ---- what a read hands back ------------------------------------------------


def test_get_hands_back_the_stores_own_instance_and_not_a_copy(store):
    """A read is a dict lookup and stays one.

    Copying on the way out would make every read cost a rebuild, and the store
    already promises the row came out of the constructor. `get`'s own docstring
    states this; the test is here so it stays true by accident of nobody's.
    """
    row_id = store.add(Task(title="Buy milk"))

    assert store.get(row_id) is store.get(row_id)
    assert store.all()[0][1] is store.get(row_id)


def test_a_row_mutated_through_get_is_seen_by_the_next_read(store):
    """The other half of handing back the store's own instance.

    Reaching into a row that way skips the schema entirely — the value is never
    rebuilt and the row is not marked unwritten — so a store can be made to hold
    something `add` would have refused. `put` is the way in that validates.
    """
    row_id = store.add(Task(title="Buy milk"))

    stored = store.get(row_id)
    stored.priority = "urgent"

    assert store.get(row_id).priority == "urgent"
    assert store.all() == [(row_id, stored)]


def test_add_stores_transport_text_where_a_date_belongs_instead_of_refusing_it(
        open_store, store_dir):
    """A field of the wrong Python type whose value is valid transport is taken.

    A row is accepted by the round trip it will make anyway — encoded to the
    transport, decoded, rebuilt — and the decode reads ISO text standing under a
    `date` as the date it names. The schema is handed a real date and has
    nothing left to refuse, so the round trip rewrites the field rather than
    rejecting it. Catching this would take a second validator run over the
    caller's own instance, which is the one thing the design rules out.
    """
    store = open_store(Delivery, store_dir, debounce=QUIET)

    row_id = store.add(Delivery(label="parcel", due="2026-08-01"))

    stored = store.get(row_id)

    assert type(stored.due) is date
    assert stored == Delivery(label="parcel", due=date(2026, 8, 1))


# ---- what a row has to be --------------------------------------------------


@WRITES
def test_an_object_of_another_class_is_refused_in_the_stores_own_words(store, write):
    store.add(Task(title="already here"))

    with pytest.raises(SchemaTypeError) as info:
        write(store, Tag(name="not a task"))

    assert str(info.value) == "expected Task, got Tag"
    assert store.all() == [(1, Task(title="already here"))]


@WRITES
def test_a_mapping_is_refused_like_any_other_class(store, write):
    store.add(Task(title="already here"))

    with pytest.raises(SchemaTypeError) as info:
        write(store, {"title": "Buy milk"})

    assert str(info.value) == "expected Task, got dict"
    assert store.all() == [(1, Task(title="already here"))]


@WRITES
def test_a_row_mutated_outside_its_choices_is_refused_in_the_cores_words(store, write):
    store.add(Task(title="already here"))
    task = Task(title="Buy milk")
    task.priority = "urgent"

    with pytest.raises((SchemaTypeError, SchemaValueError)) as info:
        write(store, task)

    core = refused_by_the_core(
        Task, {"title": "Buy milk", "priority": "urgent", "done": False})

    assert type(info.value) is type(core)
    assert str(info.value) == str(core)
    assert store.all() == [(1, Task(title="already here"))]


def test_a_field_mutated_to_the_wrong_type_is_refused_in_the_cores_words(store):
    task = Task(title="Buy milk")
    task.done = "yes"

    with pytest.raises((SchemaTypeError, SchemaValueError)) as info:
        store.add(task)

    core = refused_by_the_core(
        Task, {"title": "Buy milk", "priority": "normal", "done": "yes"})

    assert type(info.value) is type(core)
    assert str(info.value) == str(core)
    assert len(store) == 0


def test_a_nested_row_mutated_by_hand_is_refused_with_the_cores_whole_path(
        open_store, store_dir):
    store = open_store(Bag, store_dir, debounce=QUIET)
    bag = Bag(tags=[Tag(name="a"), Tag(name="b")])
    bag.tags[1].name = ""

    with pytest.raises((SchemaTypeError, SchemaValueError)) as info:
        store.add(bag)

    core = refused_by_the_core(
        Bag, {"tags": [{"name": "a"}, {"name": ""}], "note": ""})

    assert type(info.value) is type(core)
    assert str(info.value) == str(core)
    assert len(store) == 0


# ---- once it is closed -----------------------------------------------------


def test_no_operation_answers_after_close(store, store_dir, store_file):
    """A closed store answers nothing at all, reads included.

    Reporting a count or a membership from the rows it still holds in memory
    would make it half alive, and a caller reading that would believe the store
    was open.
    """
    row_id = store.add(Task(title="first"))
    store.close()

    operations = [
        lambda: store.add(Task(title="late")),
        lambda: store.get(row_id),
        lambda: store.put(row_id, Task(title="late")),
        lambda: store.remove(row_id),
        store.all,
        lambda: len(store),
        lambda: row_id in store,
    ]

    for operation in operations:
        with pytest.raises(StoreError) as info:
            operation()

        assert str(info.value) == f"{store_file}: store is closed"
