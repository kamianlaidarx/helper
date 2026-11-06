"""有界后台执行器：把 HTTP / LLM / 图片这类慢活移出插件回调线程（§2.5 第 3、4 条）。

两条硬规矩，改这个文件之前先看明白：

1. **worker 不许碰 `Store`**。前台 `Store` 是**一条**共享连接（`core/store.py:34-41`；
   `check_same_thread=False` 只是给「线程内迁移」留的余地），两个线程同时
   `BEGIN IMMEDIATE` 会把事务语义搅坏，而且这种损坏不是每次都能复现。
   worker 需要落库时用 `defer()` 把闭包交回插件线程，由 `flush()` 在回调线程里执行。
2. **队列必须有界**。`ThreadPoolExecutor` 自己的队列是无界的，群里刷屏时会把
   内存和延迟一起拖爆；这里配一个 `queue_size` 信号量，满了 `submit()` 直接返回
   `False`，由调用方决定「退化成同步」还是「回一句稍后再试」——绝不无限排队。

超时的诚实说明：线程**无法被中断**，所以 `timeout` 只在任务结束后量一下、超了记警告，
不负责终止任务。真正的超时保证来自任务自身的 transport 超时（天气是
`weather_timeout_seconds`，夹在 `0 < t ≤ 10`）。

用法：

```python
ex = Executor(logger=logger)
ex.submit(slow_http_call, city, on_done=..., on_error=..., label="weather")
ex.defer(write_cache, store, room, city, payload)   # 由插件线程 flush
```
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

DEFAULT_WORKERS = 4
DEFAULT_QUEUE_SIZE = 32
DEFAULT_TIMEOUT = 3.0
DEFERRED_LIMIT = 256
FLUSH_LIMIT = 64
THREAD_PREFIX = "playhub-work"


class Executor:
    """有界线程池 + 延迟落库队列。所有方法线程安全。"""

    def __init__(
        self,
        *,
        workers: int = DEFAULT_WORKERS,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        timeout: float = DEFAULT_TIMEOUT,
        logger: logging.Logger | None = None,
    ) -> None:
        self._log = logger if logger is not None else logging.getLogger("playhub.executor")
        self._workers = max(1, int(workers))
        self._timeout = float(timeout)
        self._pool = ThreadPoolExecutor(
            max_workers=self._workers, thread_name_prefix=THREAD_PREFIX
        )
        # 信号量就是队列上限：拿到才允许提交，任务结束时归还。
        self._slots = threading.BoundedSemaphore(max(1, int(queue_size)))
        self._lock = threading.Lock()
        self._deferred: deque[tuple[Callable[..., Any], tuple[Any, ...], str]] = deque()
        self._closed = False
        self._stats = {
            "submitted": 0,
            "rejected": 0,
            "done": 0,
            "failed": 0,
            "deferred": 0,
            "flushed": 0,
            "dropped": 0,
            "elapsed_max": 0.0,
        }

    # -- 提交 ---------------------------------------------------------------
    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        on_done: Callable[[Any], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
        label: str = "",
    ) -> bool:
        """提交一个慢活；返回 `False` 表示没接（已关闭或队列满）。"""
        if self._closed:
            self._bump("rejected")
            return False
        if not self._slots.acquire(blocking=False):
            self._bump("rejected")
            self._log.warning("后台队列已满，拒绝任务：%s", label or _name_of(fn))
            return False
        self._bump("submitted")
        try:
            self._pool.submit(self._run, fn, args, on_done, on_error, label)
        except RuntimeError:                     # 池已被关掉：把名额还回去
            self._slots.release()
            self._bump("rejected")
            return False
        return True

    def _run(
        self,
        fn: Callable[..., Any],
        args: tuple[Any, ...],
        on_done: Callable[[Any], None] | None,
        on_error: Callable[[BaseException], None] | None,
        label: str,
    ) -> None:
        name = label or _name_of(fn)
        started = time.monotonic()
        try:
            result = fn(*args)
        except BaseException as exc:             # 后台线程的异常绝不能上抛
            self._bump("failed")
            self._log.exception("后台任务失败：%s", name)
            self._safe(on_error, exc, name)
        else:
            self._bump("done")
            self._safe(on_done, result, name)
        finally:
            elapsed = time.monotonic() - started
            self._note_elapsed(elapsed)
            if elapsed > self._timeout:
                self._log.warning(
                    "后台任务超过 %.1f s 仍未结束（%s，耗时 %.1f s）：线程无法中断，仅记录",
                    self._timeout, name, elapsed,
                )
            self._slots.release()

    def _safe(self, callback, value, name: str) -> None:
        if callback is None:
            return
        try:
            callback(value)
        except Exception:
            self._log.exception("后台任务回调失败：%s", name)

    # -- 延迟落库（只允许插件线程 flush）------------------------------------
    def defer(self, fn: Callable[..., Any], *args: Any, label: str = "") -> bool:
        """把需要碰 `Store` 的收尾工作交回插件线程；返回 `False` 表示队列满。"""
        with self._lock:
            if self._closed:
                self._stats["rejected"] += 1
                return False
            if len(self._deferred) >= DEFERRED_LIMIT:
                self._stats["dropped"] += 1
                self._log.warning(
                    "延迟落库队列已满，丢弃：%s", label or _name_of(fn)
                )
                return False
            self._deferred.append((fn, args, label or _name_of(fn)))
            self._stats["deferred"] += 1
            return True

    def flush(self, *, limit: int = FLUSH_LIMIT) -> int:
        """在**插件线程**里执行延迟落库；返回执行条数。单个失败不影响后续。"""
        batch: list[tuple[Callable[..., Any], tuple[Any, ...], str]] = []
        with self._lock:
            while self._deferred and len(batch) < max(0, int(limit)):
                batch.append(self._deferred.popleft())
        for fn, args, label in batch:
            try:
                fn(*args)
            except Exception:
                # 库可能已经关了（卸载中），这不是故障，只是这条缓存作废。
                self._log.warning("延迟落库失败：%s", label, exc_info=True)
            else:
                self._bump("flushed")
        return len(batch)

    # -- 生命周期 -----------------------------------------------------------
    def shutdown(self, *, wait: bool = False) -> None:
        """停止接新任务；已在跑的由 `wait` 决定是否等（卸载路径不等）。

        延迟落库队列**直接丢弃**：它写的是缓存，卸载时库马上就关了，
        硬写只会得到一堆噪音日志。
        """
        with self._lock:
            self._closed = True
            dropped = len(self._deferred)
            self._deferred.clear()
            if dropped:
                self._stats["dropped"] += dropped
        try:
            self._pool.shutdown(wait=wait, cancel_futures=True)
        except Exception:
            self._log.exception("关闭后台执行器失败")

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def pending(self) -> int:
        """已提交但还没收尾的任务数（信息用途，不保证精确到微秒）。"""
        with self._lock:
            return (
                self._stats["submitted"] - self._stats["done"] - self._stats["failed"]
            )

    def stats(self) -> dict[str, Any]:
        with self._lock:
            snapshot = dict(self._stats)
            snapshot["deferred_pending"] = len(self._deferred)
            snapshot["closed"] = self._closed
            snapshot["workers"] = self._workers
        return snapshot

    # -- 内部 ---------------------------------------------------------------
    def _bump(self, key: str) -> None:
        with self._lock:
            self._stats[key] += 1

    def _note_elapsed(self, elapsed: float) -> None:
        with self._lock:
            if elapsed > self._stats["elapsed_max"]:
                self._stats["elapsed_max"] = elapsed


def _name_of(fn: Any) -> str:
    return getattr(fn, "__name__", None) or type(fn).__name__
