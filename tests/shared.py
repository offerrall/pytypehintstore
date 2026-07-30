"""Dataclasses every area of the suite borrows.

An area that needs a shape of its own declares it in its own module; what lives
here is what more than one of them would otherwise have written twice.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Annotated, Literal

from pytypehint import Max, Min


class Priority(Enum):
    LOW = "low"
    HIGH = "high"


@dataclass
class Task:
    title: Annotated[str, Min(1), Max(80)]
    priority: Literal["low", "normal", "high"] = "normal"
    done: bool = False


@dataclass
class Tag:
    name: Annotated[str, Min(1)]


@dataclass
class Bag:
    tags: list[Tag] = field(default_factory=list)
    note: str = ""
