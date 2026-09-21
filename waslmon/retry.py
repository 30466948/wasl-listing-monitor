from __future__ import annotations

import time
from functools import wraps


def retry(attempts: int = 3, backoff: float = 1.5, base_delay: float = 0.5,
          exceptions: tuple = (Exception,)):
    """Exponential backoff retry. Re-raises after final attempt."""
    def deco(fn):
        @wraps(fn)
        def inner(*args, **kwargs):
            last = None
            for i in range(attempts):
                try:
                    return fn(*args, **kwargs)
                except exceptions as e:
                    last = e
                    if i == attempts - 1:
                        raise
                    time.sleep(base_delay * (backoff ** i))
            if last:
                raise last
        return inner
    return deco
