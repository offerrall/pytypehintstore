# Limits

- **One process.** [The lock](lock.md) enforces it. A lockfile nobody is
  holding is taken over on POSIX and respected on Windows, where it is deleted
  by hand.
- **Everything in memory.** The file is read once at startup and rewritten whole
  on every dump. Thousands of rows, not millions.
- **Ids are auto-incrementing ints, never recycled.** See [API](api.md).
- **No queries.** `all()` and filter it yourself.
- **The file is never re-read while the process runs.** Editing it under a
  running store changes nothing, and the next dump overwrites your edit.
- **`IsPassword` encrypts nothing.** The value is written in the clear, like any
  other string.
- **`FileHint` stores the path, not the file.** The core validates the
  extension, which the text of the value settles by itself, and asks the
  filesystem nothing — so a row whose file was moved, deleted or grown comes
  back exactly as it was stored. Whether the file is still there is a question
  for whoever opens it.
- **`int | float` is ambiguous in a hand-edited file.** A bare `3` loads as an
  `int`; write `3.0` if you meant a float.
- **A date is `YYYY-MM-DD` and a time is `HH:MM[:SS]`, and no other spelling
  loads.** `date.fromisoformat` accepts far more than that, such as `"20260101"`
  or `"2026-W01-1"`; a file holding one of those is refused at load with the
  core's own message rather than read and rewritten behind your back. Write the
  canonical form.
- **A wrong Python type whose text is valid transport is rewritten, not
  refused.** With a field `due: date`, `Task(due="2026-08-01")` is accepted and
  the row ends up holding `date(2026, 8, 1)`.
- **`get()` and `all()` hand back the store's own rows, not copies.** Mutating
  what you get reaches inside, unvalidated. Build a new instance and `put` it.
- **The debounce has no ceiling.** Writes arriving faster than `debounce` keep
  pushing the dump back for as long as they keep arriving.
- **A failed dump is retried silently.** Nothing logs it; it surfaces at
  `close()`, or as a traceback from the exit hook. The library imposes no logger.
- **Upgrading `pytypehint` or Python can move the fingerprints.** A test pins
  them, and a move is declared as a breaking change, never absorbed — an
  absorbed one would silently rename every database.
- **A row the file cannot carry is refused at `add`.** See
  [the guarantee](guarantee.md).
- **Duplicate row ids in a hand-edited file: the last one wins.** `json.loads`
  drops the earlier ones before the store sees there were two, and there is no
  defence short of parsing the file ourselves.
- **Around 120 levels of nesting on Python 3.11, around 245 on 3.12 and later.** Deeper
  than that, opening the store raises `RecursionError` — loudly, and before the
  lockfile is taken, so nothing is left behind. The gap between the two is the
  interpreter's, not the store's: before 3.12 every comprehension on the way
  down takes a stack frame of its own. The core itself compiles to about 247 on
  either.
- **Rows are rebuilt one by one on load.** As an order of magnitude, 5 000 rows
  of an eight-field dataclass take about half a second to reopen and 1.7 MB on
  disk; adding them costs about a tenth of a millisecond each.
