"""playhub 插件入口：把框架回调接到 `App.handle_message`，再把 `Reply` 发出去。

只做三件事：

1. **按账号持有运行期**。每个登录账号一个 SQLite 库，落在
   `<root>/.runtime/playhub/<sha256(account)[:16]>/playhub.sqlite3`；
   `Store` 与 `App` 在 `_runtime()` 里懒建并按账号缓存（`on_load` 时还不知道账号，
   也**不**在这里开库），`on_unload()` 统一关连接。
2. **只接群文本**。非群消息、空文本、自己发的消息、拿不到账号一律静默返回 None
   （失败关闭：不猜账号、不写库）。
3. **事务外发送**。`App.handle_message` 内部已把事务在引擎里提交完毕
   （`BEGIN IMMEDIATE` → 发币 → 流水 → `COMMIT`），入口只在它返回 `Reply`
   之后调 `ctx.reply()`；发送失败只记日志，绝不抛回框架——框架会把异常
   记成插件故障（`wxbot_framework/plugins.py:104-108`）。

本批没有附件、没有慢活执行器（`adapters/executor.py` 属第 3 批），
`Reply` 只有 `text` 与 `action` 两个字段（`plugins/playhub/engines/__init__.py:27-40`）。
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from .app import App
from .core import Store

# 存档根：`<project_root>/.runtime/playhub/<hash>/`，与 image_gallery 的布局同位。
_RUNTIME_DIR = ".runtime"
_PLUGIN_DIR = "playhub"
_DB_NAME = "playhub.sqlite3"
_ACCOUNT_HASH_LEN = 16


class Plugin:
    """框架按 `plugin.json` 的 api=v1 加载本类。

    绑定约定（`wxbot_framework/plugins.py:115-116,222`）：
    实例化 → `on_load(ctx)` → 每条消息 `on_message(message, ctx)` → 卸载时 `on_unload(ctx)`。
    回调单线程串行（`plugins.py:86-111`），所以缓存字典不需要加锁。
    """

    def on_load(self, ctx):
        """只记住上下文；开库留给 `_runtime()`（此时账号还未知）。"""
        self._ctx = ctx
        self._stores: dict[str, Store] = {}
        self._apps: dict[str, App] = {}
        self._warned = False

    # -- 运行期（按账号）-----------------------------------------------------
    def _runtime(self, ctx):
        """返回 `(store, app)`；拿不到账号或开库失败时返回 `(None, None)`（失败关闭）。"""
        logger = self._logger(ctx)
        root = getattr(ctx, "project_root", None) or Path(__file__).resolve().parents[2]
        account = getattr(getattr(ctx, "members", None), "account", "") or ""
        if not isinstance(account, str) or not account:
            # 一个账号都认不出来时宁可整批静默，也不把存档写到猜测的目录。
            if not self._warned:
                self._warned = True
                logger.warning("playhub 取不到登录账号（ctx.members.account 为空），本批不处理消息")
            return (None, None)

        cached_store = self._stores.get(account)
        cached_app = self._apps.get(account)
        if cached_store is not None and cached_app is not None:
            return (cached_store, cached_app)

        store = None
        try:
            directory = (
                Path(root)
                / _RUNTIME_DIR
                / _PLUGIN_DIR
                / hashlib.sha256(account.encode("ascii")).hexdigest()[:_ACCOUNT_HASH_LEN]
            )
            directory.mkdir(parents=True, exist_ok=True)
            # migrate() 幂等（schema.sql 全是 IF NOT EXISTS），每次进程内首次开库跑一遍。
            store = Store(directory / _DB_NAME, account=account)
            store.migrate()
            app = App(store=store, repo_root=Path(root), logger=logger)
        except Exception:
            if store is not None:
                try:
                    store.close()
                except Exception:
                    pass
            logger.exception("playhub 打开存档失败：account=%s", account)
            return (None, None)

        self._stores[account] = store
        self._apps[account] = app
        return (store, app)

    # -- 消息 ---------------------------------------------------------------
    def on_message(self, message, ctx):
        """一条群文本 → 至多一条回包；任何异常都在这里收口，不上抛框架。"""
        try:
            if message is None or not getattr(message, "is_group", False):
                return None                    # 本批只接群
            text = getattr(message, "text", None)
            if not isinstance(text, str) or not text.strip():
                return None
            if getattr(message, "is_self", False):
                return None                    # I1 第一道闸：自己的消息不建库、不回复

            store, app = self._runtime(ctx)
            if store is None or app is None:
                return None

            # app 内部已处理 is_self / 未命中 / 未绑定 / 开关关闭，全部返回 None。
            reply = app.handle_message(ctx, message)
            if reply is None:
                return None
            self._send(ctx, message, reply)     # 事务已在引擎里提交完毕，这里只发文本
        except Exception:
            self._logger(ctx).exception("playhub 处理消息异常；继续处理后续消息")
        return None

    def _send(self, ctx, message, reply):
        """发送失败只吞掉并记日志——一条消息发不出去不该影响后续消息。"""
        try:
            ctx.reply(message, reply.text)
        except Exception:
            self._logger(ctx).exception(
                "playhub 回包发送失败：action=%s room=%s uid=%s",
                getattr(reply, "action", ""),
                getattr(message, "chat_id", ""),
                getattr(message, "sender_id", ""),
            )

    # -- 卸载 ---------------------------------------------------------------
    def on_unload(self, ctx):
        """逐个关连接（一个坏连接不影响其它），然后清空缓存。"""
        for account, store in list(self._stores.items()):
            try:
                store.close()
            except Exception:
                self._logger(ctx).exception("playhub 关闭存档失败：account=%s", account)
        self._stores.clear()
        self._apps.clear()

    # -- 内部 ---------------------------------------------------------------
    @staticmethod
    def _logger(ctx):
        """`ctx.logger` 缺失时退回模块 logger，保证任何路径都能记日志。"""
        logger = getattr(ctx, "logger", None) if ctx is not None else None
        return logger if logger is not None else logging.getLogger("playhub")
