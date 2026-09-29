# pytypehintstore

Rows of one validated dataclass, in memory, shadowed by a JSON file you can open
and edit. **The class is the database.**

The file is named for the class and the fingerprint of its compiled schema, so
**if the file opens, every row in it is valid against this exact schema.**
Change the contract and it is a new database; the old file stays beside it,
whole and readable.

```python
from dataclasses import dataclass
from typing import Annotated
from pytypehint import Max, Min
from pytypehintstore import store_of

@dataclass
class Task:
    title: Annotated[str, Min(1), Max(80)]
    done: bool = False

tasks = store_of(Task, "data")              # data/Task.<fingerprint>.json
row_id = tasks.add(Task(title="Buy milk"))  # 1
tasks.get(row_id)                           # Task(title='Buy milk', done=False)
tasks.close()
```

The full documentation is at https://offerrall.github.io/pytypehintstore/.

## Documentation

- [Overview](docs/overview.md): how the store works and a full walk-through of its operations.
- [The guarantee](docs/guarantee.md): every row a store opens was rebuilt through your schema.
- [The identity](docs/identity.md): the file name, and why any change to the class is a new database.
- [The file](docs/file.md): the JSON on disk, editing it by hand, and every load error.
- [Writing](docs/writing.md): debounced, atomic dumps, rotated copies and `close()`.
- [The lock](docs/lock.md): one process per store, and how orphaned lockfiles are taken over.
- [API](docs/api.md): `store_of`, its methods and its exceptions.
- [Limits](docs/limits.md): what the store does not do, stated plainly.
