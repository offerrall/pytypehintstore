# The file

The file carries the transport form, the same shapes an HTTP client would send:

```json
{
  "v": 1,
  "next_id": 3,
  "rows": {
    "1": {
      "title": "Buy milk",
      "due": "2026-08-01",
      "priority": "HIGH",
      "note": { "body": "oat" }
    }
  }
}
```

A date or a time is ISO text, an enum is its **member name**, a nested dataclass
is an object, a list is a list. Never a `repr` of a Python value.

Edit it, and the next load either accepts your edit or says exactly what is
wrong with it. Every one of these is a `StoreLoadError` raised by `store_of`,
before a single row is served:

| What is broken | What you get |
|---|---|
| The file does not exist | An empty store — not an error |
| Not UTF-8 | `…json: not valid UTF-8: …` |
| Not JSON | `…json: not valid JSON: Expecting property name … (char 1)` |
| The root is not an object | `…json: expected an object, got list` |
| `v` is not the format version | `…json: unknown format version 2, expected 1` |
| `rows` is not an object | `…json: rows must be an object, got list` |
| A row id is not an integer | `…json: row id '--5' is not an integer` |
| Two ids are the same integer (`"1"`, `"01"`) | `…json: row id '01' is already taken by another key` |
| A row does not validate | `…json: row 1: note: body: too short: 0 chars, minimum 1` |

The last one is the core speaking, prefixed with the row it happened in. A row
missing a field reports exactly that — `row 1: missing key(s): title` — and
nothing more: there is no advice to add a default, because that would fork the
database rather than fix the row.

`next_id` is a hint, not an authority: if the file names one that would collide
with a row it also carries, the highest id present wins.

## The `$type`/`$value` wrapper

Most values need no help: the JSON type says which option of a union they belong
to. The wrapper appears only when two options land on the same JSON type, and it
survives decoding only where the core still needs it.

```python
when: str | date        # {"$type": "date", "$value": "2026-08-01"}
terms: list[str] | list[int]   # {"$type": "list[int]", "$value": [1, 2]}
```

Both are text and both are lists, so in each case the option gets named. On the
way back, the first wrapper is consumed — the core tells `str` and `date` apart
by Python type — and the second is kept, because to the core both options are
`list` and only the name separates them. Dataclasses in a union name the variant
inside the object instead: `{"$type": "Square", "side": 2}`.

Anything the codec cannot read as one single thing travels intact, so the error
you see is the core's, with its path and its words.

Which option gets named is not the store's opinion. Writing asks
`pytypehint.validation.value_branch` — the core's own router, the one validation
itself uses — so the branch the file names and the branch the schema would pick
are the same answer to the same question. Reading back is the core's outright:
`schema.decode`, published in 1.0.0. That router is internal to the core and
carries no public promise, which is why the dependency is pinned to an exact
version rather than a floor.
