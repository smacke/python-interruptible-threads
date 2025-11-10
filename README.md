# Python Interruptible Threads

This repo prototypes interruptible threads in Python. It uses ctypes to access
internal Python APIs under the hood and therefore won't work on Python
implementations other than CPython (and likely only works on Linux / Darwin).
To allow interrupting things like `time.sleep` and `asyncio.sleep`, it relies
on patching some of Python's stdlib functionality.

Example usage:

```python
from interruptible_threading import InterruptibleThread

InterruptibleThread.install_patches()

def sleep_forever():
    import time
    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        print("interrupted")

t = InterruptibleThread(target=sleep_forever, daemon=True)
t.start()
```

Then later:

```python
t.interrupt()  # output: "interrupted"
```
