"""Tests for interruptible_threading.

Patches mutate global stdlib state, so each test installs/uninstalls via the
``patched`` fixture (function-scoped, try/finally) to stay isolated.
"""
from __future__ import annotations

import os
import queue
import socket
import sys
import threading
import time

import pytest

import interruptible_threading as it
from interruptible_threading import (
    InterruptibleThread,
    ThreadInterrupted,
    check_interrupt,
    clear_interrupt,
    critical_section,
    interruptible_recv,
    is_interrupted,
)

pytestmark = pytest.mark.timeout(30)

# Real (unpatched) sleep for use by the main test thread while patches are live.
_REAL_SLEEP = time.sleep


@pytest.fixture
def patched():
    InterruptibleThread.install_patches()
    try:
        yield
    finally:
        InterruptibleThread.uninstall_patches()


class Result:
    """Captures a worker's outcome and exposes lifecycle events."""

    def __init__(self) -> None:
        self.exc: BaseException | None = None
        self.value = None
        self.started = threading.Event()
        self.done = threading.Event()


def run_worker(fn, *, daemon=True) -> tuple[InterruptibleThread, Result]:
    res = Result()

    def target():
        res.started.set()
        try:
            res.value = fn(res)
        except BaseException as e:  # noqa: BLE001 - we want to capture everything
            res.exc = e
        finally:
            res.done.set()

    t = InterruptibleThread(target=target, daemon=daemon)
    t.start()
    res.started.wait(5)
    return t, res


def spin(seconds: float) -> None:
    """Busy-wait (so async injection has bytecode boundaries to fire on)."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        pass


# --------------------------------------------------------------------------- #


def test_async_injection_breaks_pure_python_loop(patched):
    def fn(res):
        while True:
            pass

    t, res = run_worker(fn)
    _REAL_SLEEP(0.05)
    t.interrupt()
    assert res.done.wait(2), "worker did not stop"
    assert isinstance(res.exc, ThreadInterrupted)


def test_sleep_interrupted_promptly(patched):
    def fn(res):
        time.sleep(100)

    t, res = run_worker(fn)
    _REAL_SLEEP(0.05)
    start = time.monotonic()
    t.interrupt()
    assert res.done.wait(2)
    assert isinstance(res.exc, ThreadInterrupted)
    assert time.monotonic() - start < 1.0


def test_sleep_interrupt_race_stress(patched):
    import random

    for _ in range(150):

        def fn(res):
            time.sleep(5)

        t, res = run_worker(fn)
        spin(random.uniform(0.0, 0.002))
        t.interrupt()
        assert res.done.wait(2), "worker hung on interrupt"
        assert isinstance(res.exc, ThreadInterrupted)
        t.join(2)
        assert not t.is_alive()


def test_select_interrupted(patched):
    import select

    r, w = os.pipe()
    try:

        def fn(res):
            select.select([r], [], [])

        t, res = run_worker(fn)
        _REAL_SLEEP(0.05)
        t.interrupt()
        assert res.done.wait(2)
        assert isinstance(res.exc, ThreadInterrupted)
    finally:
        os.close(r)
        os.close(w)


def test_asyncio_clean_cancellation(patched):
    def fn(res):
        import asyncio

        InterruptibleThread.run_interruptible(asyncio.sleep(100))

    t, res = run_worker(fn)
    _REAL_SLEEP(0.1)
    start = time.monotonic()
    t.interrupt()
    assert res.done.wait(3)
    assert isinstance(res.exc, ThreadInterrupted)
    assert time.monotonic() - start < 2.0


def test_recursive_interrupt(patched):
    def parent_fn(res):
        kids, kres = [], []
        for _ in range(2):
            kr = Result()

            def ktarget(kr=kr):
                kr.started.set()
                try:
                    time.sleep(100)
                except BaseException as e:  # noqa: BLE001
                    kr.exc = e
                finally:
                    kr.done.set()

            kt = InterruptibleThread(target=ktarget, daemon=True)
            kt.start()
            kr.started.wait(2)
            kids.append(kt)
            kres.append(kr)
        res.kids = kids
        res.kres = kres
        time.sleep(100)

    t, res = run_worker(parent_fn)
    while not getattr(res, "kres", None):
        _REAL_SLEEP(0.01)
    _REAL_SLEEP(0.1)
    t.interrupt(recursive=True)
    assert res.done.wait(3)
    assert isinstance(res.exc, ThreadInterrupted)
    for kr in res.kres:
        assert kr.done.wait(2)
        assert isinstance(kr.exc, ThreadInterrupted)


def test_child_removed_from_parent_after_exit(patched):
    release = threading.Event()

    def parent_fn(res):
        kr = Result()

        def ktarget():
            kr.started.set()
            # exits immediately

        kt = InterruptibleThread(target=ktarget, daemon=True)
        kt.start()
        kt.join(2)
        res.child_tid = kt.ident
        res.child_done = True
        # Stay alive until released so the parent's _State is still inspectable.
        while not release.is_set():
            spin(0.01)

    t, res = run_worker(parent_fn)
    while not getattr(res, "child_done", False):
        _REAL_SLEEP(0.01)
    _REAL_SLEEP(0.05)
    parent_st = it._State.get_state_by_ident(t.ident)
    assert parent_st is not None
    with parent_st.cancel_cond:
        assert res.child_tid not in parent_st.children
    # The exited child is fully unregistered.
    assert it._State.get_state_by_ident(res.child_tid) is None
    release.set()
    t.join(2)


def test_masking_defers_until_exit(patched):
    in_mask = threading.Event()

    def fn(res):
        with critical_section():
            in_mask.set()
            spin(0.4)
            res.pending_in_mask = is_interrupted()
        res.after_mask = True  # should not be reached

    t, res = run_worker(fn)
    assert in_mask.wait(2)
    t.interrupt()
    assert res.done.wait(3)
    assert isinstance(res.exc, ThreadInterrupted)
    assert res.pending_in_mask is True
    assert not getattr(res, "after_mask", False)


def test_nested_masks_raise_only_at_outermost_exit(patched):
    in_mask = threading.Event()

    def fn(res):
        with critical_section():
            with critical_section():
                in_mask.set()
                spin(0.3)
            res.between = True
            res.between_pending = is_interrupted()
            spin(0.05)
        res.after = True  # should not be reached

    t, res = run_worker(fn)
    assert in_mask.wait(2)
    t.interrupt()
    assert res.done.wait(3)
    assert isinstance(res.exc, ThreadInterrupted)
    assert res.between is True
    assert res.between_pending is True
    assert not getattr(res, "after", False)


def test_check_interrupt_in_cpu_loop(patched):
    def fn(res):
        while True:
            check_interrupt()

    t, res = run_worker(fn)
    _REAL_SLEEP(0.05)
    t.interrupt()
    assert res.done.wait(2)
    assert isinstance(res.exc, ThreadInterrupted)


def test_clear_interrupt(patched):
    # clear_interrupt() is meaningful while masked: the flag is set but no async
    # exception is armed, so the thread can observe and swallow it.
    in_mask = threading.Event()

    def fn(res):
        with critical_section():
            in_mask.set()
            while not is_interrupted():
                spin(0.01)
            res.was_interrupted = clear_interrupt()
        res.still_pending = is_interrupted()
        res.after = True

    t, res = run_worker(fn)
    assert in_mask.wait(2)
    t.interrupt()
    assert res.done.wait(2)
    assert res.was_interrupted is True
    assert res.still_pending is False
    assert getattr(res, "after", False) is True
    # Cleared cleanly, so no exception escaped.
    assert res.exc is None


def test_caught_async_interrupt_not_redelivered(patched):
    def fn(res):
        try:
            while True:
                pass
        except ThreadInterrupted:
            res.caught = True
        # The flag was consumed by delivery; further checkpoints must not re-raise.
        for _ in range(10000):
            check_interrupt()
        res.no_redeliver = True

    t, res = run_worker(fn)
    _REAL_SLEEP(0.05)
    t.interrupt()
    assert res.done.wait(2)
    assert getattr(res, "caught", False) is True
    assert getattr(res, "no_redeliver", False) is True
    assert res.exc is None


@pytest.mark.parametrize(
    "block",
    [
        pytest.param(lambda: threading.Event().wait(), id="event"),
        pytest.param(lambda: queue.Queue().get(), id="queue"),
        pytest.param(lambda: _cond_wait(), id="condition"),
    ],
)
def test_chunked_primitives_interrupted(patched, block):
    def fn(res):
        block()

    t, res = run_worker(fn)
    _REAL_SLEEP(0.05)
    start = time.monotonic()
    t.interrupt()
    assert res.done.wait(2)
    assert isinstance(res.exc, ThreadInterrupted)
    assert time.monotonic() - start < 1.0


def _cond_wait():
    c = threading.Condition()
    with c:
        c.wait()


def test_interruptible_recv(patched):
    a, b = socket.socketpair()
    prev_timeout = a.gettimeout()
    try:

        def fn(res):
            interruptible_recv(a, 100)

        t, res = run_worker(fn)
        _REAL_SLEEP(0.05)
        t.interrupt()
        assert res.done.wait(2)
        assert isinstance(res.exc, ThreadInterrupted)
        assert a.gettimeout() == prev_timeout
    finally:
        a.close()
        b.close()


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc")
def test_lazy_pipe_no_fd_growth(patched):
    def count_fds():
        return len(os.listdir("/proc/self/fd"))

    before = count_fds()
    threads = []
    for _ in range(40):

        def fn(res):
            time.sleep(100)  # uses Condition, not the self-pipe

        t, res = run_worker(fn)
        threads.append((t, res))
    after_spawn = count_fds()
    # Sleeping threads must not each allocate a 2-fd self-pipe.
    assert after_spawn - before < 40
    for t, _ in threads:
        t.interrupt()
    for t, res in threads:
        assert res.done.wait(2)
        t.join(2)


def test_legacy_keyboardinterrupt_mode():
    InterruptibleThread.install_patches(legacy_keyboardinterrupt=True)
    try:

        def fn(res):
            try:
                time.sleep(100)
            except KeyboardInterrupt:
                res.caught_ki = True
                raise

        t, res = run_worker(fn)
        _REAL_SLEEP(0.05)
        t.interrupt()
        assert res.done.wait(2)
        assert getattr(res, "caught_ki", False) is True
        assert isinstance(res.exc, KeyboardInterrupt)
        assert isinstance(res.exc, ThreadInterrupted)
    finally:
        InterruptibleThread.uninstall_patches()


def test_interrupt_finished_thread_raises_value_error(patched):
    def fn(res):
        return 42

    t, res = run_worker(fn)
    assert res.done.wait(2)
    t.join(2)
    with pytest.raises(ValueError):
        t.interrupt()


def test_main_thread_checkpoint_is_noop(patched):
    # No interruptible state on the main thread; must not raise.
    check_interrupt()
    assert is_interrupted() is False


def test_patch_round_trip():
    assert threading.Thread is it._ORIG_THREAD
    InterruptibleThread.install_patches()
    try:
        with pytest.raises(ValueError):
            InterruptibleThread.install_patches()
    finally:
        InterruptibleThread.uninstall_patches()
    assert threading.Thread is it._ORIG_THREAD
    assert time.sleep is it._ORIG_SLEEP
    assert threading.Condition.wait is it._ORIG_COND_WAIT


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
