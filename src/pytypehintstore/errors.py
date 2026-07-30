"""What a store raises on its own behalf.

A validation failure is not one of these: `add` and `put` let the core's
SchemaTypeError / SchemaValueError travel out untouched, so the caller sees the
same line pytypehint would have produced anywhere else. These three cover what
only a store can go wrong at: the file and the lock.
"""


class StoreError(Exception):
    """Base of every failure a store reports for itself."""


class StoreLockedError(StoreError):
    """Another live process already owns this file."""


class StoreLoadError(StoreError):
    """The file on disk could not be read back into rows."""
