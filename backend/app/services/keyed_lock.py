"""按 key 复用互斥锁，避免同一缓存 key 的并发请求重复打上游。"""

import threading

_locks = {}
_guard = threading.Lock()


def lock_for(key):
    """返回指定 key 的互斥锁；key 必须是可哈希对象。"""
    with _guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock
