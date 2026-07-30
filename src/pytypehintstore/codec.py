"""The transport form of a row: ISO text for a date, the member name for an
enum, an object for a nested dataclass.

Nothing here validates. `decode` converts only where the shape is the single
possible reading; anything else travels intact for the core to reject.
"""

from datetime import date, time

from pytypehint import Date, EnumShape, Float, Int, List, Str, Struct, Time

# Reserved keys of the discriminated transport. A field name is an identifier,
# so it can never collide with either.
_TYPE = "$type"
_VALUE = "$value"


# The JSON type a value is written as: a date, a time and an enum member all
# arrive as text and compete with a str. This decides whether an option needs
# naming on the wire.
def _wire_type(shape):
    if type(shape) is Struct:
        return dict

    if type(shape) in (Date, Time, EnumShape):
        return str

    return shape.pytype


# The type the core routes on, once decode has turned text back into values:
# this decides whether the core still wants the wrapper.
def _core_type(shape):
    return dict if type(shape) is Struct else shape.pytype


def _field(fields, name):
    return next((f for f in fields if f.name == name), None)


# Two lists share a Python type and differ only in what they hold, so
# list[str] and list[int] are settled by reading their items.
def _branch_of(shapes, value):
    candidates = [s for s in shapes if type(value) is s.pytype]

    if len(candidates) < 2:
        return candidates[0] if candidates else None

    return next((s for s in candidates if _accepts(s, value)), candidates[0])


def _accepts(shape, value) -> bool:
    if type(shape) is not List:
        return True

    for item in value:
        branch = _branch_of(shape.item, item)

        if branch is None or not _accepts(branch, item):
            return False

    return True


def encode(struct, instance) -> dict:
    return {f.name: _encode_options(f.shape, getattr(instance, f.name))
            for f in struct.fields}


def _encode_options(shapes, value):
    shape = _branch_of(shapes, value)

    if shape is None:
        # A field mutated by hand: no option owns its Python type. It travels
        # intact for the core to judge. Where the transport is wider than the
        # type — ISO text under a Date — the round trip rewrites the field
        # instead of refusing it; see README, "Known limits".
        return value

    written = _encode_value(shape, value)

    # The core names a dataclass variant inside the object itself.
    if type(shape) is Struct:
        if sum(1 for s in shapes if type(s) is Struct) > 1:
            return {_TYPE: shape.cls.__name__, **written}

        return written

    # One option per JSON type needs no discriminator; when several share one,
    # the option gets named.
    if sum(1 for s in shapes if _wire_type(s) is _wire_type(shape)) > 1:
        return {_TYPE: shape.option_id(), _VALUE: written}

    return written


def _encode_value(shape, value):
    if type(shape) is Struct:
        return encode(shape, value)

    if type(shape) is EnumShape:
        # The name, not the value: the stable, readable half of a member.
        return value.name

    if type(shape) in (Date, Time):
        return value.isoformat()

    if type(shape) is List:
        return [_encode_options(shape.item, item) for item in value]

    return value


def decode(struct, data):
    if type(data) is not dict:
        return data

    result = {}

    for key, value in data.items():
        field = _field(struct.fields, key)

        # An unknown key travels intact; rejecting it is build()'s job.
        result[key] = (value if field is None
                       else _decode_options(field.shape, value))

    return result


def _decode_options(shapes, value):
    # A None is a value, not a path to descend; an omitted key never gets here
    # at all and takes its default.
    if value is None:
        return None

    if type(value) is dict:
        return _decode_dict(shapes, value)

    if type(value) is list:
        return _decode_list(shapes, value)

    if type(value) is str:
        return _decode_str(shapes, value)

    # `type(value) is int` excludes bool, so a JSON true is never a number here
    # despite bool subclassing int. Coerce only where Float is the single
    # numeric reading.
    if type(value) is int:
        if (any(type(s) is Float for s in shapes)
                and not any(type(s) is Int for s in shapes)):
            return float(value)

    return value


def _decode_dict(shapes, value):
    if _TYPE in value and _VALUE in value:
        # The wrapper is those two keys and nothing else. A key beside them, or
        # a $value that is itself a wrapper, is a hand edit the core refuses —
        # and reading it here would erase it from the next dump without a word.
        if len(value) > 2 or (type(value[_VALUE]) is dict and _TYPE in value[_VALUE]):
            return value

        # A Struct never travels this way, and its option_id is a bare class
        # name an enum is allowed to share: reading one here would hand a str
        # to a dataclass branch and lose the enum.
        shape = next((s for s in shapes if type(s) is not Struct
                      and s.option_id() == value[_TYPE]), None)

        if shape is None:
            return value

        inner = _decode_options((shape,), value[_VALUE])

        # The text did not read as the option the file names. The wrapper is
        # what says so; consuming it would file a broken date under str.
        if type(inner) is not _core_type(shape):
            return {_TYPE: value[_TYPE], _VALUE: inner}

        # The core wants the wrapper only where two options share a Python
        # type. A collision that was the wire's alone is already resolved.
        if sum(1 for s in shapes if _core_type(s) is _core_type(shape)) > 1:
            return {_TYPE: value[_TYPE], _VALUE: inner}

        return inner

    if _TYPE in value:
        struct = next((s for s in shapes if type(s) is Struct
                       and s.cls.__name__ == value[_TYPE]), None)

        if struct is None:
            return value

        inner = {k: v for k, v in value.items() if k != _TYPE}
        return {_TYPE: value[_TYPE], **decode(struct, inner)}

    structs = [s for s in shapes if type(s) is Struct]

    if len(structs) == 1:
        return decode(structs[0], value)

    return value


def _decode_list(shapes, value):
    lists = [s for s in shapes if type(s) is List]

    # A union of lists collides on the transport type and travels wrapped, so
    # a bare list means a single List branch.
    if len(lists) == 1:
        return [_decode_options(lists[0].item, item) for item in value]

    return value


def _decode_str(shapes, value):
    # A Str reading keeps a string a string. Otherwise convert only where a
    # single Date, Time or enum is the one reading.
    if any(type(s) is Str for s in shapes):
        return value

    dates = [s for s in shapes if type(s) is Date]
    times = [s for s in shapes if type(s) is Time]
    enums = [s for s in shapes if type(s) is EnumShape]

    if (bool(dates) + bool(times) + bool(enums)) != 1:
        return value

    if len(dates) == 1:
        return _convert(date.fromisoformat, value)

    if len(times) == 1:
        return _convert(time.fromisoformat, value)

    if len(enums) == 1:
        # __members__ resolves an alias to its canonical member, and keeps a
        # mixin out of the way: a StrEnum inherits str.__getitem__, which
        # cls[name] would reach instead of the member lookup.
        return _convert(enums[0].cls.__members__.__getitem__, value)

    return value


def _convert(fn, value):
    try:
        return fn(value)
    except (KeyError, ValueError):
        # Not a reading decode can make: build() rejects it.
        return value
