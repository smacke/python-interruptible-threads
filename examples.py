from __future__ import annotations

import socket
import time

from interruptible_threading import (
    InterruptibleThread,
    ThreadInterrupted,
    critical_section,
    interruptible_recv,
    periodic_checkpoint,
)


def make_forever_thread(sleep_secs: int) -> InterruptibleThread:
    def forever():
        try:
            while True:
                time.sleep(sleep_secs)
        except ThreadInterrupted:
            print("interrupted")

    t = InterruptibleThread(target=forever, daemon=True)
    t.start()
    return t


def make_large_download_thread() -> InterruptibleThread:
    def large_download() -> None:
        import requests

        try:
            requests.get("http://ipv4.download.thinkbroadband.com/5GB.zip")
        except ThreadInterrupted:
            print("download interrupted")

    t = InterruptibleThread(target=large_download)
    t.start()
    return t


def make_async_worker_thread(sleep_secs: int) -> InterruptibleThread:
    import asyncio

    def async_worker():
        try:
            # run_interruptible cancels the loop's tasks cleanly on interrupt.
            InterruptibleThread.run_interruptible(asyncio.sleep(sleep_secs))
        except ThreadInterrupted:
            print("async interrupted")

    t = InterruptibleThread(target=async_worker, daemon=True)
    t.start()
    return t


def make_cpu_bound_thread() -> InterruptibleThread:
    def crunch():
        try:
            with periodic_checkpoint(every=100_000) as ck:
                x = 0
                while True:
                    ck.tick()  # honors masking; raises promptly once interrupted
                    x = (x * 1103515245 + 12345) & 0x7FFFFFFF
        except ThreadInterrupted:
            print("cpu work interrupted")

    t = InterruptibleThread(target=crunch, daemon=True)
    t.start()
    return t


def make_critical_section_thread() -> InterruptibleThread:
    def worker():
        try:
            while True:
                with critical_section():
                    # An interrupt arriving here is deferred until the block exits,
                    # so this "commit" never tears mid-way.
                    time.sleep(0.5)
                time.sleep(0.5)  # interruptible
        except ThreadInterrupted:
            print("interrupted (outside critical section)")

    t = InterruptibleThread(target=worker, daemon=True)
    t.start()
    return t


def make_socket_reader_thread() -> tuple[InterruptibleThread, socket.socket]:
    a, b = socket.socketpair()

    def reader():
        try:
            interruptible_recv(a, 1024)  # blocks until data or interrupt
        except ThreadInterrupted:
            print("socket read interrupted")
        finally:
            a.close()

    t = InterruptibleThread(target=reader, daemon=True)
    t.start()
    return t, b


if __name__ == "__main__":
    InterruptibleThread.install_patches()
    t = make_forever_thread(10)
    time.sleep(0.5)
    t.interrupt()
    t.join()
