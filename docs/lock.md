# The lock

Opening a store writes `Task.ef8e8a47.json.lock` beside the file, holding the
owner's PID, and keeps it open while the store is open.

A second store on the same file — in this process or any other — fails at
startup:

```text
…Task.ef8e8a47.json is owned by process 18220. A store belongs to one process;
you are probably running several workers. Run a single worker, or reach for a
database server — this is not one.
```

A lockfile left by a crash names a PID that no longer runs, and the next process
takes it over; so does one whose contents say nothing. Two processes racing for
the same orphan cannot both end up owning it: the operating system settles it,
not an agreement between readers — an open handle Windows will not unlink, an
advisory `flock` the kernel drops when a process dies on POSIX. Two different
classes in one directory are two files and two locks, and never meet.

Two live stores behave the same everywhere; what differs is a lockfile nobody is
holding. On POSIX the `flock` is the evidence, so a file naming a live process
that is not holding it — a crash plus a recycled pid — is taken over, and a
recycled pid cannot lock a path out. On Windows the pid is the evidence, so that
file is respected and the way out is to delete it by hand. Deleting a lockfile
*while* a store holds it is refused by Windows but allowed by POSIX, where the
exclusion is then lost until both processes end: delete a lockfile only when
nothing is holding it.
