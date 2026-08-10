# Changelog

## [1.0.0] - 2026-08-10

The store keeps what is genuinely its own — the disk, the lock, the identity of
a schema, the life of a process — and stops having opinions about the contract.
Every reading of a row was a second dialect maintained beside the core's, and a
dialect that agrees today is a dialect that diverges later. `pytypehint` 1.0.0
publishes the operations that make the second one unnecessary, so this release
deletes it.

- The dependency is `pytypehint==1.0.0`, pinned exact rather than floored. The
  codec imports `pytypehint.validation.value_branch`, which is the core's own
  router and carries no public promise; one author owns both packages, and a
  pin plus `test_codec_router_contract` is what turns a move upstream into a
  failing test rather than a file naming the wrong option.
- `codec.decode` is gone, and with it `_decode_options`, `_decode_dict`,
  `_decode_list`, `_decode_str`, `_convert`, `_core_type` and `_field`. A row is
  read back with `schema.decode`, which the core published for exactly this and
  does better: the spellings it accepts for a date and a time are fixed and
  disjoint rather than delegated to `fromisoformat`, a malformed wrapper is left
  whole instead of half-read, and a non-string key is refused before anything
  probes it. Before the deletion the two were compared over the vocabulary, the
  wrappers, the malformed wrappers and every enum shape including a `StrEnum`
  mixin and an alias; they agreed in every case.
- Writing still belongs here — the core describes and validates, and turning a
  live instance into the tree that goes on disk is the file's business — but the
  router underneath it does not. `codec._branch_of` and `codec._accepts` are
  replaced by `value_branch`, so the option the file names is the option
  validation would have chosen, by construction rather than by coincidence.
- `store._ambiguous` and `store._shared_name` are gone. They existed because the
  old core compiled a union whose options share a transport name — an enum class
  called `date` beside a `date` — and the store refused it at the door to keep a
  row from coming back as the wrong thing. The core now applies the identity
  rule at compilation, inside lists included, so the schema never exists. The
  refusal a caller sees is the core's own, with the core's coordinates, and the
  store adds nothing to it.

Three limits declared in the README are gone with them:

- **A union of lists differing only in a constraint.**
  `Annotated[list[str], Min(1)] | list[int]` with `[]` used to be written under
  the branch that rejects it, so `add` refused a row the core had just called
  valid. The core's router reads the constraints, because it calls `_check`.
- **A union whose options share a transport name.** Refused by the core at
  compilation now, rather than by the store at open.
- **A date in another ISO spelling.** `"20260101"` and `"2026-W01-1"` used to
  load, because `date.fromisoformat` accepts the whole of ISO 8601, and the next
  dump rewrote them in the one form the store emits — turning a week date into
  the Monday it names, in another year, without a word. The core pinned the
  canonical spellings, so these are refused out loud instead.

Breaking, and the reason it is declared rather than absorbed:

- **Fingerprints move, so every store gets a new file.** The core renamed the
  `Str` field `is_path_file` to `file_hint`, and a field *name* is part of the
  text a schema is rendered as. This moves the fingerprint of every schema
  holding a `str` at any depth — a field, a list item, a union option, a nested
  dataclass — which in practice is nearly every schema. A schema with no `str`
  anywhere keeps its fingerprint and its file.

  What moved is the *rendering* of the contract, not the contract: the same
  dataclass describes the same rows, and the old file is valid against the new
  schema in every byte. So this is the one case where the usual answer — a
  changed contract is a separate database, never a migration — is answering a
  question nobody asked. **Rename the file** from
  `Class.<old>.json` to `Class.<new>.json` and it loads: ids, `next_id` and
  every row survive, because the transport form is unchanged from 0.0.4 (verified
  field by field across the vocabulary and all three wrappers). Get the new name
  by opening a store on an empty directory and reading `store.path`.

  Two things to know before renaming. A file holding a date or time in a
  non-canonical ISO spelling now fails the load rather than being read and
  rewritten — loudly, naming the row. And a row whose `FileHint` file has since
  moved or grown now loads where it used to be refused. If you would rather
  revalidate than rename, re-import through `add()` from the old JSON; the old
  file is never read and never deleted either way.
- **`FileHint` replaces `IsPathFile`, and the core stopped checking the file.**
  A row whose file was moved, deleted or grown past its maximum no longer stops
  a load: the extension is validated because the text settles it, and existence
  and size are questions for whoever opens the file. The one declared exception
  to "a row that went in comes back out" is therefore gone.

What 1.0.0 means here: the documented surface is the real one, and the store
interprets the contract in zero places. What it adds is a file, a lock, an
identity and a lifecycle — and it adds nothing else.

## [0.0.4]

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
- One difference is left, and declared: with a lockfile nobody is holding, POSIX
  trusts the `flock` and takes it over — so a recycled pid cannot lock a path
  out there — while Windows trusts the pid written in it and refuses. Two live
  stores behave identically on both.

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
