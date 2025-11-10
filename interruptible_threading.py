"""
Exposes an InterruptibleThread class that raises KeyboardInterrupt when `thread.interrupt()` is called.
Requires monkey patching some stdlib functionality via `InterruptibleThread.install_patches()`.
"""
from __future__ import annotations

import ctypes
import os
import selectors
import select
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from io import IOBase


_PTSSE = ctypes.pythonapi.PyThreadState_SetAsyncExc
_PTSSE.argtypes = [ctypes.c_ulong, ctypes.py_object]
_PTSSE.restype = ctypes.c_int
_MAIN_IDENT = threading.main_thread().ident
_ORIG_SLEEP = time.sleep
_ORIG_DEFAULT_SELECTOR = selectors.DefaultSelector
_ORIG_SELECT = select.select
_ORIG_THREAD = threading.Thread


class _Lockable(type):
    def __enter__(cls: _State):
        return cls.registry_lock.__enter__()

    def __exit__(cls: _State, exc_type, exc_val, exc_tb):
        return cls.registry_lock.__exit__(exc_type, exc_val, exc_tb)


class _State(metaclass=_Lockable):
    registry_lock = threading.RLock()
    registry: dict[int, _State] = {}

    def __init__(self) -> None:
        cur_thread = threading.current_thread()
        if not isinstance(cur_thread, InterruptibleThread):
            raise TypeError(f"current thread should be of type {InterruptibleThread.__name__}")
        self.thread: InterruptibleThread = cur_thread
        self.children_tids: list[int] = []
        self.cancel_cond = threading.Condition()
        self.sleeping = False
        self.cancel_sleep_notified = False
        self.selecting = False
        self.cancel_select_notified = False
        r, w = os.pipe()
        os.set_blocking(r, False)
        self.rfd, self.wfd = r, w

    @classmethod
    def register_current_thread(cls) -> _State:
        tid = threading.get_ident()
        with cls:
            st = cls.registry.get(tid)
            if st is None:
                st = cls()
                cls.registry[tid] = st
            return st

    @classmethod
    def get_state_by_ident(cls, tid: int | None = None) -> _State | None:
        return cls.registry.get(tid or threading.get_ident())

    @classmethod
    def unregister_current_thread(cls) -> None:
        tid = threading.get_ident()
        with cls:
            st = cls.registry.pop(tid, None)
            if not st:
                return
            for fd in (st.rfd, st.wfd):
                try:
                    os.close(fd)
                except OSError:
                    pass


def _coop_sleep(secs: float) -> None:
    if threading.get_ident() == _MAIN_IDENT:
        return _ORIG_SLEEP(secs)
    st = _State.get_state_by_ident()
    if not st:
        return _ORIG_SLEEP(secs)
    with st.cancel_cond:
        st.sleeping = True
        try:
            st.cancel_cond.wait(timeout=secs)
        finally:
            st.sleeping = False
    if st.cancel_sleep_notified:
        raise KeyboardInterrupt()


class _InterruptibleSelector(_ORIG_DEFAULT_SELECTOR):
    def __init__(self) -> None:
        self._inner = _ORIG_DEFAULT_SELECTOR()
        st = self._st = _State.get_state_by_ident()
        if st:
            try:
                self._inner.register(st.rfd, selectors.EVENT_READ, data="__wakeup__")
            except KeyError:
                pass

    def __getattr__(self, item: str) -> object:
        return getattr(self._inner, item)

    def select(self, timeout: float | None = None) -> list[tuple[selectors.SelectorKey, int]]:
        if not self._st:
            return self._inner.select(timeout)
        with self._st.cancel_cond:
            self._st.selecting = True
        events = []
        try:
            events = self._inner.select(timeout)
        finally:
            with self._st.cancel_cond:
                self._st.selecting = False
            select_cancelled = any(key.data == "__wakup__" for key, _ in events) or self._st.cancel_select_notified
        out = []
        for key, mask in events:
            if key.data == "__wakeup__":
                try:
                    while True:
                        if os.read(key.fd, 1024) == b"":
                            break
                except (BlockingIOError, OSError):
                    pass
                continue
            out.append((key, mask))
        if select_cancelled:
            raise KeyboardInterrupt()
        return out


def _fileno(x: IOBase | int) -> int:
    try:
        return x.fileno()
    except AttributeError:
        return int(x)  # type: ignore


def _patched_select(
        rlist: list[IOBase | int], wlist: list[IOBase | int], xlist: list[IOBase | int], timeout: float | None = None
) -> tuple[list[IOBase | int], list[IOBase | int], list[IOBase | int]]:
    if threading.get_ident() == _MAIN_IDENT:
        return _ORIG_SELECT(rlist, wlist, xlist, timeout)

    st = _State.get_state_by_ident()
    if not st:
        return _ORIG_SELECT(rlist, wlist, xlist, timeout)

    rfd = st.rfd

    def to_fd_list(lst: list[IOBase | int]) -> list[tuple[int, IOBase | int]]:
        return [(_fileno(o), o) for o in lst]

    rmap = to_fd_list(rlist)
    wmap = to_fd_list(wlist)
    xmap = to_fd_list(xlist)

    rfd_list = [fd for fd, _ in rmap]
    wfd_list = [fd for fd, _ in wmap]
    xfd_list = [fd for fd, _ in xmap]

    if rfd not in rfd_list:
        rfd_list.append(rfd)

    rr = []
    with st.cancel_cond:
        st.selecting = True
    try:
        rr, ww, xx = _ORIG_SELECT(rfd_list, wfd_list, xfd_list, timeout)
    finally:
        with st.cancel_cond:
            st.selecting = False
            select_cancelled = rfd in rr or st.cancel_select_notified

    if rfd in rr:
        try:
            while True:
                if os.read(rfd, 1024) == b"":
                    break
        except (BlockingIOError, OSError):
            pass
    if select_cancelled:
        raise KeyboardInterrupt()

    def map_back(fd_list: list[IOBase], fmap: list[tuple[int, IOBase | int]]) -> list[IOBase | int]:
        fds = set(fd_list)
        return [obj for fd, obj in fmap if fd in fds]

    return (
        map_back(rr, rmap),
        map_back(ww, wmap),
        map_back(xx, xmap),
    )


class InterruptibleThread(_ORIG_THREAD):
    """Thread with built-in cooperative interrupt()."""

    _patches_installed = False

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._parent_tid = threading.get_ident()

    def run(self) -> None:
        with _State:
            _State.register_current_thread()
            parent_st = _State.get_state_by_ident(self._parent_tid)
            if parent_st is not None:
                parent_st.children_tids.append(threading.get_ident())
        try:
            super().run()
        finally:
            st = _State.get_state_by_ident(self.ident)
            if st is None:
                return
            with st.cancel_cond:
                _State.unregister_current_thread()

    def _raise_keyboard_interrupt_in_thread(self) -> None:
        rc = _PTSSE(ctypes.c_ulong(self.ident), ctypes.py_object(KeyboardInterrupt))
        if rc == 0:
            raise ValueError("no such thread")
        elif rc > 1:
            _PTSSE(ctypes.c_ulong(self.ident), None)
            raise SystemError("SetAsyncExc affected multiple threads")

    def interrupt(self, recursive: bool = False) -> None:
        st = _State.get_state_by_ident(self.ident)
        if st is None:
            raise ValueError("no such thread")
        for child_tid in (st.children_tids if recursive else []):
            child_st = _State.get_state_by_ident(child_tid)
            if child_st is None:
                continue
            child_thread = child_st.thread
            if not child_thread.is_alive():
                continue
            try:
                child_thread.interrupt(recursive=recursive)
            except ValueError:
                continue
        with st.cancel_cond:
            if _State.get_state_by_ident(self.ident) is None:
                # if this occurs, the thread has already terminated, no need to interrupt
                return
            st.cancel_sleep_notified = st.sleeping
            st.cancel_select_notified = st.selecting
            if st.cancel_sleep_notified:
                st.cancel_cond.notify()
            elif st.cancel_select_notified:
                try:
                    os.write(st.wfd, b"\x00")
                except OSError:
                    pass
            else:
                self._raise_keyboard_interrupt_in_thread()

    @classmethod
    def get_thread_cls_for_current_thread(cls, item: str) -> type[threading.Thread]:
        if item != "Thread":
            raise AttributeError("No attribute %s in module threading" % item)
        st = _State.get_state_by_ident()
        if st is None:
            return _ORIG_THREAD
        else:
            return cls

    @classmethod
    def install_patches(cls) -> None:
        if cls._patches_installed:
            raise ValueError("patches already installed")
        cls._patches_installed = True
        time.sleep = _coop_sleep  # type: ignore[assignment]
        selectors.DefaultSelector = _InterruptibleSelector  # type: ignore[assignment]
        select.select = _patched_select  # type: ignore[assignment]
        del threading.Thread
        threading.__getattr__ = cls.get_thread_cls_for_current_thread

        def __dir__():
            return list(threading.__dict__.keys()) + [_ORIG_THREAD.__name__]

        threading.__dir__ = __dir__

    @classmethod
    def uninstall_patches(cls) -> None:
        if not cls._patches_installed:
            raise ValueError("patches not installed")
        time.sleep = _ORIG_SLEEP
        select.select = _ORIG_SELECT
        selectors.DefaultSelector = _ORIG_DEFAULT_SELECTOR
        threading.Thread = _ORIG_THREAD
        del threading.__getattr__  # type: ignore
        del threading.__dir__  # type: ignore

