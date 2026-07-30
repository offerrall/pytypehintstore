"""The identity of a schema: eight hex that move when the contract moves."""

import hashlib
import json
from dataclasses import fields, is_dataclass
from datetime import date, time
from enum import Enum

from pytypehint import MISSING

_LENGTH = 8


def fingerprint(schema) -> str:
    """Eight hex of the sha256 of `schema` written as deterministic text."""
    text = json.dumps(_plain(schema, (), {}), sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:_LENGTH]


def _plain(value, seen, done):
    """`value` as text json can render.

    `seen` is the chain of dataclasses above it, so a recursive schema stops
    instead of descending forever. `done` is every dataclass already written,
    because the core compiles a class once and shares that object between every
    field naming it: the schema is a graph, and walking it as a tree costs a
    path per branch — minutes for a class the core compiles in a millisecond.
    """
    if value is MISSING:
        return "MISSING"

    if value is None or type(value) in (bool, int, float, str):
        return value

    if is_dataclass(value) and not isinstance(value, type):
        if id(value) in seen:
            return f"<{type(value).__name__}>"

        if id(value) in done:
            return {"$ref": done[id(value)]}

        done[id(value)] = len(done)

        # Only the fields the core compares. The rest are derived — a compiled
        # regex, the recipe behind a default that is already materialised — and
        # a compiled regex has no text to read.
        return {type(value).__name__: {
            f.name: _plain(getattr(value, f.name), (*seen, id(value)), done)
            for f in fields(value) if f.compare}}

    if isinstance(value, type):
        if issubclass(value, Enum):
            # The member names are contract: renaming one is another database.
            return [value.__name__, list(value.__members__)]

        return value.__name__

    if isinstance(value, Enum):
        return value.name

    if isinstance(value, (date, time)):
        return value.isoformat()

    if isinstance(value, dict):
        return {str(key): _plain(item, seen, done) for key, item in value.items()}

    if isinstance(value, (set, frozenset)):
        return sorted(repr(item) for item in value)

    if isinstance(value, (list, tuple)):
        return [_plain(item, seen, done) for item in value]

    if callable(value):
        return _plain(value(), seen, done)

    # No repr() of courtesy: a repr carries memory addresses, and a hash built
    # on one renames every database on the next run. If the core grows a type
    # this does not know, the store must fail loudly at open.
    raise TypeError(
        f"cannot fingerprint a {type(value).__name__}: it has no stable text")
