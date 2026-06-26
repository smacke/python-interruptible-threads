# Python Interruptible Threads

Cooperative, prompt thread interruption for CPython. Call `thread.interrupt()` and a
target thread raises an exception (`ThreadInterrupted` by default) — even when it is
parked in `time.sleep`, `select`, `asyncio.sleep`, an `Event`/`Queue`/`Condition` wait,
or a (helper-wrapped) blocking socket read.

It uses `ctypes` to reach `PyThreadState_SetAsyncExc` and patches a curated set of
stdlib blocking primitives, so it works only on **CPython** and **POSIX (Linux / Darwin)**.

```python
from interruptible_threading import InterruptibleThread, ThreadInterrupted
import time

InterruptibleThread.install_patches()

def sleep_forever():
    try:
        while True:
            time.sleep(10)
    except ThreadInterrupted:
        print("interrupted")

t = InterruptibleThread(target=sleep_forever, daemon=True)
t.start()
...
t.interrupt()   # output: "interrupted"
```

> **Back-compat:** earlier versions raised `KeyboardInterrupt`. To keep that behavior,
> `InterruptibleThread.install_patches(interrupt_exc=KeyboardInterrupt)`, or
> `install_patches(legacy_keyboardinterrupt=True)` to deliver an exception caught by
> *both* `ThreadInterrupted` and `KeyboardInterrupt` handlers.

## How it works

Pure-Python code is interrupted with `PyThreadState_SetAsyncExc`, an async exception
that fires at the next bytecode boundary. That cannot break a thread sitting in a
C-level blocking call, so `install_patches()` replaces `time.sleep`,
`selectors.DefaultSelector`, `select.select`, and `threading.Condition.wait` (which
also covers `Event.wait` and `queue.Queue`) with versions that wake promptly via a
per-thread self-pipe / chunked polling.

The design rests on a single **durable per-thread "interrupt pending" flag**.
`interrupt()` sets the flag first (under one lock), then issues a wakeup nudge; every
blocking primitive checks the flag *before* parking and *again* after waking. This
removes the time-of-check/time-of-use races inherent in choosing a delivery path from
transient state, and makes interrupts maskable and pollable.

### Why not `signal.pthread_kill`?

`pthread_kill` can unblock a syscall but cannot deliver an *exception* to a worker
thread: CPython runs Python-level signal handlers only on the main thread, and PEP 475's
EINTR auto-retry loops call `PyErr_CheckSignals()` — a no-op off the main thread — without
consulting `tstate->async_exc`, so the syscall is transparently retried. The self-pipe +
cooperative-primitive approach is the only way to get prompt, exception-bearing
interruption of worker threads on CPython.

## API

| Name | Purpose |
| --- | --- |
| `InterruptibleThread(...)` | `threading.Thread` subclass with `.interrupt(recursive=False)`. |
| `InterruptibleThread.install_patches(interrupt_exc=ThreadInterrupted, legacy_keyboardinterrupt=False, monkeypatch_socket=False)` | Install the stdlib patches (process-wide). |
| `InterruptibleThread.uninstall_patches()` | Restore the originals. |
| `InterruptibleThread.run_interruptible(coro)` | Run a coroutine via `asyncio.run` with clean, cancellation-based interruption. |
| `ThreadInterrupted` | Default interrupt exception (subclass of `BaseException`). |
| `is_interrupted(thread=None)` | Whether an interrupt is pending (non-consuming). |
| `clear_interrupt()` | Consume a pending interrupt without raising; returns whether one was pending. |
| `check_interrupt()` / `interruptible_checkpoint()` | Raise if pending and not masked — for CPU-bound loops. |
| `periodic_checkpoint(every=N)` | Context manager yielding a `.tick()` that checkpoints every N calls. |
| `critical_section()` / `interrupts_disabled()` | Defer delivery during cleanup; deliver on exit. |
| `interruptible_recv/send/accept(sock, ...)` | Interruptible blocking socket ops. |
| `set_poll_interval(seconds)` | Tune the chunked-poll wakeup latency (default 50 ms). |

### Masking critical sections

```python
from interruptible_threading import critical_section

with critical_section():
    commit()          # interrupts that arrive here are deferred...
release_resources()   # ...and delivered exactly once when the block exits
```

### CPU-bound loops

```python
from interruptible_threading import periodic_checkpoint

with periodic_checkpoint(every=1000) as ck:
    for row in huge_iterable:
        ck.tick()     # raises ThreadInterrupted promptly once interrupted
        crunch(row)
```

### asyncio

```python
import asyncio
from interruptible_threading import InterruptibleThread

def worker():
    try:
        InterruptibleThread.run_interruptible(main_coro())
    except ThreadInterrupted:
        print("cancelled cleanly")
```

`run_interruptible` cancels the loop's tasks via `call_soon_threadsafe` (proper
`finally` / `async with` unwind) instead of injecting an exception into the selector.

## Limitations

- **CPython + POSIX only.** Relies on `PyThreadState_SetAsyncExc`, `os.pipe`, and `select`.
- **Uncovered blocking calls** stay blocked until they return: synchronous regular-file
  disk I/O (`open().read()`, `os.read` on files), raw blocking `socket.recv` (use the
  `interruptible_recv` helpers or `monkeypatch_socket=True`), `os.waitpid`, and
  `Lock.acquire` on a builtin lock. The pending flag is honored at the next patched
  primitive / checkpoint, but the in-progress call is not broken.
- **C extensions that release the GIL and never re-enter Python** (heavy NumPy kernels,
  compiled crypto) cannot be preempted; only cooperative `check_interrupt()` helps.
- **Chunked-poll primitives** (`Event`/`Queue`/`Condition`) have up to `_POLL_INTERVAL`
  (default 50 ms) latency, tunable via `set_poll_interval`.
- **Async injection lands at an arbitrary bytecode boundary** and is un-recallable, so
  `critical_section()` is airtight only for the flag-driven paths; prefer
  checkpoints/blocking primitives inside code that must not be interrupted mid-cleanup.
- **Catch-and-continue clears the flag explicitly.** The interrupt-pending flag is
  durable (so an interrupt is never lost if the target parks in a blocking call before
  async injection can fire). Consequently, if you *catch* `ThreadInterrupted` and want to
  keep running, call `clear_interrupt()` — otherwise the next `check_interrupt()` /
  blocking primitive re-raises. This mirrors Java's `Thread.interrupted()`.
- **`install_patches()` mutates global stdlib state.** Code that captured references
  before install (e.g. `from time import sleep`) keeps the originals. Not for libraries
  to call implicitly.
- **The main thread is intentionally not interruptible** by this mechanism, preserving
  real Ctrl-C / `KeyboardInterrupt` semantics.

## Development

```sh
pip install -e .[test]   # or: make devdeps
make check               # blackcheck + ruff + mypy + pytest (with coverage)
pytest                   # just the tests
```
