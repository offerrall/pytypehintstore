# pytypehintstore

[![PyPI version](https://img.shields.io/pypi/v/pytypehintstore.svg)](https://pypi.org/project/pytypehintstore/)
[![Python](https://img.shields.io/pypi/pyversions/pytypehintstore.svg)](https://pypi.org/project/pytypehintstore/)
[![License](https://img.shields.io/pypi/l/pytypehintstore.svg)](LICENSE)

Rows of one validated dataclass, in memory, shadowed by a JSON file you can open
and edit. **The class is the database.**

The rows live in a dict and every read answers from there; a writer thread
carries them to a file named for the class and the fingerprint of its compiled
schema — `Task.a50534e4.json`. Change one character of the contract, a type, a
limit, a default, a `Label`, and it is a different database: a new file, with
the old one left beside it, whole and readable.

That is the whole of it, and it is what buys [the guarantee](docs/guarantee.md): **if
the file opens, every row in it is valid against this exact schema.**

## Install

```bash
pip install pytypehintstore
```

That brings [`pytypehint`](https://github.com/offerrall/pytypehint) with it, the
only dependency, pinned to the exact version this release was built against.
Stdlib otherwise. Python 3.11+, `py.typed` included.

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

## Documentation

- [The guarantee](docs/guarantee.md): every row a store opens was rebuilt through your schema.
- [The identity](docs/identity.md): the file name, and why any change to the class is a new database.
- [The file](docs/file.md): the JSON on disk, editing it by hand, and every load error.
- [Writing](docs/writing.md): debounced, atomic dumps, rotated copies and `close()`.
- [The lock](docs/lock.md): one process per store, and how orphaned lockfiles are taken over.
- [API](docs/api.md): `store_of`, its methods and its exceptions.
- [Known limits](docs/limits.md): what the store does not do, stated plainly.
- [Changelog](CHANGELOG.md)

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
