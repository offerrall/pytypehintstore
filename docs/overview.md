# Overview

The rows live in a dict and every read answers from there; a writer thread
carries them to a file named for the class and the fingerprint of its compiled
schema, such as `Task.ef8e8a47.json`. Change one character of the contract, a
type, a limit, a default, a `Label`, and it is a different database: a new file,
with the old one left beside it, whole and readable. See
[The identity](identity.md).

That is the whole of it, and it is what buys [the guarantee](guarantee.md): if
the file opens, every row in it is valid against this exact schema.

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
tasks.path.name                        # 'Task.ef8e8a47.json'

first = tasks.add(Task(title="Buy milk", priority=Priority.HIGH))   # 1
second = tasks.add(Task(title="Write the store"))                   # 2

tasks.get(first)                       # Task(title='Buy milk', priority=<Priority.HIGH: 'high'>, done=False)
tasks.put(second, Task(title="Write the store", done=True))
tasks.remove(first)

len(tasks)                             # 1
second in tasks                        # True
tasks.all()                            # [(2, Task(title='Write the store', ...))]

tasks.close()
```

Reopen it and the rows are there with the same ids; the next `add` gives `3`.
The same walk-through, runnable and printing each step, is
[example.py](https://github.com/offerrall/pytypehintstore/blob/main/example.py).

## Alongside

The store defers every validation to [pytypehint](https://offerrall.github.io/pytypehint/)
and fingerprints its compiled schema. The file carries the transport form, the
shape [pytypehintweb](https://offerrall.github.io/pytypehintweb/) forms speak,
which makes a store the small persistence a
[func-to-web](https://offerrall.github.io/func-to-web/) app usually wants,
without adding a database to it.
