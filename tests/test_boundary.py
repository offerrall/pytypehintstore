"""The library reads no private of the core.

The store defers all validation to `pytypehint`, and it may only do so through
what the core publishes: a name with a leading underscore belongs to whoever
wrote it, and reading one would tie this library to an implementation the core
is free to change without warning.

This is a lint rather than a behaviour, and it lives in the suite because a
one-off grep is a claim while a test is a guarantee.
"""

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "src" / "pytypehintstore"

MODULES = sorted(PACKAGE.glob("*.py"))


def private_reads(tree):
    """Every `x._y` in the tree whose owner is not `self`.

    Dunders are Python's own vocabulary — `__name__`, `__members__` — and say
    nothing about the core's internals, so they are not what this looks for.
    """
    found = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue

        name = node.attr

        if not name.startswith("_") or name.startswith("__"):
            continue

        owner = node.value

        if isinstance(owner, ast.Name) and owner.id == "self":
            continue

        found.append(f"{ast.unparse(owner)}.{name}")

    return found


def test_the_package_is_not_empty_so_this_test_can_fail():
    """A scan of nothing passes; this says there was something to scan."""
    assert {path.name for path in MODULES} >= {
        "store.py", "codec.py", "lockfile.py", "errors.py", "fingerprint.py"}


@pytest.mark.parametrize("module", MODULES, ids=lambda path: path.name)
def test_a_module_reads_no_private_attribute_of_anything_but_itself(module):
    tree = ast.parse(module.read_text(encoding="utf-8"))

    assert private_reads(tree) == []


def test_the_scan_catches_a_private_read():
    """The guard has to be able to fail: a passing lint that cannot fail is a
    comment."""
    assert private_reads(ast.parse("shape._check(value)")) == ["shape._check"]
    assert private_reads(ast.parse("self._rows[1]")) == []
    assert private_reads(ast.parse("value.__name__")) == []
