# The lock

Opening a store writes `Task.a50534e4.json.lock` beside the file, holding the
owner's PID, and keeps it open while the store is open.

A second store on the same file — in this process or any other — fails at
startup:

```
…Task.a50534e4.json is owned by process 18220. A store belongs to one process;
you are probably running several workers. Run a single worker, or reach for a
database server — this is not one.
```

A lockfile left by a crash names a PID that no longer runs, and the next process
takes it over; so does one whose contents say nothing. Two processes racing for
the same orphan cannot both end up owning it: the operating system settles it,
not an agreement between readers — an open handle Windows will not unlink, an
advisory `flock` the kernel drops when a process dies on POSIX. Two different
classes in one directory are two files and two locks, and never meet.
