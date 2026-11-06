"""消息级幂等（`docs/design/playhub.md` §4.2.10）：同一条微信消息只生效一次。

重投是常态（断线重连、session 重放、宿主重试），而「送礼」「工资」这类命令
每次都合法，光靠业务状态位兜不住。这里测的是 `App.handle_message` 在 dispatch
之前 claim `idem` 表的行为：

* 键优先用 `native_message_id`（`""`/`"0"` 算没有），退回
  `<session_id>:<sequence>`；两者都没有就不做幂等（编不出身份别假装有）；
* 先 claim 后执行，重复直接返回 `None`（不回复、不改状态）；
* 执行失败不释放（至多一次），claim 自己失败则不执行。

全部离线：临时目录 + 已迁移 `Store` + 注入 `now_fn`，不联网、不 sleep。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from plugins.playhub.app import ERROR_TEXT, App
from plugins.playhub.core import Store, balance
from plugins.playhub.core import kv
from plugins.playhub.engines.daily import SIGNIN_ALREADY

ROOM = "12345678@chatroom"
OTHER_ROOM = "87654321@chatroom"
UID = "wxid_player_a"
NOW = 1_760_000_000          # 固定时间，避免依赖真实时钟
NATIVE = "native-0001"
SESSION = "sess-1"


class FakeMsg:
    """最小 Message 替身；额外带 §4.2.10 需要的三个身份字段。"""

    def __init__(
        self,
        text="签到",
        *,
        chat_id=ROOM,
        sender_id=UID,
        native=NATIVE,
        session_id=None,
        sequence=None,
        is_self=False,
    ):
        self.text = text
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.is_self = is_self
        self.session_id = session_id
        self.sequence = sequence
        self.data: dict = {"sender_wechat_name": "小明"}
        if native is not None:
            self.data["native_message_id"] = native


class CaptureLogger:
    """接住 app 的日志：断言「重复投递只记 debug、不刷 warning」。"""

    def __init__(self):
        self.records: list[tuple[str, tuple]] = []

    def exception(self, msg, *args, **kwargs):
        self.records.append(("exception", (msg, args)))

    def warning(self, msg, *args, **kwargs):
        self.records.append(("warning", (msg, args)))

    def debug(self, msg, *args, **kwargs):
        self.records.append(("debug", (msg, args)))

    def info(self, msg, *args, **kwargs):
        self.records.append(("info", (msg, args)))


class IdemBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.log = CaptureLogger()
        self.store = self.open_store()
        self.app = App(
            store=self.store,
            now_fn=lambda: NOW,
            repo_root=self.root / "app",
            logger=self.log,
        )

    def open_store(self, name: str = "playhub.db", timeout: float = 3.0) -> Store:
        store = Store(self.root / name, account="acct", timeout=timeout)
        store.migrate()
        self.addCleanup(store.close)
        return store

    # -- 断言辅助 ------------------------------------------------------------
    def coins(self, room: str = ROOM) -> int:
        with self.store.tx(immediate=False) as conn:
            return balance(conn, room=room, uid=UID, currency="coin")

    def idem_rows(self) -> list[tuple[str, str, int]]:
        with self.store.tx(immediate=False) as conn:
            return [
                (str(key), str(scope), int(at))
                for key, scope, at in conn.execute(
                    "SELECT key, scope, at FROM idem ORDER BY key"
                ).fetchall()
            ]

    def ledger_count(self) -> int:
        with self.store.tx(immediate=False) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM wallet_ledger").fetchone()[0])


class IdemKeyTest(IdemBase):
    """两种键来源与「都不满足就不幂等」。"""

    def test_same_native_id_takes_effect_once(self):
        first = self.app.handle_message(None, FakeMsg())
        second = self.app.handle_message(None, FakeMsg())
        self.assertIsNone(second)                            # 重复：不回复
        self.assertEqual(self.coins(), 30)                   # 只发了一次金币
        self.assertEqual(self.ledger_count(), 1)
        self.assertEqual(
            self.idem_rows(), [(f"msg:{ROOM}:{NATIVE}:daily.signin", "daily.signin", NOW)]
        )

    def test_native_id_wins_over_session_and_sequence(self):
        self.app.handle_message(None, FakeMsg(native=NATIVE, session_id="sess-a", sequence=3))
        again = self.app.handle_message(
            None, FakeMsg(native=NATIVE, session_id="sess-b", sequence=99)
        )
        self.assertIsNone(again)                             # 同一原消息 id 就是同一条
        self.assertEqual(self.coins(), 30)

    def test_empty_native_id_falls_back_to_session_sequence(self):
        self.app.handle_message(None, FakeMsg(native="", session_id=SESSION, sequence=7))
        again = self.app.handle_message(None, FakeMsg(native="0", session_id=SESSION, sequence=7))
        self.assertIsNone(again)
        self.assertEqual(self.coins(), 30)
        self.assertEqual(self.idem_rows(), [(f"{SESSION}:7:daily.signin", "daily.signin", NOW)])

    def test_without_any_identity_both_deliveries_run(self):
        self.app.handle_message(None, FakeMsg(native=None))
        again = self.app.handle_message(None, FakeMsg(native=None))
        self.assertIsNotNone(again)                           # 没有键 → 不做幂等
        self.assertEqual(again.text, SIGNIN_ALREADY)          # 业务状态位照常兜住
        self.assertEqual(self.coins(), 30)
        self.assertEqual(self.idem_rows(), [])

    def test_sequence_zero_is_not_an_identity(self):
        self.app.handle_message(None, FakeMsg(native=None, session_id=SESSION, sequence=0))
        self.app.handle_message(None, FakeMsg(native=None, session_id=SESSION, sequence=0))
        self.assertEqual(self.idem_rows(), [])                # sequence=0 不算序号

    def test_same_native_id_in_two_rooms_are_different_keys(self):
        self.app.handle_message(None, FakeMsg(chat_id=ROOM))
        self.app.handle_message(None, FakeMsg(chat_id=OTHER_ROOM))
        self.assertEqual(self.coins(ROOM), 30)
        self.assertEqual(self.coins(OTHER_ROOM), 30)
        self.assertEqual(len(self.idem_rows()), 2)

    def test_scope_and_at_are_recorded(self):
        self.app.handle_message(None, FakeMsg())
        (key, scope, at), = self.idem_rows()
        self.assertEqual(scope, "daily.signin")               # scope = action
        self.assertEqual(at, NOW)                             # at = 注入的 now


class IdemFailureTest(IdemBase):
    """执行失败、claim 失败、重复三种「什么都没发生」的路径。"""

    def test_engine_exception_keeps_the_claim(self):
        class Boom:
            name = "daily"

            def on_command(self, cmd, msg=None, ctx=None, game=None):
                raise RuntimeError("炸了")

        app = App(store=self.store, engines={"daily": Boom()}, now_fn=lambda: NOW, logger=self.log)
        reply = app.handle_message(None, FakeMsg())
        self.assertEqual(reply.text, ERROR_TEXT)
        self.assertEqual(len(self.idem_rows()), 1)            # 失败也不释放（至多一次）
        self.assertIsNone(app.handle_message(None, FakeMsg()))  # 重投不再执行
        self.assertEqual(self.ledger_count(), 0)              # 引擎没跑，账没动

    def test_switch_off_never_writes_idem(self):
        with self.store.tx() as conn:
            kv.put(conn, ROOM, "feature.daily", "off", NOW)
        self.assertIsNone(self.app.handle_message(None, FakeMsg()))
        self.assertEqual(self.idem_rows(), [])

    def test_jailed_member_never_writes_idem(self):
        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO jail (room, uid, until, reason, by_uid, at) VALUES (?, ?, ?, ?, ?, ?)",
                (ROOM, UID, NOW + 600, "test", UID, NOW),
            )
        self.assertIsNone(self.app.handle_message(None, FakeMsg()))
        self.assertEqual(self.idem_rows(), [])

    def test_plain_text_never_writes_idem(self):
        self.assertIsNone(self.app.handle_message(None, FakeMsg("今晚吃什么")))
        self.assertEqual(self.idem_rows(), [])

    def test_claim_under_write_lock_declines_without_running(self):
        # 另开一条连接持写锁：claim 拿到 `database is locked`，这条就不执行。
        locked = self.open_store(name="playhub.db")
        app_store = Store(self.root / "playhub.db", account="acct", timeout=0.05)
        self.addCleanup(app_store.close)
        app = App(store=app_store, now_fn=lambda: NOW, repo_root=self.root / "app", logger=self.log)

        with locked.tx() as conn:
            conn.execute("SELECT 1").fetchall()               # BEGIN IMMEDIATE 已持锁
            self.assertIsNone(app.handle_message(None, FakeMsg()))
        self.assertEqual(self.ledger_count(), 0)              # 没执行：钱一分没动
        self.assertEqual(self.idem_rows(), [])
        self.assertTrue(
            any(level == "warning" and "幂等键写入失败" in msg for level, (msg, _args) in self.log.records)
        )
        # 锁释放之后同一条消息还能正常处理（没被执行过，也没留下占位）
        self.assertIsNotNone(app.handle_message(None, FakeMsg()))
        self.assertEqual(self.coins(), 30)


class MessageKeyTest(unittest.TestCase):
    """`core/idem.py` 的纯函数口径（不经 app）。"""

    def test_native_id_strips_and_treats_zero_as_empty(self):
        from plugins.playhub.core.idem import native_id

        self.assertEqual(native_id(FakeMsg(native=" abc ")), "abc")
        self.assertEqual(native_id(FakeMsg(native="0")), "")
        self.assertEqual(native_id(FakeMsg(native=None)), "")
        self.assertEqual(native_id(None), "")

    def test_message_key_needs_an_action(self):
        from plugins.playhub.core.idem import message_key

        self.assertIsNone(message_key(FakeMsg(), ""))
        self.assertEqual(message_key(FakeMsg(), "daily.signin"), f"msg:{ROOM}:{NATIVE}:daily.signin")

    def test_message_key_clips_long_keys(self):
        from plugins.playhub.core.idem import MAX_KEY_LENGTH, message_key

        key = message_key(FakeMsg(native="x" * 900), "daily.signin")
        self.assertEqual(len(key), MAX_KEY_LENGTH)


if __name__ == "__main__":
    unittest.main()
