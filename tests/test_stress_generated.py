"""Schemas nobody wrote by hand, thrown at the codec and at the store.

A generator builds random-but-reproducible dataclasses — atoms, enums, lists,
nested dataclasses, unions, constraints, notation — and, for each one, instances
biased to the edges: the value exactly at `Min`, the value exactly at `Max`, the
empty list, the `None` of every optional, the first and the last branch of every
union.

Every case is a seed. A failure names its seed in the test id, and the seed is
all it takes to rebuild the exact schema and the exact instance.

The properties, as the campaign states them:

* **P1** `add(obj)` then `get(id)` returns something `== obj`, to any depth.
* **P2** `close()` and reopening with the same class returns rows `==` to what
  was stored, under the same ids.
* **P3** the file is JSON in the transport format — ISO text for a date, the
  member NAME for an enum, an object for a nested dataclass — and never a
  Python repr.
* **P4** `build(decode(json.loads(json.dumps(encode(schema, obj))))) == obj`,
  the codec alone, without a store.
* **P5** `fingerprint(struct_of(C))` is stable across compilations.

The golden rule: if `struct_of(C)` does not raise, the store must handle C or
fail loudly. Accepting a row and corrupting it in silence is the finding this
file exists to look for.

Two defects came out of the sweep, each cut by hand down to the smallest schema
that still shows it:

1. `test_a_union_of_lists_routes_by_type_alone_*` — the codec names the wrong
   `list` branch in `$type` and the store then refuses a row the core had just
   certified. Loud; 4 of the 500 generated schemas hit it. Still open, and left
   as `xfail(strict=True)` so a fix turns into an XPASS rather than nothing.
   Declared in the README under Known limits.
2. `test_an_enum_named_like_a_scalar_*` — an enum class named `str`, `date` or
   `time` shared its `$type` with the scalar shape of that name, and the member
   came back as a plain string. Silent, and therefore the worse of the two:
   **fixed**, by refusing at open any union whose options share a transport
   name. The tests now pin the refusal, and the codec half of the finding stays
   beside it to say what the guard is for.

Run the whole file with:

    python -m pytest tests/test_stress_generated.py -q --durations=10

It is deterministic: same seeds, same classes, same instances, same result.
"""

import copy
import json
import math
import random
import re
import unicodedata
from dataclasses import field as dc_field, make_dataclass
from datetime import date, time
from enum import Enum
from functools import partial
from typing import Annotated, Union

import pytest

from pytypehint import (
    MISSING, Bool, Choices, Date, Description, EnumShape, Float, Int, Label,
    List, Max, Min, MultipleOf, NoneShape, Pattern, Placeholder,
    SchemaValueError, Struct, Time, struct_of,
)
from pytypehint.validation import check_options_value, value_branch
from pytypehintstore import store_of
from pytypehintstore.codec import encode
from pytypehintstore.fingerprint import fingerprint

# How much of the campaign runs here. The whole file stays inside a minute on a
# laptop; see the report at the bottom of this docstring block for the numbers.
P4_SEEDS = tuple(range(1, 501))          # 500 schemas x 3 instances, codec only
STORE_SEEDS = tuple(range(1, 51))        # 50 of them through a real store
FINGERPRINT_SEEDS = tuple(range(1, 101))  # 100 compiled twice

MODES = ("lo", "hi", "mid")

# Schemas the core refused to compile, collected as the sweeps run. A refusal is
# not a failure — it is the core saying the combination is not a schema — but a
# generator that produced nothing but refusals would be testing itself, so the
# rate is asserted at the end.
_REJECTED: list[tuple[int, str]] = []

# The seeds that used to hit the list-routing defect, kept as empty sets rather
# than removed: the machinery that marks a seed broken is what a future finding
# would reach for, and an empty set says the campaign currently has none.
_LIST_ROUTING_DEFECT = "no seed is expected to fail"
_BAD_P4_SEEDS: set[int] = set()
_BAD_STORE_SEEDS: set[int] = set()
_BAD_ROUTING_SEEDS: set[int] = set()


def _seed_params(seeds, broken):
    return [pytest.param(seed, id=f"seed{seed}",
                         marks=(pytest.mark.xfail(strict=True, reason=_LIST_ROUTING_DEFECT),)
                         if seed in broken else ())
            for seed in seeds]


# --------------------------------------------------------------- the alphabet

# Strings the transport has to carry intact: ASCII, accents, CJK, astral planes,
# the characters JSON escapes, and the ones that are legal in JSON text but
# famously break naive readers.
_TEXT = (
    "abcXYZ019 _-.,:;"
    "ñáéÜß"
    "日本語한국어Привет"
    "🙂🎉"
    "\t\n\\\"'{}[]$"
)

# The characters a source file cannot spell without lying about itself:
# NUL, a control, DEL, a no-break space, and the two separators that are legal
# JSON text and break naive readers.
_TEXT += "".join(chr(c) for c in (0x00, 0x1F, 0x7F, 0xA0, 0x2028, 0x2029))

# Identifier fragments. Every name below is NFKC-stable, so the source Python
# generates for __init__ spells the same name the annotations do.
_IDENT_ATOMS = ("a", "z", "Q", "ñ", "é", "日", "к", "_x", "M0", "Zz")


def _identifier(rng, *, style=None):
    style = style or rng.choice(("short", "plain", "unicode", "long", "single"))

    if style == "single":
        name = rng.choice("abcXYZñé日к")
    elif style == "short":
        name = rng.choice(_IDENT_ATOMS).lstrip("_") or "a"
    elif style == "unicode":
        name = "".join(rng.choice(_IDENT_ATOMS) for _ in range(rng.randint(2, 4)))
    elif style == "long":
        name = rng.choice("abcXYZ") * rng.randint(60, 140)
    else:
        name = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(rng.randint(3, 9)))

    name = name.lstrip("_")

    if not name or not name[0].isalpha():
        name = "a" + name

    assert name.isidentifier() and unicodedata.normalize("NFKC", name) == name
    return name


# Patterns whose language is small enough to sample from, and portable across
# every regex engine the transport could meet.
_PATTERNS = (
    (r"[a-z]+", lambda rng: "".join(rng.choice("abcdefghij") for _ in range(rng.randint(1, 8)))),
    (r"[A-Z]{3}", lambda rng: "".join(rng.choice("ABCDEFG") for _ in range(3))),
    (r"[0-9]{4}", lambda rng: "".join(rng.choice("0123456789") for _ in range(4))),
    (r"[a-z0-9_]{1,8}", lambda rng: "".join(rng.choice("abc012_") for _ in range(rng.randint(1, 8)))),
    (r"(alpha|beta|gamma)", lambda rng: rng.choice(("alpha", "beta", "gamma"))),
    (r"x*", lambda rng: "x" * rng.randint(0, 6)),
    (r"[^,]{2}", lambda rng: "".join(rng.choice("ab日🙂") for _ in range(2))),
)


# ------------------------------------------------------------------- the node

class Node:
    """One option of one field: the hint to write, and how to fill it.

    `key` is what the core uses to tell two options apart — its runtime type and
    its public identity — so the generator can refuse to build a union the core
    would reject for having two options it cannot name.
    """

    __slots__ = ("hint", "pykey", "oid", "make")

    def __init__(self, hint, pykey, oid, make):
        self.hint = hint
        self.pykey = pykey
        self.oid = oid
        self.make = make

    @property
    def key(self):
        return (self.pykey, self.oid)


def _annotated(base, meta):
    return Annotated[tuple([base, *meta])] if meta else base


# ------------------------------------------------------------- the generator

class Generator:
    """Dataclasses from a seed. Same seed, same classes, same instances."""

    def __init__(self, seed, *, max_depth=5, max_fields=6, budget=40):
        self.seed = seed
        self.rng = random.Random(seed)
        self.max_depth = max_depth
        self.max_fields = max_fields
        self.budget = budget
        self._n = 0

    def _name(self, kind):
        self._n += 1
        return f"{kind}{self.seed}_{self._n}"

    # ---- atoms ------------------------------------------------------------

    def _placeholder(self):
        # Notation: it must reach the schema and nothing else.
        if self.rng.random() < 0.15:
            return [Placeholder(self.rng.choice(("type here", "…", "0")))]
        return []

    def _int(self):
        rng = self.rng
        meta = []
        style = rng.choice(("plain", "min", "max", "range", "multiple", "choices", "range"))
        lo = hi = None
        mult = None
        choices = None

        if style == "choices":
            choices = tuple(sorted(rng.sample(range(-100, 100), rng.randint(1, 5))))
            meta.append(Choices(values=choices))
        else:
            if style in ("min", "range"):
                base = rng.randint(-50, 50)
                exclusive = rng.random() < 0.3
                meta.append(Min(base, exclusive=exclusive))
                lo = base + 1 if exclusive else base
            if style in ("max", "range"):
                start = lo if lo is not None else rng.randint(-50, 50)
                base = start + rng.randint(1, 100)
                exclusive = rng.random() < 0.3
                meta.append(Max(base, exclusive=exclusive))
                hi = base - 1 if exclusive else base
            if style == "multiple":
                mult = rng.choice((2, 3, 5, 7))
                meta.append(MultipleOf(mult))

        meta.extend(self._placeholder())

        def make(rng, mode):
            if choices is not None:
                return choices[0] if mode == "lo" else choices[-1] if mode == "hi" else rng.choice(choices)

            a = lo if lo is not None else -1_000_000
            b = hi if hi is not None else 1_000_000

            if mult is not None:
                a = -(-a // mult) * mult
                b = (b // mult) * mult

            if mode == "lo":
                return a
            if mode == "hi":
                return b

            value = rng.randint(a, b)
            return (value // mult) * mult if mult is not None else value

        return Node(_annotated(int, meta), int, "int", make)

    def _float(self):
        rng = self.rng
        meta = []
        style = rng.choice(("plain", "min", "max", "range", "choices"))
        lo = hi = None
        choices = None

        if style == "choices":
            choices = tuple(sorted({round(rng.uniform(-50, 50), 3) for _ in range(rng.randint(1, 4))}))
            meta.append(Choices(values=choices))
        else:
            if style in ("min", "range"):
                base = round(rng.uniform(-50, 50), 3)
                exclusive = rng.random() < 0.3
                meta.append(Min(base, exclusive=exclusive))
                lo = math.nextafter(base, math.inf) if exclusive else base
            if style in ("max", "range"):
                start = lo if lo is not None else round(rng.uniform(-50, 50), 3)
                base = round(start + rng.uniform(1, 100), 3)
                exclusive = rng.random() < 0.3
                meta.append(Max(base, exclusive=exclusive))
                hi = math.nextafter(base, -math.inf) if exclusive else base

        meta.extend(self._placeholder())

        def make(rng, mode):
            if choices is not None:
                return choices[0] if mode == "lo" else choices[-1] if mode == "hi" else rng.choice(choices)

            a = float(lo) if lo is not None else -1e6
            b = float(hi) if hi is not None else 1e6

            if mode == "lo":
                return a
            if mode == "hi":
                return b

            return min(max(round(rng.uniform(a, b), 6), a), b)

        return Node(_annotated(float, meta), float, "float", make)

    def _str(self):
        rng = self.rng
        meta = []
        flavor = rng.choice(("plain", "plain", "length", "pattern", "choices"))
        low = high = None
        pattern_make = None
        choices = None

        if flavor == "length":
            low = rng.randint(0, 4)
            high = low + rng.randint(0, 20)
            meta.append(Min(low))
            meta.append(Max(high))
        elif flavor == "pattern":
            expression, pattern_make = rng.choice(_PATTERNS)
            meta.append(Pattern(expression))
        elif flavor == "choices":
            values = {self._text(rng, rng.randint(0, 6)) for _ in range(rng.randint(1, 4))}
            choices = tuple(sorted(values))
            meta.append(Choices(values=choices))

        meta.extend(self._placeholder())

        def make(rng, mode):
            if choices is not None:
                return choices[0] if mode == "lo" else choices[-1] if mode == "hi" else rng.choice(choices)

            if pattern_make is not None:
                return pattern_make(rng)

            if low is not None:
                length = low if mode == "lo" else high if mode == "hi" else rng.randint(low, high)
                return self._text(rng, length)

            length = 0 if mode == "lo" else 200 if mode == "hi" else rng.randint(0, 12)
            return self._text(rng, length)

        return Node(_annotated(str, meta), str, "str", make)

    @staticmethod
    def _text(rng, length):
        return "".join(rng.choice(_TEXT) for _ in range(length))

    def _bool(self):
        return Node(bool, bool, "bool",
                    lambda rng, mode: mode != "lo" if mode in ("lo", "hi") else rng.choice((True, False)))

    def _date(self):
        rng = self.rng
        meta = []
        style = rng.choice(("plain", "plain", "range", "choices"))
        lo = hi = None
        choices = None

        if style == "choices":
            ordinals = sorted({rng.randint(1, date.max.toordinal()) for _ in range(rng.randint(1, 4))})
            choices = tuple(date.fromordinal(o) for o in ordinals)
            meta.append(Choices(values=choices))
        elif style == "range":
            start = rng.randint(1, date.max.toordinal() - 1000)
            lo = date.fromordinal(start)
            hi = date.fromordinal(start + rng.randint(0, 1000))
            meta.append(Min(lo))
            meta.append(Max(hi))

        meta.extend(self._placeholder())

        def make(rng, mode):
            if choices is not None:
                return choices[0] if mode == "lo" else choices[-1] if mode == "hi" else rng.choice(choices)

            a = lo.toordinal() if lo is not None else 1
            b = hi.toordinal() if hi is not None else date.max.toordinal()

            if mode == "lo":
                return date.fromordinal(a)
            if mode == "hi":
                return date.fromordinal(b)

            return date.fromordinal(rng.randint(a, b))

        return Node(_annotated(date, meta), date, "date", make)

    def _time(self):
        rng = self.rng
        meta = []
        style = rng.choice(("plain", "plain", "range", "choices"))
        lo = hi = None
        choices = None

        if style == "choices":
            seconds = sorted({rng.randint(0, 86399) for _ in range(rng.randint(1, 4))})
            choices = tuple(_time_of(s) for s in seconds)
            meta.append(Choices(values=choices))
        elif style == "range":
            start = rng.randint(0, 86399)
            lo = _time_of(start)
            hi = _time_of(min(86399, start + rng.randint(0, 3600)))
            meta.append(Min(lo))
            meta.append(Max(hi))

        meta.extend(self._placeholder())

        def make(rng, mode):
            if choices is not None:
                return choices[0] if mode == "lo" else choices[-1] if mode == "hi" else rng.choice(choices)

            a = _seconds_of(lo) if lo is not None else 0
            b = _seconds_of(hi) if hi is not None else 86399

            if mode == "lo":
                return _time_of(a)
            if mode == "hi":
                return _time_of(b)

            return _time_of(rng.randint(a, b))

        return Node(_annotated(time, meta), time, "time", make)

    def _none(self):
        return Node(None, type(None), "None", lambda rng, mode: None)

    # ---- enums ------------------------------------------------------------

    def sample_enum(self):
        rng = self.rng
        name = self._name("E")
        count = rng.randint(1, 8)

        names = []
        while len(names) < count:
            candidate = _identifier(rng)
            if candidate not in names and candidate not in ("name", "value", "mro"):
                names.append(candidate)

        if rng.random() < 0.5:
            values = [f"v{i}" for i in range(count)]
        else:
            values = list(range(1, count + 1))

        # An alias: a second name on a value that already has one. Python folds
        # it into the first member, so the NAME the transport writes is the
        # canonical one and the round trip has to agree.
        if count > 1 and rng.random() < 0.4:
            values[-1] = values[0]

        cls = Enum(name, list(zip(names, values)))
        members = list(cls)

        def make(rng, mode):
            if mode == "lo":
                return members[0]
            if mode == "hi":
                return members[-1]
            return rng.choice(members)

        return Node(cls, cls, name, make)

    # ---- containers -------------------------------------------------------

    def _list(self, depth):
        rng = self.rng
        options = self._options(depth + 1, count=rng.choices((1, 1, 2, 3), k=1)[0],
                               allow_none=True)

        if len(options) == 1:
            item_hint = options[0].hint
        else:
            item_hint = Union[tuple(o.hint for o in options)]

        meta = []
        low = high = None

        if rng.random() < 0.3:
            low = rng.randint(0, 2)
            high = low + rng.randint(0, 4)
            meta.append(Min(low))
            meta.append(Max(high))

        oid = f"list[{' | '.join(o.oid for o in options)}]"

        def make(rng, mode):
            a = low if low is not None else 0
            b = high if high is not None else 4
            length = a if mode == "lo" else b if mode == "hi" else rng.randint(a, b)
            return [_pick(options, rng, mode) for _ in range(length)]

        return Node(_annotated(list[item_hint], meta), list, oid, make)

    # ---- dataclasses ------------------------------------------------------

    def _struct(self, depth):
        rng = self.rng
        name = self._name("G")
        count = rng.randint(1, self.max_fields)

        plain = []
        defaulted = []
        used = set()

        for _ in range(count):
            field_name = _identifier(rng)
            while field_name in used:
                field_name = _identifier(rng) + str(len(used))
            used.add(field_name)

            options = self._options(depth + 1, count=rng.choices((1, 1, 1, 2, 3, 4), k=1)[0],
                                    allow_none=True)
            hint = options[0].hint if len(options) == 1 else Union[tuple(o.hint for o in options)]

            # Field notation. It rides on the field, never on an option, so a
            # union carries it without the core reading it as type metadata.
            notes = []
            if rng.random() < 0.2:
                notes.append(Label(self._text(rng, rng.randint(1, 6)) or "l"))
            if rng.random() < 0.15:
                notes.append(Description(self._text(rng, rng.randint(1, 10)) or "d"))
            if notes:
                hint = Annotated[tuple([hint, *notes])]

            maker = partial(_pick, options)

            if rng.random() < 0.35:
                value = maker(rng, "mid")
                spec = (dc_field(default_factory=partial(copy.deepcopy, value))
                        if value.__class__.__hash__ is None
                        else dc_field(default=value))
                defaulted.append(((field_name, hint, spec), maker))
            else:
                plain.append(((field_name, hint), maker))

        entries = [e for e, _ in plain] + [e for e, _ in defaulted]
        makers = [(e[0], m) for e, m in plain] + [(e[0], m) for e, m in defaulted]

        cls = make_dataclass(name, entries)

        def make(rng, mode):
            return cls(**{field_name: maker(rng, mode) for field_name, maker in makers})

        return Node(cls, cls, name, make)

    # ---- choosing options -------------------------------------------------

    def _option(self, depth):
        rng = self.rng
        atoms = (self._int, self._float, self._str, self._bool, self._date, self._time)

        if depth >= self.max_depth or self.budget <= 0:
            return rng.choice(atoms)()

        self.budget -= 1
        kind = rng.choices(("atom", "enum", "list", "struct"), weights=(9, 3, 3, 3), k=1)[0]

        if kind == "atom":
            return rng.choice(atoms)()
        if kind == "enum":
            return self.sample_enum()
        if kind == "list":
            return self._list(depth)
        return self._struct(depth)

    def _options(self, depth, *, count, allow_none):
        """`count` options no two of which the core would call the same thing."""
        rng = self.rng
        chosen = []
        keys = set()

        for _ in range(count):
            node = self._option(depth)
            if node.key in keys:
                continue
            keys.add(node.key)
            chosen.append(node)

        if not chosen:
            node = self._int()
            chosen.append(node)
            keys.add(node.key)

        # `None` is optionality, never the whole of a field, and never the whole
        # of a list item either.
        if allow_none and rng.random() < 0.3:
            none = self._none()
            if none.key not in keys:
                chosen.insert(rng.randrange(len(chosen) + 1), none)

        return chosen

    def root(self):
        return self._struct(0)


def _pick(options, rng, mode):
    """The branch a mode asks for: the first, the last, or one at random."""
    if len(options) == 1:
        node = options[0]
    elif mode == "lo":
        node = options[0]
    elif mode == "hi":
        node = options[-1]
    else:
        node = rng.choice(options)
    return node.make(rng, mode)


def _time_of(seconds):
    return time(seconds // 3600, (seconds // 60) % 60, seconds % 60)


def _seconds_of(value):
    return value.hour * 3600 + value.minute * 60 + value.second


# ------------------------------------------------------------- what a seed is

def compile_seed(seed):
    """The class a seed names, its schema, and three instances of it.

    Returns None when the core refuses the schema: a refusal is the core's
    verdict on a combination, not a failure of the store.
    """
    generator = Generator(seed)
    root = generator.root()

    try:
        schema = struct_of(root.hint)
    except (TypeError, ValueError) as e:
        _REJECTED.append((seed, f"{type(e).__name__}: {e}"))
        return None

    instances = []
    for i, mode in enumerate(MODES):
        rng = random.Random(seed * 7919 + i)
        instances.append(root.make(rng, mode))

    return root.hint, schema, instances


# ------------------------------------------------------------ the transport

def _wire_type(shape):
    if type(shape) is Struct:
        return dict
    if type(shape) in (Date, Time, EnumShape):
        return str
    return shape.pytype


def check_transport(shapes, value, path=()):
    """P3: `value` is JSON in the transport format, read against `shapes`.

    Written from the format's own rules rather than from the codec's code: a
    date is ISO text, an enum is a member NAME, a dataclass is an object, and
    two options that would arrive as the same JSON type carry a `$type`.
    """
    where = ".".join(str(p) for p in path) or "<row>"

    assert type(value) in (dict, list, str, int, float, bool, type(None)), (
        f"{where}: {type(value).__name__} is not JSON — a Python value reached the file")

    if type(value) is dict:
        if "$type" in value and "$value" in value:
            named = [s for s in shapes
                     if type(s) is not Struct and s.option_id() == value["$type"]]
            assert named, f"{where}: $type {value['$type']!r} names no option"
            check_transport((named[0],), value["$value"], (*path, "$value"))
            return

        structs = [s for s in shapes if type(s) is Struct]
        assert structs, f"{where}: an object where no dataclass is accepted"

        if "$type" in value:
            picked = [s for s in structs if s.cls.__name__ == value["$type"]]
            assert picked, f"{where}: $type {value['$type']!r} names no dataclass"
            struct = picked[0]
        else:
            assert len(structs) == 1, f"{where}: {len(structs)} dataclasses and no $type"
            struct = structs[0]

        names = {f.name for f in struct.fields}
        assert set(value) - {"$type"} == names, f"{where}: fields do not match {struct.cls.__name__}"

        for f in struct.fields:
            check_transport(f.shape, value[f.name], (*path, f.name))
        return

    if type(value) is list:
        lists = [s for s in shapes if type(s) is List]
        assert len(lists) == 1, f"{where}: a bare list where {len(lists)} list options compete"
        for i, item in enumerate(value):
            check_transport(lists[0].item, item, (*path, i))
        return

    if type(value) is str:
        readings = [s for s in shapes if _wire_type(s) is str]
        assert len(readings) == 1, f"{where}: bare text where {len(readings)} options read as text"
        shape = readings[0]

        if type(shape) is Date:
            assert date.fromisoformat(value), f"{where}: {value!r} is not an ISO date"
        elif type(shape) is Time:
            time.fromisoformat(value)
        elif type(shape) is EnumShape:
            assert value in shape.cls.__members__, f"{where}: {value!r} is not a member name"
        return

    if type(value) is bool:
        assert any(type(s) is Bool for s in shapes), f"{where}: a bool where none is accepted"
    elif type(value) is int:
        assert any(type(s) is Int for s in shapes), f"{where}: an int where none is accepted"
    elif type(value) is float:
        assert any(type(s) is Float for s in shapes), f"{where}: a float where none is accepted"
    else:
        assert any(type(s) is NoneShape for s in shapes), f"{where}: a null where none is accepted"


# ---------------------------------------------------------------- P4: codec

@pytest.mark.parametrize("seed", _seed_params(P4_SEEDS, _BAD_P4_SEEDS))
def test_codec_round_trip(seed):
    """P4: encode, write, read, decode, build — and get the object back."""
    compiled = compile_seed(seed)

    if compiled is None:
        pytest.skip(f"seed {seed}: the core refused the schema")

    cls, schema, instances = compiled

    for mode, obj in zip(MODES, instances):
        wire = json.loads(json.dumps(encode(schema, obj), ensure_ascii=False))
        check_transport((schema,), wire)
        back = schema.build(schema.decode(wire))

        assert type(back) is cls, f"seed {seed} [{mode}]: came back a {type(back).__name__}"
        assert back == obj, f"seed {seed} [{mode}]: round trip changed the row"


def check_routing(shapes, value, wire, path=()):
    """The `$type` on the wire names the branch the core would have chosen.

    A sharper reading of P4 than equality is. Two options that share a Python
    type — in practice, two `list[...]` — are told apart by the core with
    `value_branch`, which reads the whole shape. If the transport names the
    other one, the row is only saved by the two branches happening to agree
    about this particular value; the next value is the one that breaks.
    """
    where = ".".join(str(p) for p in path) or "<row>"
    chosen = value_branch(shapes, value)

    if type(wire) is dict and "$type" in wire and "$value" in wire:
        assert chosen is not None, f"{where}: no option of the schema owns this value"
        assert chosen.option_id() == wire["$type"], (
            f"{where}: the file says {wire['$type']!r}, the core would read it as "
            f"{chosen.option_id()!r}")
        _descend(chosen, value, wire["$value"], path)
        return

    if chosen is None:
        return

    inner = ({k: v for k, v in wire.items() if k != "$type"}
             if type(wire) is dict else wire)
    _descend(chosen, value, inner, path)


def _descend(shape, value, wire, path):
    if type(shape) is Struct:
        for f in shape.fields:
            check_routing(f.shape, getattr(value, f.name), wire[f.name], (*path, f.name))
    elif type(shape) is List:
        for i, (item, written) in enumerate(zip(value, wire)):
            check_routing(shape.item, item, written, (*path, i))


@pytest.mark.parametrize("seed", _seed_params(P4_SEEDS, _BAD_ROUTING_SEEDS))
def test_the_transport_names_the_branch_the_core_would_choose(seed):
    compiled = compile_seed(seed)

    if compiled is None:
        pytest.skip(f"seed {seed}: the core refused the schema")

    _, schema, instances = compiled

    for mode, obj in zip(MODES, instances):
        wire = json.loads(json.dumps(encode(schema, obj), ensure_ascii=False))
        check_routing((schema,), obj, wire, (mode,))


# ----------------------------------------------------------- P1, P2, P3: store

@pytest.mark.parametrize("seed", _seed_params(STORE_SEEDS, _BAD_STORE_SEEDS))
def test_store_round_trip(seed, tmp_path):
    """P1, P2 and P3 through a real store on disk."""
    compiled = compile_seed(seed)

    if compiled is None:
        pytest.skip(f"seed {seed}: the core refused the schema")

    cls, schema, instances = compiled

    store = store_of(cls, tmp_path, debounce=0.0, keep=2)
    try:
        ids = [store.add(obj) for obj in instances]

        # P1
        for row_id, obj in zip(ids, instances):
            assert store.get(row_id) == obj, f"seed {seed}: get({row_id}) is not what was added"
            assert row_id in store

        assert len(store) == len(instances)
        path = store.path
    finally:
        store.close()

    # P3
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["v"] == 1
    text = path.read_text(encoding="utf-8")
    for repr_mark in ("datetime.date(", "datetime.time(", "<enum ", "object at 0x"):
        assert repr_mark not in text, f"seed {seed}: a Python repr reached the file"

    for row_id, obj in zip(ids, instances):
        check_transport((schema,), payload["rows"][str(row_id)])

    # P2
    reopened = store_of(cls, tmp_path, debounce=0.0)
    try:
        assert reopened.all() == list(zip(ids, instances)), (
            f"seed {seed}: reopening did not return the rows that were stored")
    finally:
        reopened.close()


# ---------------------------------------------------------- P5: fingerprint

@pytest.mark.parametrize("seed", FINGERPRINT_SEEDS, ids=lambda s: f"seed{s}")
def test_fingerprint_is_stable(seed):
    """P5: compiling the same class twice is the same database.

    And so is compiling a second class built from the same seed: the schema is
    the contract, and two classes that spell the same contract have to land on
    the same file.
    """
    first = Generator(seed).root().hint

    try:
        once = fingerprint(struct_of(first))
    except (TypeError, ValueError):
        pytest.skip(f"seed {seed}: the core refused the schema")

    assert fingerprint(struct_of(first)) == once, f"seed {seed}: recompiling moved the fingerprint"

    twin = Generator(seed).root().hint
    assert fingerprint(struct_of(twin)) == once, (
        f"seed {seed}: an identical class landed on a different database")


COVERAGE_SEEDS = tuple(range(1, 201))


def _survey(shapes, depth, found):
    """What a compiled schema actually contains, so the sweep can prove it aims."""
    found["max_union"] = max(found["max_union"], len(shapes))

    for shape in shapes:
        kind = type(shape)
        found["kinds"].add(kind.__name__)

        if kind is NoneShape:
            found["optional"] += 1
        elif kind is EnumShape:
            found["enums"] += 1
            if len(shape.cls.__members__) > len(list(shape.cls)):
                found["enum_aliases"] += 1
            if len(shape.cls.__members__) == 1:
                found["enum_single"] += 1
        elif kind is List:
            found["max_depth"] = max(found["max_depth"], depth + 1)
            if len(shape.item) > 1:
                found["list_of_union"] += 1
            if any(type(i) is List for i in shape.item):
                found["list_of_list"] += 1
            if shape.min is not None or shape.max is not None:
                found["list_bounds"] += 1
            _survey(shape.item, depth + 1, found)
        elif kind is Struct:
            found["max_depth"] = max(found["max_depth"], depth + 1)
            found["structs"] += 1
            for f in shape.fields:
                if f.default is not MISSING:
                    found["defaults"] += 1
                if f.label is not None:
                    found["labels"] += 1
                if f.description is not None:
                    found["descriptions"] += 1
                _survey(f.shape, depth + 1, found)
        else:
            for limit in ("min", "max", "choices", "multiple_of", "pattern"):
                if getattr(shape, limit, None) is not None:
                    found[limit] += 1
            if getattr(shape, "placeholder", None) is not None:
                found["placeholders"] += 1
            if getattr(shape, "min", None) is not None and shape.min.exclusive:
                found["exclusive"] += 1


def test_the_generator_reaches_every_corner_it_claims():
    """The sweep is only worth its runtime if the schemas are actually varied.

    A generator that quietly collapsed to `dataclass(x: int)` would pass every
    property in this file. This is the test that would notice.
    """
    found = {"kinds": set(), "max_union": 0, "max_depth": 0}
    for counter in ("optional", "enums", "enum_aliases", "enum_single", "structs",
                    "list_of_union", "list_of_list", "list_bounds", "defaults",
                    "labels", "descriptions", "placeholders", "exclusive",
                    "min", "max", "choices", "multiple_of", "pattern"):
        found[counter] = 0

    for seed in COVERAGE_SEEDS:
        compiled = compile_seed(seed)
        if compiled is not None:
            _survey((compiled[1],), 0, found)

    assert found["kinds"] >= {"Int", "Float", "Str", "Bool", "Date", "Time",
                              "NoneShape", "List", "EnumShape", "Struct"}, found["kinds"]
    assert found["max_depth"] >= 5, found
    assert found["max_union"] >= 4, found

    for counter, floor in (("optional", 50), ("enums", 50), ("enum_aliases", 5),
                           ("enum_single", 5), ("structs", 400), ("list_of_union", 20),
                           ("list_of_list", 5), ("list_bounds", 20), ("defaults", 200),
                           ("labels", 50), ("descriptions", 30), ("placeholders", 30),
                           ("exclusive", 20), ("min", 100), ("max", 100),
                           ("choices", 50), ("multiple_of", 10), ("pattern", 20)):
        assert found[counter] >= floor, f"{counter}: {found[counter]} < {floor}"


def test_the_combinations_the_core_refuses_to_compile():
    """What the generator has to steer around, spelled out.

    The brief asks how many combinations the core rejects. The answer from the
    sweep is none — because the generator computes each option's identity before
    it writes the union and never offers the core a schema it would refuse.
    These are the refusals it is steering around; if the core ever stops
    refusing one of them, this test says so and the generator can widen.
    """
    def refuses(build):
        with pytest.raises((TypeError, ValueError)):
            struct_of(build())

    # Two options the core cannot tell apart: same runtime type, same identity.
    refuses(lambda: make_dataclass("DupOptions", [("f", Union[list[str], list[Annotated[str, Min(1)]]])]))

    # A field that is only None: optionality with nothing to be optional about.
    refuses(lambda: make_dataclass("OnlyNone", [("f", None)]))

    # A list with no item type.
    refuses(lambda: make_dataclass("BareList", [("f", list)]))

    # Type metadata over a union of several real options.
    refuses(lambda: make_dataclass("SharedMeta", [("f", Annotated[Union[int, str], Min(0)])]))

    # Two dataclasses in one field that share a name: nothing left to name them by.
    twin_a = make_dataclass("Twin", [("a", int)])
    twin_b = make_dataclass("Twin", [("b", int)])
    refuses(lambda: make_dataclass("TwinField", [("f", Union[twin_a, twin_b])]))

    # A field atom on a list item.
    refuses(lambda: make_dataclass("ItemLabel", [("f", list[Annotated[int, Label("no")]])]))

    # An enum with no members.
    refuses(lambda: make_dataclass("EmptyEnum", [("f", Enum("Nothing", []))]))

    # A limit whose range is empty, and a Choices outside its own bounds.
    refuses(lambda: make_dataclass("EmptyRange", [("f", Annotated[int, Min(5), Max(4)])]))
    refuses(lambda: make_dataclass("OutOfRange",
                                   [("f", Annotated[int, Max(3), Choices(values=(9,))])]))


def test_the_core_refuses_few_of_the_generated_schemas():
    """The generator is meant to build schemas, not to discover the core's no.

    Runs last on purpose: it reads what the sweeps above collected. Run alone it
    sees nothing and passes, which is what an upper bound should do.
    """
    attempted = len(P4_SEEDS) + len(STORE_SEEDS) + len(FINGERPRINT_SEEDS)
    assert len(_REJECTED) <= attempted * 0.05, (
        f"the core refused {len(_REJECTED)} schemas; examples: {_REJECTED[:5]}")


# =============================================================================
# Reduced findings: every one below started as a random seed and was cut down
# by hand to the smallest schema that still shows it.
# =============================================================================

# An enum whose class is called `str`. The class name is what `option_id()`
# reports, the transport uses that name as its `$type`, and `str` is also what
# the `Str` shape reports. Built with the functional API so the name is exactly
# "str" without shadowing the builtin in this module.
StrNamedEnum = Enum("str", [("A", "a"), ("B", "b")])  # type: ignore[misc]


def test_an_enum_named_like_a_scalar_never_reaches_a_schema():
    """The finding the store's own guard used to cover, now the core's to refuse.

    `option_id()` is the bare class name, so an enum called `str` answers to the
    same wrapper name as the `Str` beside it, and a member written under that
    name came back as a plain string. The store used to check for the pair when
    a store was opened, which left `struct_of` accepting a schema nobody could
    round trip. In 1.0.0 the identity rule belongs to the core and runs at
    compilation, so the schema never exists and the store has nothing to guard.
    """
    with pytest.raises(ValueError, match=r"duplicate discriminator name\(s\): str"):
        struct_of(make_dataclass("RowWithStrNamedEnum",
                                 [("f", Union[str, StrNamedEnum])]))


def test_a_schema_the_codec_could_not_name_never_becomes_a_store(tmp_path):
    """The row that used to be accepted, written, and read back as a str.

    `store_of` compiles before it touches the directory, so the core's refusal
    arrives first and in the core's own words — the store adds nothing to it —
    and nothing is left on disk either way."""
    Row = make_dataclass("StoredRowWithStrNamedEnum",
                         [("f", Union[str, StrNamedEnum])])

    with pytest.raises(ValueError, match=r"duplicate discriminator name\(s\): str"):
        store_of(Row, tmp_path)

    assert list(tmp_path.iterdir()) == [], "a refused schema left something behind"


# The defect these three reduced cases were written for: the codec used to pick
# between two `list` options with a router of its own that read the runtime type
# of each item and never the constraints, so it named a `$type` the value did not
# satisfy and `build()` refused a row the core had just certified. In 1.0.0 the
# codec asks `pytypehint.validation.value_branch` — the core's own router, the
# one `check_options_value` agrees with by construction — and the disagreement
# has nowhere left to come from. They are kept as plain tests: the property they
# pin is the one the fix delivers.


def test_a_union_of_lists_routes_by_type_alone_on_length():
    """`[]` is only valid on the branch without `Min(1)`, and goes to the other."""
    Row = make_dataclass("EmptyListRoutingRow",
                         [("f", Union[Annotated[list[str], Min(1)], list[int]])])
    schema = struct_of(Row)

    obj = Row([])
    check_options_value(schema.fields[0].shape, obj.f)  # the core accepts it

    wire = json.loads(json.dumps(encode(schema, obj)))
    assert wire == {"f": {"$type": "list[int]", "$value": []}}, wire

    assert schema.build(schema.decode(wire)) == obj


def test_a_union_of_lists_routes_by_type_alone_on_a_pattern():
    """`["ZZ"]` matches no `[a-z]+`, so it belongs to the other branch."""
    Row = make_dataclass("PatternRoutingRow", [
        ("f", Union[list[Annotated[str, Pattern("[a-z]+")]], list[Union[str, int]]]),
    ])
    schema = struct_of(Row)

    obj = Row(["ZZ"])
    check_options_value(schema.fields[0].shape, obj.f)

    assert schema.build(schema.decode(json.loads(json.dumps(encode(schema, obj))))) == obj


def test_a_union_of_lists_routes_by_type_alone_on_choices():
    """Same defect with `Choices`, and through a real store."""
    Row = make_dataclass("ChoicesRoutingRow", [
        ("f", Union[list[Annotated[int, Choices(values=(1, 2))]], list[Union[int, str]]]),
    ])
    schema = struct_of(Row)

    obj = Row([7])
    check_options_value(schema.fields[0].shape, obj.f)

    assert schema.build(schema.decode(json.loads(json.dumps(encode(schema, obj))))) == obj


# Every list branch the catalogue below knows, grouped by the identity the core
# gives it. Two branches from one group cannot share a field — the core refuses
# a union it could not name — so the pairs are drawn across groups.
_LIST_BRANCHES = {
    "list[str]": (
        list[str],
        Annotated[list[str], Min(1)],
        list[Annotated[str, Pattern("[a-z]+")]],
        list[Annotated[str, Max(2)]],
    ),
    "list[int]": (
        list[int],
        list[Annotated[int, Choices(values=(1, 2))]],
        Annotated[list[int], Max(1)],
    ),
    "list[str | int]": (list[Union[str, int]],),
    "list[str | None]": (list[Union[str, None]],),
}

_LIST_VALUES: tuple[list, ...] = (
    [], ["a"], ["ZZ"], ["abcdef"], [1], [7], [None], ["a", None], [1, "a"],
    ["a", "b", "c"])


def test_a_misrouted_list_is_loud_and_never_silent():
    """The blast radius of the defect above, searched rather than argued.

    Every pair of list branches the core will accept in one field, against every
    value the core calls valid: the codec either names the branch the core would
    have named, or names another one that refuses the row outright. What it
    never does is name another one that ACCEPTS the row and gives back something
    different — a constraint cannot change the Python type an item decodes to,
    so the two readings of a value both branches accept are the same reading.

    If this test ever reports a silent case, that case is the finding, not this
    paragraph.
    """
    groups = sorted(_LIST_BRANCHES)
    agreed = misrouted = silent = 0
    examples = []

    for i, left in enumerate(groups):
        for right in groups[i + 1:]:
            for a in _LIST_BRANCHES[left]:
                for b in _LIST_BRANCHES[right]:
                    for hints in ((a, b), (b, a)):
                        Row = make_dataclass(f"Pair{agreed}_{misrouted}",
                                             [("f", Union[hints])])
                        schema = struct_of(Row)
                        shapes = schema.fields[0].shape

                        for value in _LIST_VALUES:
                            try:
                                check_options_value(shapes, value)
                            except (TypeError, ValueError):
                                continue  # not a value this field accepts

                            obj = Row(list(value))
                            wire = json.loads(json.dumps(encode(schema, obj)))
                            named = wire["f"]["$type"]
                            wanted = value_branch(shapes, value).option_id()

                            if named == wanted:
                                agreed += 1
                                assert schema.build(schema.decode(wire)) == obj
                                continue

                            misrouted += 1
                            try:
                                back = schema.build(schema.decode(wire))
                            except (TypeError, ValueError):
                                continue  # loud, which is the whole point

                            if back != obj:
                                silent += 1
                                examples.append((hints, value, back.f))

    assert agreed > 100, agreed
    assert misrouted == 0, (
        f"the codec named a branch the core would not have: {examples[:3]}")
    assert silent == 0, f"the transport rewrote a row in silence: {examples[:3]}"


def test_the_core_and_the_codec_agree_about_which_list_branch_it_is():
    """The disagreement that used to be here, stated as the agreement it became.

    There is one router now. The codec asks the core's, so the branch the file
    names and the branch validation would pick are the same answer to the same
    question rather than two answers that happened to coincide.
    """
    Row = make_dataclass("RoutingAgreementRow",
                         [("f", Union[Annotated[list[str], Min(1)], list[int]])])
    schema = struct_of(Row)
    shapes = schema.fields[0].shape

    # `[]` satisfies only the branch without `Min(1)`, and both now say so.
    assert value_branch(shapes, []).option_id() == "list[int]"
    assert encode(schema, Row([]))["f"]["$type"] == "list[int]"

    # And the row survives the trip it used to be refused on.
    obj = Row([])
    assert schema.build(schema.decode(json.loads(json.dumps(encode(schema, obj))))) == obj


DateNamedEnum = Enum("date", [("EARLY", 1), ("LATE", 2)])  # type: ignore[misc]


def test_an_enum_named_like_date_is_refused_the_same_way(tmp_path):
    """The same collision with `date`: an enum class called `date` and the Date
    shape both report `option_id() == 'date'`."""
    Row = make_dataclass("RowWithDateNamedEnum", [("f", Union[date, DateNamedEnum])])

    with pytest.raises(ValueError, match=r"duplicate discriminator name\(s\): date"):
        store_of(Row, tmp_path)


# =============================================================================
# Named, deterministic cases the sweep leans on — documenting what the core
# actually does, so a change of mind upstream is a failing test and not a
# silent shift under the generator.
# =============================================================================

def test_a_dataclass_with_no_fields_compiles_and_round_trips(tmp_path):
    """The campaign brief says the core rejects an empty dataclass. It does not.

    `struct_of` accepts it, the transport writes `{}`, and the row survives the
    round trip. The generator therefore never needed to avoid them; this test
    is what says so.
    """
    Empty = make_dataclass("EmptyLeaf", [])
    Holder = make_dataclass("HolderOfEmpty", [("inner", Empty)])

    schema = struct_of(Holder)
    obj = Holder(Empty())

    wire = json.loads(json.dumps(encode(schema, obj)))
    assert wire == {"inner": {}}
    assert schema.build(schema.decode(wire)) == obj

    store = store_of(Holder, tmp_path, debounce=0.0)
    try:
        row_id = store.add(obj)
        assert store.get(row_id) == obj
    finally:
        store.close()


def test_notation_does_not_reach_the_transport():
    """Label, Description and Placeholder change the schema and nothing on disk."""
    Plain = make_dataclass("PlainRow", [("n", int), ("s", str)])
    Noted = make_dataclass("PlainRow", [
        ("n", Annotated[int, Placeholder("0"), Label("A number"), Description("How many")]),
        ("s", Annotated[str, Placeholder("text"), Label("Some text")]),
    ])

    plain = struct_of(Plain)
    noted = struct_of(Noted)

    assert encode(plain, Plain(3, "x")) == encode(noted, Noted(3, "x"))
    assert plain.decode({"n": 3, "s": "x"}) == noted.decode({"n": 3, "s": "x"})


def test_notation_moves_the_fingerprint():
    """...but it does move the file name, and that is worth knowing.

    `Label` and `Placeholder` are notation — nothing about them changes what a
    row is or how it is written — yet they sit on comparable fields of the
    compiled shapes, so `fingerprint` reads them and adding one renames the
    database. Current behaviour, pinned here rather than argued about: a store
    that gains a label opens an empty file beside the old one.
    """
    Plain = make_dataclass("LabelledRow", [("n", int)])
    Noted = make_dataclass("LabelledRow", [("n", Annotated[int, Label("A number")])])
    Held = make_dataclass("LabelledRow", [("n", Annotated[int, Placeholder("0")])])

    bare = fingerprint(struct_of(Plain))
    assert fingerprint(struct_of(Noted)) != bare
    assert fingerprint(struct_of(Held)) != bare


def test_an_enum_alias_travels_as_its_canonical_name():
    """An alias is not a member of its own, and the file says the canonical name."""
    Aliased = Enum("Aliased", [("FIRST", "x"), ("SECOND", "x")])
    assert Aliased.SECOND is Aliased.FIRST

    Row = make_dataclass("AliasedRow", [("f", Aliased)])
    schema = struct_of(Row)

    obj = Row(Aliased.SECOND)
    wire = json.loads(json.dumps(encode(schema, obj)))

    assert wire == {"f": "FIRST"}
    assert schema.build(schema.decode(wire)) == obj

    # The alias name is still a reading the file may carry, and it resolves.
    assert schema.build(schema.decode({"f": "SECOND"})) == obj


def test_every_member_of_a_generated_enum_round_trips():
    """Each member of each generated enum, aliases included, through the codec."""
    for seed in range(1, 41):
        generator = Generator(seed)
        enum_cls = generator.sample_enum().hint
        Row = make_dataclass(f"EnumRow{seed}", [("f", enum_cls)])
        schema = struct_of(Row)

        for name, member in enum_cls.__members__.items():
            obj = Row(member)
            wire = json.loads(json.dumps(encode(schema, obj), ensure_ascii=False))
            assert schema.build(schema.decode(wire)) == obj, f"seed {seed}, member {name!r}"
            # A file written by hand may name the alias; it must still resolve.
            assert schema.build(schema.decode({"f": name})) == obj


def test_the_edges_of_the_calendar_and_the_clock_survive():
    """`date.min`, `date.max`, midnight and 23:59:59 are ISO text like any other."""
    Row = make_dataclass("EdgeRow", [("d", date), ("t", time)])
    schema = struct_of(Row)

    for day in (date.min, date.max, date(1970, 1, 1)):
        for moment in (time(0, 0, 0), time(23, 59, 59), time(12, 34, 56)):
            obj = Row(day, moment)
            wire = json.loads(json.dumps(encode(schema, obj)))
            assert type(wire["d"]) is str and type(wire["t"]) is str
            assert schema.build(schema.decode(wire)) == obj


def test_an_empty_list_picks_a_branch_and_stays_empty():
    """`list[A] | list[B]` cannot tell which empty list it was, and need not."""
    Row = make_dataclass("EmptyListRow", [("f", Union[list[int], list[str]])])
    schema = struct_of(Row)

    obj = Row([])
    wire = json.loads(json.dumps(encode(schema, obj)))

    assert wire["f"]["$value"] == []
    assert schema.build(schema.decode(wire)) == obj


def test_a_string_that_reads_as_a_date_stays_a_string():
    """ISO text under a `str | date` union comes back on the branch it left on."""
    Row = make_dataclass("IsoTextRow", [("f", Union[str, date])])
    schema = struct_of(Row)

    as_text = Row("2026-08-01")
    as_date = Row(date(2026, 8, 1))

    for obj in (as_text, as_date):
        wire = json.loads(json.dumps(encode(schema, obj)))
        back = schema.build(schema.decode(wire))
        assert back == obj and type(back.f) is type(obj.f)


def test_a_lone_surrogate_passes_the_codec():
    """A str the core accepts and UTF-8 cannot carry: in memory nothing objects."""
    Row = make_dataclass("SurrogateRow", [("f", str)])
    schema = struct_of(Row)

    obj = Row("\ud800")
    assert schema.build(schema.decode(json.loads(json.dumps(encode(schema, obj))))) == obj


def test_a_lone_surrogate_is_refused_before_it_becomes_a_row(tmp_path):
    """...and `add()` is where it stops.

    The core validates it as the str it is, and the codec carries it in memory
    without complaint; only the UTF-8 write cannot. The store asks that last
    question itself, so a row it could never dump never becomes a row — it used
    to be accepted, freeze every later dump, and surface at `close()` as a bare
    UnicodeEncodeError.
    """
    Row = make_dataclass("SurrogateStoreRow", [("f", str)])

    store = store_of(Row, tmp_path, debounce=0.0)

    with pytest.raises(SchemaValueError, match="cannot be written as UTF-8"):
        store.add(Row("\ud800"))

    assert store.add(Row("plain")) == 1, "the refused row spent an id"
    store.close()

    assert store.path.exists()


def test_big_integers_and_extreme_floats_survive_the_file(tmp_path):
    Row = make_dataclass("NumbersRow", [("n", int), ("x", float)])
    schema = struct_of(Row)

    cases = [
        Row(0, 0.0),
        Row(-(2 ** 63), -0.0),
        Row(10 ** 40, 1e308),
        Row(-(10 ** 40), 5e-324),
        Row(2 ** 53 + 1, 0.1 + 0.2),
    ]

    for obj in cases:
        wire = json.loads(json.dumps(encode(schema, obj)))
        assert schema.build(schema.decode(wire)) == obj

    store = store_of(Row, tmp_path, debounce=0.0)
    try:
        ids = [store.add(obj) for obj in cases]
    finally:
        store.close()

    reopened = store_of(Row, tmp_path, debounce=0.0)
    try:
        assert reopened.all() == list(zip(ids, cases))
    finally:
        reopened.close()


def test_a_unicode_field_name_is_a_json_key_like_any_other(tmp_path):
    Row = make_dataclass("UnicodeFieldRow", [("日本語", int), ("ñé", str)])
    schema = struct_of(Row)

    obj = Row(1, "ok")
    wire = json.loads(json.dumps(encode(schema, obj), ensure_ascii=False))
    assert wire == {"日本語": 1, "ñé": "ok"}
    assert schema.build(schema.decode(wire)) == obj

    store = store_of(Row, tmp_path, debounce=0.0)
    try:
        row_id = store.add(obj)
    finally:
        store.close()

    assert "日本語" in store.path.read_text(encoding="utf-8")

    reopened = store_of(Row, tmp_path, debounce=0.0)
    try:
        assert reopened.get(row_id) == obj
    finally:
        reopened.close()


def test_a_pattern_is_matched_in_full_not_searched():
    """`Pattern` is a fullmatch, so the generator only ever offers whole words."""
    expression = r"[a-z]+"
    Row = make_dataclass("PatternRow", [("f", Annotated[str, Pattern(expression)])])
    schema = struct_of(Row)

    assert re.fullmatch(expression, "abc")
    assert schema.build(schema.decode({"f": "abc"})) == Row("abc")

    with pytest.raises((TypeError, ValueError)):
        schema.build(schema.decode({"f": "abc1"}))
