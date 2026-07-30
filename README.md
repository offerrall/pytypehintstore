# pytypehintstore

Rows of one validated dataclass, in memory, shadowed by a JSON file you can open
and edit. **The class is the database.**

The rows live in a dict and every read answers from there; a writer thread
carries them to a file named for the class and the fingerprint of its compiled
schema — `Task.a50534e4.json`. Change one character of the contract, a type, a
limit, a default, a `Label`, and it is a different database: a new file, with
the old one left beside it, whole and readable.

That is the whole of it, and it is what buys the guarantee below: **if the file
opens, every row in it is valid against this exact schema.**

## Install

Not on PyPI yet. From a clone of the repository:

```bash
pip install -e .
```

That brings [`pytypehint`](https://pypi.org/project/pytypehint/) with it, the
only dependency. Stdlib otherwise. Python 3.11+, `py.typed` included.

## Quick start

```python
from dataclasses import dataclass
from enum import Enum
from typing import Annotated

from pytypehint import Max, Min
from pytypehintstore import store_of


class Priority(Enum):
    LOW = "low"
    HIGH = "high"


@dataclass
class Task:
    title: Annotated[str, Min(1), Max(80)]
    priority: Priority = Priority.LOW
    done: bool = False


tasks = store_of(Task, "data")
tasks.path.name                        # 'Task.a50534e4.json'

first = tasks.add(Task(title="Buy milk", priority=Priority.HIGH))   # 1
second = tasks.add(Task(title="Write the store"))                   # 2

tasks.get(first)                       # Task(title='Buy milk', priority=<Priority.HIGH: 'high'>, done=False)
tasks.put(second, Task(title="Write the store", done=True))
tasks.remove(first)

len(tasks)                             # 1
second in tasks                        # True
tasks.all()                            # [(2, Task(title='Write the store', ...))]

tasks.close()                          # the file is on disk when this returns
```

Reopen it and the rows are there with the same ids. Ids are spent, never
recycled, so the next `add` gives `3`.

## The guarantee

If a store opens, every row it holds was rebuilt through the schema you are
holding right now. There is no such thing as a row from an older version: those
live in an older file, under an older fingerprint.

`add` and `put` accept a row by making the round trip it will make anyway — the
instance is encoded to its transport form and rebuilt through the schema — so
what the store keeps always came out of the constructor and is known to survive
the file.

The store has no second validation system. A row that does not validate fails
with the core's own error, word for word:

```python
tasks.add(Task(title=""))
# SchemaValueError: title: too short: 0 chars, minimum 1
```

That is the same line `struct_of(Task).build({"title": ""})` produces. Only one
message belongs to the store itself, because only the store can be asked this:

```python
tasks.add("not a task")
# SchemaTypeError: expected Task, got str
```

The instance the store keeps is not the one you passed; mutating yours
afterwards changes nothing inside.

One question the store asks on its own behalf, after the core has spoken: a row
has to fit in a UTF-8 file. A lone surrogate is a `str` Python allows and no
encoder can write, so it is refused at `add` rather than discovered at `close`:

```python
tasks.add(Task(title="\ud800"))
# SchemaValueError: cannot be written as UTF-8: 'utf-8' codec can't encode …
```

**The property, stated plainly:** if the core compiles your schema, the store
persists it without loss or fails loudly — at `add`, or at `store_of` before a
lockfile is taken. A stress campaign of 1150 generated schemas and four
adversarial fronts found one exception, and it is loud rather than silent: a
[union of lists differing only in a constraint](#known-limits).

## The identity

The file is `{ClassName}.{fingerprint}.json`. The fingerprint is eight hex of
the sha256 of the compiled schema, rendered as deterministic text.

**Everything in the schema counts** — types, limits, defaults, the order of the
fields, and the notation atoms (`Label`, `Description`, `Placeholder`, …) that
change nothing a file can carry. No exceptions and no small print: a contract
without special cases fits in one sentence and is tested in three lines.

Change the class and you get a new, empty database. The old file stays where it
was, untouched and readable, named for the schema that wrote it. Moving rows
across is a script over readable JSON — a minute of work for you or an agent —
and it is deliberately outside this library, because a migration that runs
automatically is a migration nobody read.

## The file

The file carries the transport form, the same shapes an HTTP client would send:

```json
{
  "v": 1,
  "next_id": 3,
  "rows": {
    "1": {
      "title": "Buy milk",
      "due": "2026-08-01",
      "priority": "HIGH",
      "note": { "body": "oat" }
    }
  }
}
```

A date or a time is ISO text, an enum is its **member name**, a nested dataclass
is an object, a list is a list. Never a `repr` of a Python value.

Edit it, and the next load either accepts your edit or says exactly what is
wrong with it. Every one of these is a `StoreLoadError` raised by `store_of`,
before a single row is served:

| What is broken | What you get |
|---|---|
| The file does not exist | An empty store — not an error |
| Not UTF-8 | `…json: not valid UTF-8: …` |
| Not JSON | `…json: not valid JSON: Expecting property name … (char 1)` |
| The root is not an object | `…json: expected an object, got list` |
| `v` is not the format version | `…json: unknown format version 2, expected 1` |
| `rows` is not an object | `…json: rows must be an object, got list` |
| A row id is not an integer | `…json: row id '--5' is not an integer` |
| Two ids are the same integer (`"1"`, `"01"`) | `…json: row id '01' is already taken by another key` |
| A row does not validate | `…json: row 1: note: body: too short: 0 chars, minimum 1` |

The last one is the core speaking, prefixed with the row it happened in. A row
missing a field reports exactly that — `row 1: missing key(s): title` — and
nothing more: there is no advice to add a default, because that would fork the
database rather than fix the row.

`next_id` is a hint, not an authority: if the file names one that would collide
with a row it also carries, the highest id present wins.

### The `$type`/`$value` wrapper

Most values need no help: the JSON type says which option of a union they belong
to. The wrapper appears only when two options land on the same JSON type, and it
survives decoding only where the core still needs it.

```python
when: str | date        # {"$type": "date", "$value": "2026-08-01"}
terms: list[str] | list[int]   # {"$type": "list[int]", "$value": [1, 2]}
```

Both are text and both are lists, so in each case the option gets named. On the
way back, the first wrapper is consumed — the core tells `str` and `date` apart
by Python type — and the second is kept, because to the core both options are
`list` and only the name separates them. Dataclasses in a union name the variant
inside the object instead: `{"$type": "Square", "side": 2}`.

Anything the codec cannot read as one single thing travels intact, so the error
you see is the core's, with its path and its words.

## Writing

A write mutates the dict at once and marks the store unwritten. The writer
thread dumps it `debounce` seconds later, and every write pushes that deadline
back, so a burst costs one dump.

The dump is atomic: the text goes to a `.part` file, is flushed and `fsync`ed,
then replaces the real file with `os.replace`. The file goes from one whole JSON
to the next, never a half of either. A failed dump leaves nothing of itself
behind, keeps the state unwritten, and is tried again — it never kills the
writer and never passes for a success. Before each replace the previous file is
copied aside with a nanosecond stamp, and copies beyond `keep` are pruned. Each
database prunes its own copies: the fingerprint is in the name.

`close()` stops the writer, dumps what is pending, and releases the lock. When it
returns the file is on disk — or it raised, and it raised at every caller that
was closing the same store. The lock is released either way.

If you never call `close()`, an `atexit` hook does it on a normal interpreter
exit. It does not run on `os._exit`, on a hard kill, or on a power cut; what is
on disk then is the last dump that landed, whole.

## The lock

Opening a store writes `Task.a50534e4.json.lock` beside the file, holding the
owner's PID, and keeps it open while the store is open.

A second store on the same file — in this process or any other — fails at
startup:

```
…Task.a50534e4.json is owned by process 18220. A store belongs to one process;
you are probably running several workers. Run a single worker, or reach for a
database server — this is not one.
```

A lockfile left by a crash names a PID that no longer runs, and the next process
takes it over; so does one whose contents say nothing. Because the descriptor
stays open, two processes racing for the same orphan cannot both end up owning
it. Two different classes in one directory are two files and two locks, and
never meet.

## API

```python
store = store_of(cls, directory, *, debounce=2.0, keep=5)
```

`cls` is the dataclass whose rows this store holds. `directory` is where the file
goes; it is created if missing, and a file sitting in its place is an error.
`debounce` is how long a burst of writes may keep pushing the dump back. `keep`
is how many rotated copies survive.

| Method | What it does |
|---|---|
| `add(obj) -> int` | Validates and stores a row; returns its new id. A rejected row does not spend an id. |
| `get(row_id)` | The row under that id. `KeyError` if there is none. |
| `put(row_id, obj)` | Validates and replaces the row under an existing id. `KeyError` if there is none. |
| `remove(row_id)` | Takes the row out. `KeyError` if there is none. The id is not reused. |
| `all() -> list[tuple[int, obj]]` | Every row, sorted by id. |
| `len(store)` | How many rows. |
| `row_id in store` | Membership by id. |
| `store.path` | The `Path` of the file this class landed on. |
| `close()` | Dumps what is pending, stops the writer, releases the lock. Idempotent. |

| Exception | When |
|---|---|
| `StoreLockedError` | Another live process — or this one — already owns the file |
| `StoreLoadError` | The file exists and could not be read back into rows |
| `StoreError` | An operation on a closed store, or a directory that is not one |

Validation failures are not among them: `SchemaTypeError` and `SchemaValueError`
travel out of `add` and `put` exactly as the core raised them.

## Known limits

- **One process.** The lock enforces it.
- **Everything in memory.** The file is read once at startup and rewritten whole
  on every dump. Thousands of rows, not millions.
- **Ids are auto-incrementing ints, never recycled.**
- **No queries.** `all()` and filter it yourself.
- **The file is never re-read while the process runs.** Editing it under a
  running store changes nothing, and the next dump overwrites your edit.
- **`IsPassword` encrypts nothing.** The value is written in the clear, like any
  other string.
- **`IsPathFile` stores the path, not the file.** The core revalidates it on
  every load, so a row whose file was moved, deleted or grown past its maximum
  stops the load.
- **`int | float` is ambiguous in a hand-edited file.** A bare `3` loads as an
  `int`; write `3.0` if you meant a float.
- **A wrong Python type whose text is valid transport is rewritten, not
  refused.** With `due: date`, `Task(due="2026-08-01")` is accepted and the row
  ends up holding `date(2026, 8, 1)`.
- **`get()` and `all()` hand back the store's own rows, not copies.** Mutating
  what you get reaches inside, unvalidated. Build a new instance and `put` it.
- **The debounce has no ceiling.** Writes arriving faster than `debounce` keep
  pushing the dump back for as long as they keep arriving.
- **A failed dump is retried silently.** Nothing logs it; it surfaces at
  `close()`, or as a traceback from the exit hook. The library imposes no logger.
- **Upgrading `pytypehint` or Python can move the fingerprints.** The pinned test
  in `tests/test_identity.py` catches it. It is declared as a breaking change,
  never absorbed — an absorbed one would silently rename every database.
- **A union whose options share a transport name is refused at open.** A wrapped
  option is named by its bare class name, so an enum class called `date`
  competes with `date` itself for one `$type` and the file could not say which
  option a row belongs to. The core compiles such a union; the store refuses it,
  naming the field and the shared name. The check is for real collisions, not
  for forbidden names: the same enum in a field with no `date` beside it stores
  its members like any other.
- **A union of lists that differ only in a constraint can name the wrong
  branch.** `Annotated[list[str], Min(1)] | list[int]` with `[]`: the core routes
  by the whole shape, the codec by the item types alone, so `add` refuses a row
  the core had just called valid. Loud, never silent, and rare — 4 of 500
  generated schemas. Fixing it needs the core's own value router, which is not
  public.
- **A row the file cannot carry is refused at `add`.** A lone surrogate is a
  `str` Python allows and no encoder can write.
- **Duplicate row ids in a hand-edited file: the last one wins.** `json.loads`
  drops the earlier ones before the store sees there were two, and there is no
  defence short of parsing the file ourselves.
- **About 160 levels of nesting.** Deeper than that, opening the store raises
  `RecursionError` — loudly, and before the lockfile is taken, so nothing is
  left behind. The core itself compiles up to about 192.
- **Rows are rebuilt one by one on load.** 5 000 rows of an eight-field
  dataclass take about half a second to reopen and 1.7 MB on disk; adding them
  costs about 0.12 ms each.

## Ecosystem

* **[`pytypehint`](https://github.com/offerrall/pytypehint)** compiles standard
  Python type hints into strict, inspectable schemas. It is the validation this
  store defers to, and the schema it fingerprints.
* **[`pytypehintweb`](https://github.com/offerrall/pytypehintweb)** compiles the
  same schemas into a browser runtime for forms — the transport this store
  writes to disk is the shape it speaks.
* **[`FuncToWeb`](https://github.com/offerrall/FuncToWeb)** exposes typed Python
  functions through generated web interfaces. A store is the small persistence
  such an app usually wants, without adding a database to it.
