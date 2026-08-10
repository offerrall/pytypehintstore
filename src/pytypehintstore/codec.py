"""The transport form of a row: ISO text for a date, the member name for an
enum, an object for a nested dataclass.

Writing is the store's, because the core offers no operation for it: a schema
describes and validates, and turning a live instance into the tree that goes on
disk is the file's business. Reading back is not the store's and no longer lives
here — `schema.decode` is the core's own inverse of this, published in 1.0.0,
and a second reading maintained beside it would be a second dialect.

Nothing here validates. What this writes, the core judges.
"""

from pytypehint import Date, EnumShape, List, Struct, Time

# The core's own router, which is not public API. The dependency is pinned to an
# exact version because of this line: it is the one place a move in the core
# reaches the store silently, and `test_codec_router_contract` is what turns that
# into a failing test instead of a wrong file.
from pytypehint.validation import value_branch

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


def encode(struct, instance) -> dict:
    return {f.name: _encode_options(f.shape, getattr(instance, f.name))
            for f in struct.fields}


def _encode_options(shapes, value):
    shape = value_branch(shapes, value)

    if shape is None:
        # A field mutated by hand: no option owns its Python type, or none
        # accepts the value. It travels intact for the core to judge. Where the
        # transport is wider than the type — ISO text under a Date — the round
        # trip rewrites the field instead of refusing it; see README, "Known
        # limits".
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
