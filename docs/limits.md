# Known limits

- **One process.** The lock enforces it. Two live stores behave the same
  everywhere; what differs is a lockfile nobody is holding. On POSIX the `flock`
  is the evidence, so a file naming a live process that is not holding it — a
  crash plus a recycled pid — is taken over, and a recycled pid cannot lock a
  path out. On Windows the pid is the evidence, so that file is respected and
  the way out is to delete it by hand. And deleting it *while* a store holds it
  is refused by Windows but allowed by POSIX, where the exclusion is then lost
  until both processes end: delete a lockfile only when nothing is holding it.
- **Everything in memory.** The file is read once at startup and rewritten whole
  on every dump. Thousands of rows, not millions.
- **Ids are auto-incrementing ints, never recycled.**
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
  loads.** `date.fromisoformat` accepts far more than that, so `"20260101"` and
  `"2026-W01-1"` used to load and then be rewritten in the canonical form on the
  next dump — a week date quietly becoming the Monday it names, in another year.
  The core pinned the spellings in 1.0.0, so a file holding one of the others is
  now refused at load with the core's own message rather than read and rewritten
  behind your back. Write the canonical form.
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
- **A row the file cannot carry is refused at `add`.** A lone surrogate is a
  `str` Python allows and no encoder can write.
- **Duplicate row ids in a hand-edited file: the last one wins.** `json.loads`
  drops the earlier ones before the store sees there were two, and there is no
  defence short of parsing the file ourselves.
- **Around 120 levels of nesting on 3.11, around 245 on 3.12 and later.** Deeper
  than that, opening the store raises `RecursionError` — loudly, and before the
  lockfile is taken, so nothing is left behind. The gap between the two is the
  interpreter's, not the store's: before 3.12 every comprehension on the way
  down takes a stack frame of its own. The core itself compiles to about 247 on
  either.
- **Rows are rebuilt one by one on load.** 5 000 rows of an eight-field
  dataclass take about half a second to reopen and 1.7 MB on disk; adding them
  costs about 0.12 ms each.
