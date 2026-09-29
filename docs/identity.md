# The identity

The file is `{ClassName}.{fingerprint}.json`. The fingerprint is eight hex of
the sha256 of the compiled schema, rendered as deterministic text.

**Everything in the schema counts** — types, limits, defaults, the order of the
fields, and the notation atoms (`Label`, `Description`, `Placeholder`, …) that
change nothing a file can carry. No exceptions and no small print: a contract
without special cases fits in one sentence and is tested in three lines.

Change the class and you get a new, empty database. The old file stays where it
was, untouched and readable, named for the schema that wrote it. Moving rows
across is a script over readable JSON — a minute of work for you or an agent —
and it is deliberately outside this library, because a migration that runs
automatically is a migration nobody read.
