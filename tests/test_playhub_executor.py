"""`adapters/executor.py` 的门槛用例：队列上限、失败不杀池、延迟落库、生命周期。

这些用例只碰执行器本身，不建库、不发消息——执行器是**没有业务语义**的基础件，
它的 bug 会以「群里偶发丢一条卡」的形式出现，所以边界必须钉死。
"""
import threading
import time
import unittest

from plugins.playhub.adapters.executor import (
    DEFAULT_QUEUE_SIZE,
    DEFAULT_WORKERS,
    DEFERRED_LIMIT,
    THREAD_PREFIX,
    Executor,
)


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    """等一个条件成立（最长 2 s）。测试里不用 sleep 猜时间。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class ExecutorTestBase(unittest.TestCase):
    def make(self, **kwargs) -> Executor:
        ex = Executor(**kwargs)
        self.addCleanup(ex.shutdown)
        return ex


class SubmitTests(ExecutorTestBase):
    def test_runs_task_and_calls_on_done(self):
        ex = self.make()
        got = []
        done = threading.Event()

        def task(value):
            return value * 2

        self.assertTrue(
            ex.submit(task, 21, on_done=lambda r: (got.append(r), done.set()))
        )
        self.assertTrue(done.wait(2.0))
        self.assertEqual(got, [42])
        stats = ex.stats()
        self.assertEqual(stats["submitted"], 1)
        self.assertEqual(stats["done"], 1)
        self.assertEqual(stats["failed"], 0)

    def test_task_exception_goes_to_on_error_and_pool_survives(self):
        ex = self.make()
        errors = []
        failed = threading.Event()

        def boom():
            raise ValueError("boom")

        with self.assertLogs("playhub.executor", level="ERROR"):
            self.assertTrue(
                ex.submit(boom, on_error=lambda exc: (errors.append(exc), failed.set()))
            )
            self.assertTrue(failed.wait(2.0))
        self.assertIsInstance(errors[0], ValueError)

        # 池没被杀：下一个任务照常跑完。
        done = threading.Event()
        self.assertTrue(ex.submit(lambda: "ok", on_done=lambda _r: done.set()))
        self.assertTrue(done.wait(2.0))
        stats = ex.stats()
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["done"], 1)

    def test_callback_exception_does_not_break_pool(self):
        ex = self.make()
        after = threading.Event()

        def bad_callback(_result):
            raise RuntimeError("回调自己炸了")

        with self.assertLogs("playhub.executor", level="ERROR"):
            self.assertTrue(ex.submit(lambda: 1, on_done=bad_callback))
            self.assertTrue(_wait_until(lambda: ex.stats()["done"] == 1))

        self.assertTrue(ex.submit(lambda: 2, on_done=lambda _r: after.set()))
        self.assertTrue(after.wait(2.0))
        self.assertEqual(ex.stats()["failed"], 0)

    def test_rejects_when_queue_is_full(self):
        ex = self.make(workers=1, queue_size=1)
        gate = threading.Event()
        started = threading.Event()

        def blocker():
            started.set()
            gate.wait(2.0)

        self.assertTrue(ex.submit(blocker, label="blocker"))
        self.assertTrue(started.wait(2.0))
        with self.assertLogs("playhub.executor", level="WARNING"):
            self.assertFalse(ex.submit(lambda: None, label="overflow"))
        self.assertGreaterEqual(ex.stats()["rejected"], 1)

        gate.set()
        self.assertTrue(_wait_until(lambda: ex.stats()["done"] == 1))
        self.assertTrue(ex.submit(lambda: None))       # 名额归还后可以再收

    def test_rejects_after_shutdown(self):
        ex = self.make()
        ex.shutdown()
        self.assertFalse(ex.submit(lambda: None))
        self.assertEqual(ex.stats()["rejected"], 1)

    def test_pending_counts_in_flight_tasks(self):
        ex = self.make(workers=1)
        gate = threading.Event()
        started = threading.Event()

        def blocker():
            started.set()
            gate.wait(2.0)

        self.assertTrue(ex.submit(blocker))
        self.assertTrue(started.wait(2.0))
        self.assertEqual(ex.pending, 1)
        gate.set()
        self.assertTrue(_wait_until(lambda: ex.pending == 0))


class DeferTests(ExecutorTestBase):
    def test_defer_does_not_run_until_flush(self):
        ex = self.make()
        calls = []
        self.assertTrue(ex.defer(calls.append, 1, label="cache"))
        self.assertEqual(calls, [])                    # 只有插件线程能执行
        self.assertEqual(ex.flush(), 1)
        self.assertEqual(calls, [1])
        stats = ex.stats()
        self.assertEqual(stats["deferred"], 1)
        self.assertEqual(stats["flushed"], 1)
        self.assertEqual(stats["deferred_pending"], 0)

    def test_flush_keeps_going_after_one_task_fails(self):
        ex = self.make()
        calls = []

        def broken():
            raise OSError("库已经关了")

        ex.defer(broken)
        ex.defer(calls.append, "second")
        with self.assertLogs("playhub.executor", level="WARNING"):
            self.assertEqual(ex.flush(), 2)
        self.assertEqual(calls, ["second"])
        self.assertEqual(ex.stats()["flushed"], 1)     # 失败的没算「已落库」

    def test_flush_respects_limit(self):
        ex = self.make()
        calls = []
        for i in range(5):
            ex.defer(calls.append, i)
        self.assertEqual(ex.flush(limit=2), 2)
        self.assertEqual(calls, [0, 1])
        self.assertEqual(ex.stats()["deferred_pending"], 3)

    def test_defer_rejected_after_shutdown(self):
        ex = self.make()
        ex.shutdown()
        self.assertFalse(ex.defer(lambda: None))
        self.assertEqual(ex.stats()["rejected"], 1)

    def test_defer_queue_is_bounded(self):
        ex = self.make()
        for _ in range(DEFERRED_LIMIT):
            self.assertTrue(ex.defer(lambda: None))
        with self.assertLogs("playhub.executor", level="WARNING"):
            self.assertFalse(ex.defer(lambda: None))
        self.assertEqual(ex.stats()["dropped"], 1)
        self.assertEqual(ex.stats()["deferred_pending"], DEFERRED_LIMIT)

    def test_flush_on_empty_queue_returns_zero(self):
        ex = self.make()
        self.assertEqual(ex.flush(), 0)
        self.assertEqual(ex.stats()["flushed"], 0)


class TimeoutTests(ExecutorTestBase):
    def test_slow_task_only_logs_a_warning(self):
        ex = self.make(workers=1, timeout=0.01)
        done = threading.Event()
        with self.assertLogs("playhub.executor", level="WARNING") as logs:
            self.assertTrue(
                ex.submit(lambda: time.sleep(0.05), on_done=lambda _r: done.set())
            )
            self.assertTrue(done.wait(2.0))
        messages = "\n".join(logs.output)
        self.assertIn("线程无法中断", messages)
        self.assertEqual(ex.stats()["failed"], 0)      # 超时不是失败

    def test_elapsed_max_tracks_the_slowest_task(self):
        ex = self.make(workers=2)
        done = threading.Event()

        def slow():
            time.sleep(0.02)

        self.assertTrue(ex.submit(slow, on_done=lambda _r: done.set()))
        self.assertTrue(done.wait(2.0))
        self.assertGreaterEqual(ex.stats()["elapsed_max"], 0.02)


class LifecycleTests(ExecutorTestBase):
    def test_worker_threads_use_the_playhub_prefix(self):
        ex = self.make(workers=1)
        names = []
        done = threading.Event()

        self.assertTrue(
            ex.submit(
                lambda: threading.current_thread().name,
                on_done=lambda name: (names.append(name), done.set()),
            )
        )
        self.assertTrue(done.wait(2.0))
        self.assertTrue(names[0].startswith(THREAD_PREFIX))

    def test_shutdown_drops_deferred_tasks(self):
        ex = self.make()
        calls = []
        ex.defer(calls.append, 1)
        ex.defer(calls.append, 2)
        ex.shutdown()
        self.assertEqual(ex.stats()["dropped"], 2)
        self.assertEqual(ex.stats()["deferred_pending"], 0)
        self.assertEqual(ex.flush(), 0)
        self.assertEqual(calls, [])

    def test_shutdown_wait_lets_running_task_finish(self):
        ex = self.make(workers=1)
        finished = []
        started = threading.Event()

        def task():
            started.set()
            time.sleep(0.02)
            finished.append(True)

        self.assertTrue(ex.submit(task))
        self.assertTrue(started.wait(2.0))
        ex.shutdown(wait=True)
        self.assertEqual(finished, [True])
        self.assertTrue(ex.closed)

    def test_stats_shape(self):
        ex = self.make()
        stats = ex.stats()
        for key in (
            "submitted", "rejected", "done", "failed", "deferred",
            "flushed", "dropped", "elapsed_max",
        ):
            self.assertIn(key, stats)
        self.assertEqual(stats["workers"], DEFAULT_WORKERS)
        self.assertFalse(stats["closed"])


class DefaultsTests(unittest.TestCase):
    def test_defaults_match_the_design_doc(self):
        # §2.5 第 3 条：max_workers=4、queue=32
        self.assertEqual(DEFAULT_WORKERS, 4)
        self.assertEqual(DEFAULT_QUEUE_SIZE, 32)


if __name__ == "__main__":
    unittest.main()
