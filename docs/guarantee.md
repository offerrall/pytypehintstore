# The guarantee

If a store opens, every row it holds was rebuilt through the schema you are
holding right now. There is no such thing as a row from an older version: those
live in an older file, under an older fingerprint.

`add` and `put` accept a row by making the round trip it will make anyway — the
instance is encoded to its transport form and rebuilt through the schema — so
what the store keeps always came out of the constructor and is known to survive
the file.

The store has no second validation system. A row that does not validate fails
with the core's own error, word for word:

```python
tasks.add(Task(title=""))
# SchemaValueError: title: too short: 0 chars, minimum 1
```

That is the same line `struct_of(Task).build({"title": ""})` produces. Only one
message belongs to the store itself, because only the store can be asked this:

```python
tasks.add("not a task")
# SchemaTypeError: expected Task, got str
```

The instance the store keeps is not the one you passed; mutating yours
afterwards changes nothing inside.

One question the store asks on its own behalf, after the core has spoken: a row
has to fit in a UTF-8 file. A lone surrogate is a `str` Python allows and no
encoder can write, so it is refused at `add` rather than discovered at `close`:

```python
tasks.add(Task(title="\ud800"))
# SchemaValueError: cannot be written as UTF-8: 'utf-8' codec can't encode …
```

**The property, stated plainly:** if the core compiles your schema, the store
persists it without loss or fails loudly — at `add`, or at `store_of` before a
lockfile is taken. No exception, and no asterisk.
