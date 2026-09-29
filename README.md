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

- [Overview](https://offerrall.github.io/pytypehintstore/): how the store works and a full walk-through of its operations.
- [The guarantee](https://offerrall.github.io/pytypehintstore/guarantee/): every row a store opens was rebuilt through your schema.
- [The identity](https://offerrall.github.io/pytypehintstore/identity/): the file name, and why any change to the class is a new database.
- [The file](https://offerrall.github.io/pytypehintstore/file/): the JSON on disk, editing it by hand, and every load error.
- [Writing](https://offerrall.github.io/pytypehintstore/writing/): debounced, atomic dumps, rotated copies and `close()`.
- [The lock](https://offerrall.github.io/pytypehintstore/lock/): one process per store, and how orphaned lockfiles are taken over.
- [API](https://offerrall.github.io/pytypehintstore/api/): `store_of`, its methods and its exceptions.
- [Limits](https://offerrall.github.io/pytypehintstore/limits/): what the store does not do, stated plainly.
