# API

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
