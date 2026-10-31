"""成长日结：每天自动把记忆压成一小块自我提醒，全自动，不经审批。

刻意不走 scheduled_tasks：那是网页面板驱动的用户任务清单（有 revision、
开关和启停语义），不是内部定时器。这里用一个后台线程自管节奏，
第一次延迟启动，避免和微信刚连上时的初始化抢资源。
"""

from __future__ import annotations

import logging
import threading

from wxbot_framework import evolution

LOGGER = logging.getLogger("growth")

FIRST_DELAY_SECONDS = 600
INTERVAL_SECONDS = 1800


class Plugin:
    def on_load(self, ctx):
        self.llm = getattr(ctx, "llm", None)
        self.stopped = threading.Event()
        self.thread = None
        if self.llm is None:
            return
        self.thread = threading.Thread(target=self._loop, name="growth", daemon=True)
        self.thread.start()

    def on_unload(self, ctx):
        self.stopped.set()

    def consolidate_now(self):
        """手动跑一次日结：网页面板和排查时用，不等定时。"""
        if self.llm is None:
            return {"updated": 0}
        return {"updated": evolution.sweep(self.llm)}

    def _loop(self):
        if self.stopped.wait(FIRST_DELAY_SECONDS):
            return
        while not self.stopped.is_set():
            try:
                count = evolution.sweep(self.llm)
                if count:
                    LOGGER.info("成长日结完成：%d 个会话", count)
            except Exception as error:
                LOGGER.warning("成长日结失败：%s", error)
            if self.stopped.wait(INTERVAL_SECONDS):
                return
