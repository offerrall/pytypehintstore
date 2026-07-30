# Changelog

## [0.0.3]

One library on both platforms: the lock now holds the same contract on POSIX
that it held on Windows, and the checker agrees on both.

- The lock is settled by the operating system on POSIX too. It was an open
  handle on Windows — a file Windows will not unlink while it is open — and a
  bare `O_EXCL` everywhere else, which POSIX does not defend: two processes
  reaching for one orphaned lockfile could both unlink it and both create their
  own, ending as two live owners of one file. POSIX now takes an advisory
  `flock`, which the kernel drops when a process dies, and checks the inode
  afterwards in case the file was replaced between the open and the lock.
- An orphan needs no stealing on POSIX: what makes it an orphan is that its
  `flock` died with its owner. The behaviour a caller sees is unchanged — one
  owner, orphans reclaimed, the same message naming the live PID.
- `release` unlinks before closing on POSIX and after closing on Windows, each
  being the order that cannot hand a second owner the lock.
- `mypy` passes on both platforms. The Windows probe is declared under
  `sys.platform` rather than `os.name`, which is the form a checker reads as a
  platform guard, so `ctypes.WinDLL` is not looked for on Linux.
- CI runs the suite on `ubuntu-latest` and `windows-latest`, over 3.11, 3.12
  and 3.13. Half of the lock only exists on one of them.
- New test: two processes reaching for one orphan at the same instant leave a
  single owner. It is the property both halves exist to hold, and it runs on
  both.

## [0.0.2]

Out of a stress campaign: 1150 generated schemas, four adversarial fronts, and
the suite from 200 tests to 1583.

- Fixed, data loss: `fingerprint` walked a schema as a tree when it is a graph —
  the core compiles a class once and shares it between every field naming it —
  so a class the core compiled in a millisecond could take minutes to
  fingerprint, and `store_of` hung with no error and no output. Each dataclass
  is now written once and referred back to. Schemas with the same class in two
  fields change their fingerprint, and so their file name.
- Fixed, data loss: a lone surrogate was accepted by `add`, could never be
  written to a UTF-8 file, and froze every later dump — the failure surfacing
  only at `close()`, as an error from another library. Such a row is refused at
  `add` with `SchemaValueError: cannot be written as UTF-8`, after the core has
  had its say.
- Fixed, data loss: a union whose options share a transport name — an enum class
  called `date` beside `date` itself — routed values to the wrong branch, and a
  stored member came back as a plain string. The store now refuses the schema at
  open, naming the field and the shared name, before the lockfile is taken. Real
  collisions only: the same enum with nothing to collide with still works.
- A `$type`/`$value` wrapper is those two keys and nothing else. A key beside
  them, or a `$value` that is itself a wrapper, travels intact for the core to
  refuse instead of being read anyway and erased by the next dump.
- The format version is checked by type as well as by value, so a `1.0` or a
  `true` in `v` is no longer read as version 1.
- Packaging: classifiers, keywords and project URLs for PyPI.

## [0.0.1]

First release. Rows of one `pytypehint`-validated dataclass, kept in memory and
mirrored to a JSON file a person can open and edit.

- `store_of(cls, directory, *, debounce=2.0, keep=5)` opens the store of one
  dataclass on a file named for the class and the fingerprint of its schema.
  Reads answer from memory; writes mutate the dict at once and a writer thread
  dumps the whole state `debounce` seconds after the burst of writes stops.
- `add` and `put` accept a row by making the round trip it will make anyway:
  the instance is encoded to its transport form and rebuilt through the schema,
  so what the store holds always came out of the constructor and always
  survives the file. Validation failures are the core's own `SchemaTypeError`
  and `SchemaValueError`, untouched; the store adds no second validation
  system.
- The file carries the transport form — ISO text for a date or a time, the
  member name for an enum, an object for a nested dataclass — so it stays
  readable and editable. A row that cannot be read back names the file, the row
  and the core's error.
- Dumps are atomic: `.part`, `fsync`, `os.replace`. A dump that fails leaves
  nothing of itself behind, keeps the state unwritten and is retried; the
  writer thread never dies of it. The previous file is rotated aside with a
  nanosecond stamp and copies beyond `keep` are pruned.
- `close()` dumps what is pending, stops the writer and releases the lock. It is
  idempotent, and a failure on the way down reaches every caller that was
  closing the same store. An `atexit` hook closes a store the caller forgot, on
  a normal interpreter exit.
- One process owns a store. The lockfile holds the owner's PID and stays open
  for as long as the lock is held, so a second process is refused at startup
  rather than corrupting the file later, and two processes racing for the same
  orphaned lock cannot both end up owning it.
- Three exceptions, all subclasses of `StoreError`: `StoreLockedError`,
  `StoreLoadError`, and `StoreError` itself for an operation on a closed store.
