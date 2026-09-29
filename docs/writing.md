# Writing

A write mutates the dict at once and marks the store unwritten. The writer
thread dumps it `debounce` seconds later, and every write pushes that deadline
back, so a burst costs one dump.

The dump is atomic: the text goes to a `.part` file, is flushed and `fsync`ed,
then replaces the real file with `os.replace`. The file goes from one whole JSON
to the next, never a half of either. A failed dump leaves nothing of itself
behind, keeps the state unwritten, and is tried again — it never kills the
writer and never passes for a success. Before each replace the previous file is
copied aside with a nanosecond stamp, and copies beyond `keep` are pruned. Each
database prunes its own copies: the fingerprint is in the name.

`close()` stops the writer, dumps what is pending, and releases the lock. When it
returns the file is on disk — or it raised, and it raised at every caller that
was closing the same store. The lock is released either way.

If you never call `close()`, an `atexit` hook does it on a normal interpreter
exit. It does not run on `os._exit`, on a hard kill, or on a power cut; what is
on disk then is the last dump that landed, whole.
