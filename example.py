from dataclasses import dataclass
from typing import Annotated, Literal

from pytypehint import Max, Min

from pytypehintstore import store_of

DIRECTORY = "data"


@dataclass
class Task:
    title: Annotated[str, Min(1), Max(80)]
    priority: Literal["low", "normal", "high"] = "normal"
    done: bool = False


def show(label, store):
    print(f"\n{label} — {len(store)} row(s)")
    for task_id, task in store.all():
        print(f"  {task_id}: {task}")


def main():
    tasks = store_of(Task, DIRECTORY)
    print(f"the class is the database: {tasks.path.name}")
    show("opened — rows left by earlier runs", tasks)

    first = tasks.add(Task(title="Buy milk", priority="high"))
    second = tasks.add(Task(title="Write the store", done=True))
    print(f"\nadd gave ids {first} and {second}; "
          f"in the store: {first in tasks}, {second in tasks}")
    show("after two adds", tasks)

    tasks.put(first, Task(title=f"Buy oat milk (edited row {first})",
                          priority="low", done=True))
    tasks.remove(second)
    print(f"\nput {first}, removed {second}; "
          f"in the store: {first in tasks}, {second in tasks}")
    show("after put and remove", tasks)

    tasks.close()

    tasks = store_of(Task, DIRECTORY)
    show("reopened — the rows survived", tasks)

    print(f"\nfile: {tasks.path}")
    print("open it and break a value by hand: the load fails with the core's "
          "error.\nadd a field to Task instead, and this file is left alone "
          "while a new one starts empty.")
    tasks.close()


if __name__ == "__main__":
    main()
