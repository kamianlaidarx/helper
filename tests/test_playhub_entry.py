"""playhub 插件入口单测：`Plugin.on_message` 接 `App` 并发出 `Reply`。

覆盖：

1. 存档布局：目录名 = `sha256(account)[:16]`，库文件落该目录，`migrate()` 后
   `wallet` / `wallet_ledger` / `daily_counter` 三张表存在。
2. 连接复用：同账号第二条消息复用同一个 `Store` 实例，不重复开库。
3. 账号隔离：两个账号 → 两个目录，各记各的流水。
4. 走通路径：「签到」回一条「签到成功…」，金币+流水+计数器各 1 行；
   「帮助」回 `HELP_TEXT` 且不碰钱包。
5. 静默路径：非群 / 空文本 / 自己发的消息 → 不回复、不建目录。
6. 兜底：`ctx.reply` 抛异常不冒泡、只记日志，后续消息照常回。
7. 账号缺失：`ctx.members.account` 为空 → 不建库、不回复，warning 只记一次。

不联网、不 sleep、不写仓库外路径：全部在 `tempfile.TemporaryDirectory` 里跑。
"""
from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from plugins.playhub import Plugin
from plugins.playhub.app import HELP_TEXT
from plugins.playhub.core.store import SCHEMA_VERSION
from plugins.playhub.engines.daily import signin_coin

ROOM = "12345678@chatroom"
UID_A = "wxid_player_a"
UID_B = "wxid_player_b"
NICK = "小明"
ACCOUNT_A = "wxid_bot_account_a"
ACCOUNT_B = "wxid_bot_account_b"


class FakeMsg:
    """最小 Message 替身：只带入口与引擎真正读的字段。"""

    def __init__(
        self,
        text="签到",
        *,
        is_group=True,
        is_self=False,
        chat_id=ROOM,
        sender_id=UID_A,
        nickname=NICK,
    ):
        self.text = text
        self.is_group = is_group
        self.is_self = is_self
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.data = {"sender_wechat_name": nickname}


class CaptureLogger:
    """接住入口的日志（与 `tests/test_playhub_app.py` 里的同形替身）。"""

    def __init__(self):
        self.records: list[tuple[str, tuple]] = []

    def exception(self, msg, *args, **kwargs):        # 与 logging.Logger.exception 同形
        self.records.append((msg, args))

    def warning(self, msg, *args, **kwargs):
        self.records.append((msg, args))

    def debug(self, msg, *args, **kwargs):
        self.records.append((msg, args))

    def info(self, msg, *args, **kwargs):
        self.records.append((msg, args))


class FakeCtx:
    """框架 `PluginContext` 的替身：账号、仓库根、日志、`reply()` 记录。"""

    def __init__(self, root: Path, account: str, *, logger=None, reply_raises=False):
        self.project_root = root
        self.members = SimpleNamespace(account=account)
        self.logger = logger if logger is not None else CaptureLogger()
        self.replies: list[tuple[object, str]] = []
        self._reply_raises = reply_raises

    def reply(self, message, text, *, delivery_guard=None):
        if self._reply_raises:
            raise RuntimeError("模拟发送失败")
        self.replies.append((message, text))


def account_dir(root: Path, account: str) -> Path:
    return root / ".runtime" / "playhub" / hashlib.sha256(account.encode("ascii")).hexdigest()[:16]


def fetch(db_path: Path, sql: str, params: tuple = ()):
    """只读直连查库（入口自己那条连接正开着，WAL 下读连接不受影响）。"""
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


class EntryCase(unittest.TestCase):
    """一个临时仓库根 + 一个已 `on_load` 的 Plugin；账号默认 `ACCOUNT_A`。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.plugin = Plugin()
        self.ctx = FakeCtx(self.root, ACCOUNT_A)
        self.plugin.on_load(self.ctx)
        self.addCleanup(self.plugin.on_unload, self.ctx)

    def db_path(self, account: str = ACCOUNT_A) -> Path:
        return account_dir(self.root, account) / "playhub.sqlite3"

    # -- 用例 ---------------------------------------------------------------
    def test_store_dir_and_schema(self):
        self.plugin.on_message(FakeMsg("签到"), self.ctx)

        directory = account_dir(self.root, ACCOUNT_A)
        self.assertEqual(directory.name, hashlib.sha256(ACCOUNT_A.encode("ascii")).hexdigest()[:16])
        self.assertTrue(directory.is_dir())
        db = directory / "playhub.sqlite3"
        self.assertTrue(db.is_file())

        names = {row[0] for row in fetch(db, "SELECT name FROM sqlite_master WHERE type = 'table'")}
        for table in ("wallet", "wallet_ledger", "daily_counter"):
            self.assertIn(table, names, f"migrate() 后缺表：{table}")
        self.assertEqual(
            fetch(db, "SELECT value FROM meta WHERE key = 'schema_version'")[0][0],
            str(SCHEMA_VERSION),
        )

    def test_same_account_reuses_store(self):
        self.plugin.on_message(FakeMsg("签到"), self.ctx)
        first = id(self.plugin._stores[ACCOUNT_A])

        self.plugin.on_message(FakeMsg("帮助"), self.ctx)
        second = id(self.plugin._stores[ACCOUNT_A])

        self.assertEqual(first, second, "同账号第二条消息不应重复开库")
        self.assertEqual(len(self.plugin._stores), 1)
        self.assertEqual(len(self.plugin._apps), 1)

        self.plugin.on_unload(self.ctx)          # 再跑一次 on_unload 也应安全
        self.assertEqual(self.plugin._stores, {})
        self.assertEqual(self.plugin._apps, {})

    def test_two_accounts_use_separate_dirs(self):
        ctx_b = FakeCtx(self.root, ACCOUNT_B)
        self.plugin.on_message(FakeMsg("签到", sender_id=UID_A), self.ctx)
        self.plugin.on_message(FakeMsg("签到", sender_id=UID_B), ctx_b)

        dir_a, dir_b = account_dir(self.root, ACCOUNT_A), account_dir(self.root, ACCOUNT_B)
        self.assertNotEqual(dir_a, dir_b)
        self.assertTrue((dir_a / "playhub.sqlite3").is_file())
        self.assertTrue((dir_b / "playhub.sqlite3").is_file())
        self.assertEqual(sorted(self.plugin._stores), sorted([ACCOUNT_A, ACCOUNT_B]))

        uids_a = {row[0] for row in fetch(self.db_path(ACCOUNT_A), "SELECT DISTINCT uid FROM wallet")}
        uids_b = {row[0] for row in fetch(self.db_path(ACCOUNT_B), "SELECT DISTINCT uid FROM wallet")}
        self.assertEqual(uids_a, {UID_A})
        self.assertEqual(uids_b, {UID_B})
        # 各记各的：两个库各自一条金币流水、余额各是首签额度，互不叠加。
        for db in (self.db_path(ACCOUNT_A), self.db_path(ACCOUNT_B)):
            self.assertEqual(fetch(db, "SELECT COUNT(*) FROM wallet_ledger")[0][0], 1)
            self.assertEqual(fetch(db, "SELECT balance FROM wallet WHERE currency = 'coin'")[0][0], signin_coin(1))

    def test_signin_replies_once(self):
        self.plugin.on_message(FakeMsg("签到"), self.ctx)

        self.assertEqual(len(self.ctx.replies), 1)
        _message, text = self.ctx.replies[0]
        self.assertTrue(text.startswith("签到成功"), text)

        db = self.db_path()
        self.assertEqual(fetch(db, "SELECT COUNT(*) FROM wallet WHERE currency = 'coin'")[0][0], 1)
        self.assertEqual(fetch(db, "SELECT COUNT(*) FROM wallet_ledger")[0][0], 1)
        self.assertEqual(fetch(db, "SELECT COUNT(*) FROM daily_counter WHERE key = 'signin'")[0][0], 1)
        self.assertEqual(
            fetch(db, "SELECT balance FROM wallet WHERE currency = 'coin' AND uid = ?", (UID_A,))[0][0],
            signin_coin(1),
        )

    def test_help_does_not_touch_wallet(self):
        self.plugin.on_message(FakeMsg("帮助"), self.ctx)

        self.assertEqual(len(self.ctx.replies), 1)
        self.assertEqual(self.ctx.replies[0][1], HELP_TEXT)
        db = self.db_path()
        self.assertEqual(fetch(db, "SELECT COUNT(*) FROM wallet_ledger")[0][0], 0)
        self.assertEqual(fetch(db, "SELECT COUNT(*) FROM daily_counter")[0][0], 0)
        self.assertEqual(fetch(db, "SELECT COUNT(*) FROM wallet")[0][0], 0)

    def test_ignored_messages_create_nothing(self):
        cases = (
            FakeMsg("签到", is_group=False),      # 非群
            FakeMsg("   "),                       # 空文本
            FakeMsg(None),                        # 非字符串文本
            FakeMsg("签到", is_self=True),         # 自己发的
        )
        for message in cases:
            self.assertIsNone(self.plugin.on_message(message, self.ctx))
        self.assertIsNone(self.plugin.on_message(None, self.ctx))

        self.assertEqual(self.ctx.replies, [])
        self.assertFalse(account_dir(self.root, ACCOUNT_A).exists(), "被忽略的消息不应建目录")
        self.assertEqual(self.plugin._stores, {})

    def test_reply_failure_is_swallowed(self):
        bad = FakeCtx(self.root, ACCOUNT_A, reply_raises=True)
        self.assertIsNone(self.plugin.on_message(FakeMsg("签到"), bad))
        self.assertTrue(
            any("发送失败" in record[0] for record in bad.logger.records),
            bad.logger.records,
        )

        # 同一账号的后续消息照常走（连接没被发送异常破坏）。
        self.plugin.on_message(FakeMsg("帮助"), self.ctx)
        self.assertEqual(len(self.ctx.replies), 1)
        self.assertEqual(self.ctx.replies[0][1], HELP_TEXT)

    def test_missing_account_warns_once(self):
        anonymous = FakeCtx(self.root, "")
        for _ in range(2):
            self.assertIsNone(self.plugin.on_message(FakeMsg("签到"), anonymous))

        self.assertEqual(anonymous.replies, [])
        self.assertEqual(len(anonymous.logger.records), 1, anonymous.logger.records)
        self.assertIn("account", anonymous.logger.records[0][0])
        self.assertEqual(list((self.root / ".runtime" / "playhub").glob("*")), [])
        self.assertEqual(self.plugin._stores, {})


if __name__ == "__main__":
    unittest.main()
