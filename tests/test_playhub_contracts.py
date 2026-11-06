"""插件入口层合同（`plugins/playhub/__init__.py` + `app.py` 的失败路径）。

这里测的是**接线**，不是玩法：
- 一个账号一个库，两个账号的数据互不可见；
- 拿不到账号 / 非群消息 / 自己的消息 / 空文本 → 一行都不写、一句都不回；
- 发不出去、读黑名单失败、记活跃失败都不该影响后续消息——
  前两条是刻意的「失败开放」（见 `app._muted` 的 docstring），写死在这里。
"""
from __future__ import annotations

import hashlib
import logging
import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock

from plugins.playhub import Plugin, db_path_for

ROOM = "12345678@chatroom"
ACC_A = "wxid_account_a"
ACC_B = "wxid_account_b"
UID_A = "wxid_a"
UID_B = "wxid_b"


class FakeMessage:
    def __init__(
        self,
        text: str = "签到",
        *,
        group: bool = True,
        is_self: bool = False,
        room: str = ROOM,
        uid: str = UID_A,
        nickname: str = "小明",
    ) -> None:
        self.text = text
        self.is_group = group
        self.is_self = is_self
        self.chat_id = room
        self.sender_id = uid
        self.data = {"sender_wechat_name": nickname}


class FakeMembers:
    def __init__(self, account: str) -> None:
        self.account = account

    def resolve(self, name, room=None):
        raise KeyError(name)


class FakeCtx:
    def __init__(self, root: pathlib.Path, account: str = ACC_A, config: dict | None = None) -> None:
        self.project_root = root
        self.members = FakeMembers(account)
        self.config = config if config is not None else {}
        self.sent: list[tuple[str, str]] = []
        self.logger = logging.getLogger("test.playhub")

    def reply(self, message, text) -> None:
        self.sent.append((message.chat_id, text))


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.plugin = Plugin()
        self.ctx = FakeCtx(self.root)
        self.plugin.on_load(self.ctx)

    def tearDown(self) -> None:
        self.plugin.on_unload(self.ctx)
        self._tmp.cleanup()

    # -- 工具 ---------------------------------------------------------------
    def send(self, text: str = "签到", ctx: FakeCtx | None = None, **kwargs) -> FakeCtx:
        target = ctx if ctx is not None else self.ctx
        self.plugin.on_message(FakeMessage(text, **kwargs), target)
        return target

    def query(self, account: str, sql: str, params: tuple = ()):
        """只读一条查询，**显式关连接**——`with sqlite3.connect()` 不关连接，
        Windows 下会让临时目录删不掉（WinError 32）。"""
        conn = sqlite3.connect(db_path_for(self.root, account))
        try:
            return conn.execute(sql, params).fetchone()
        finally:
            conn.close()

    def balance(self, account: str, uid: str = UID_A, currency: str = "coin") -> int:
        row = self.query(
            account,
            "SELECT balance FROM wallet WHERE room=? AND uid=? AND currency=?",
            (ROOM, uid, currency),
        )
        return row[0] if row else 0

    def runtime_dir(self) -> pathlib.Path:
        return self.root / ".runtime" / "playhub"


class PathTests(unittest.TestCase):
    def test_path_is_hashed_and_stable(self) -> None:
        path = db_path_for(pathlib.Path("D:/x"), ACC_A)
        digest = hashlib.sha256(ACC_A.encode("ascii")).hexdigest()[:16]
        self.assertEqual(path, pathlib.Path("D:/x") / ".runtime" / "playhub" / digest / "playhub.sqlite3")
        self.assertNotIn(ACC_A, str(path), "存档路径不能带明文账号")

    def test_different_accounts_never_share_a_file(self) -> None:
        self.assertNotEqual(db_path_for(pathlib.Path("D:/x"), ACC_A), db_path_for(pathlib.Path("D:/x"), ACC_B))


class IsolationTests(Base):
    def test_each_account_gets_its_own_database(self) -> None:
        other = FakeCtx(self.root, ACC_B)
        self.send("签到", self.ctx)
        self.send("签到", other, uid=UID_B)
        self.assertTrue(db_path_for(self.root, ACC_A).exists())
        self.assertTrue(db_path_for(self.root, ACC_B).exists())
        self.assertEqual(self.balance(ACC_A), 30)
        self.assertEqual(self.balance(ACC_B, uid=UID_B), 30, "第二个账号要从零开始，不能累计第一个账号的余额")

    def test_same_account_reuses_one_store(self) -> None:
        self.send("签到")
        store, app = self.plugin._runtime(self.ctx)          # noqa: SLF001 - 接线断言
        again = self.plugin._runtime(self.ctx)[0]            # noqa: SLF001
        self.assertIs(store, again, "同一个账号不该重复开库")


class SilentPathTests(Base):
    """这些路径必须一句话都不回、一个文件都不建（失败关闭）。"""

    def assert_nothing_happened(self) -> None:
        self.assertEqual(self.ctx.sent, [])
        self.assertFalse(self.runtime_dir().exists(), "静默路径不该建存档目录")

    def test_missing_account_is_silent(self) -> None:
        self.send("签到", FakeCtx(self.root, ""))
        self.assert_nothing_happened()

    def test_non_group_message_is_silent(self) -> None:
        self.send("签到", group=False)
        self.assert_nothing_happened()

    def test_self_message_is_silent(self) -> None:
        self.send("签到", is_self=True)
        self.assert_nothing_happened()

    def test_blank_text_is_silent(self) -> None:
        self.send("   ")
        self.assert_nothing_happened()

    def test_plain_chatter_is_silent_but_recorded(self) -> None:
        self.send("今天天气不错")
        self.assertEqual(self.ctx.sent, [], "闲聊不该有任何回包")
        row = self.query(
            ACC_A,
            "SELECT messages FROM activity_daily WHERE room=? AND uid=?",
            (ROOM, UID_A),
        )
        self.assertEqual(row[0], 1, "闲聊不进回包，但要进活跃计数")


class FailurePathTests(Base):
    def test_send_failure_does_not_break_later_messages(self) -> None:
        class Boom(FakeCtx):
            def reply(self, message, text):
                raise RuntimeError("发送失败")

        boom = Boom(self.root)
        self.send("签到", boom)
        self.assertEqual(boom.sent, [])
        self.send("签到")                     # 换回正常 ctx，同一个库继续用
        self.assertEqual(len(self.ctx.sent), 1)
        self.assertEqual(self.balance(ACC_A), 30, "第一次的签到已经落库，第二次是重复签到")

    def test_muted_check_failure_is_open(self) -> None:
        """读小黑屋/黑名单失败 → 按「不在」处理，否则全群一起静音。"""
        with mock.patch("plugins.playhub.engines.moderation.is_jailed", side_effect=RuntimeError("库坏了")):
            self.send("签到")
        self.assertEqual(len(self.ctx.sent), 1, "读失败不能变成全员静音")
        self.assertIn("签到成功", self.ctx.sent[0][1])

    def test_activity_record_failure_does_not_block(self) -> None:
        with mock.patch("plugins.playhub.app.activity.record", side_effect=RuntimeError("写不进去")):
            self.send("今天天气不错")          # 只有闲聊会走 _touch
            self.send("签到")
        self.assertEqual(len(self.ctx.sent), 1)
        self.assertIn("签到成功", self.ctx.sent[0][1])

    def test_unload_closes_and_reopens(self) -> None:
        self.send("签到")
        self.plugin.on_unload(self.ctx)
        self.assertEqual(self.plugin._stores, {})             # noqa: SLF001
        self.send("签到")
        self.assertEqual(len(self.ctx.sent), 2, "卸载后应能重新开库")
        self.assertEqual(self.balance(ACC_A), 30, "重新打开读到的是同一个库，不是新库")


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
