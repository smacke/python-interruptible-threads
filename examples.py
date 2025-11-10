from __future__ import annotations
from interruptible_threading import InterruptibleThread


def make_forever_thread(sleep_secs: int) -> InterruptibleThread:
    def forever():
        import time
        try:
            while True:
                time.sleep(sleep_secs)
        except KeyboardInterrupt:
            print("interrupted")

    t = InterruptibleThread(target=forever, daemon=True)
    t.start()
    return t


def make_large_download_thread() -> InterruptibleThread:
    def large_download() -> None:
        import requests

        try:
            requests.get("http://ipv4.download.thinkbroadband.com/5GB.zip")
        except KeyboardInterrupt:
            print("download interrupted")

    t = InterruptibleThread(target=large_download)
    t.start()
    return t


def make_async_worker_thread(sleep_secs: int) -> InterruptibleThread:
    import asyncio

    def async_worker():
        try:
            asyncio.run(asyncio.sleep(sleep_secs))
        except KeyboardInterrupt:
            print("interrupted")

    t = InterruptibleThread(target=async_worker, daemon=True)
    t.start()
    return t
