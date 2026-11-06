"""第 3 批第二批引擎单测：`lounge`（文案/群空间/聊天）、`duel`（切磋）、
`music`（点歌队列）、`outbound`（天气，离线占位）。

都走 `App.handle_message`，即从关键词一路到回包，不用手搓 `Command`——
这样顺带验证了绑定表与引擎名对得上。不联网、不 sleep、不写仓库外路径。
"""
from __future__ import annotations

import pathlib
import tempfile
import unittest
from datetime import datetime

from plugins.playhub.app import App
from plugins.playhub.core import Store, credit, ensure_player, kv, switches
from plugins.playhub.core.clock import CN

ROOM = "12345678@chatroom"
UID_A = "wxid_a"
NAME_A = "小明"
UID_B = "wxid_b"
NAME_B = "小红"

DAY = 86400


class FixedRng:
    """`random() < 0.5` 恒为真、`randrange(n)` 恒 0，胜负可断言。"""

    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def random(self) -> float:
        return self.value

    def randrange(self, n: int) -> int:
        return 0 if n else 0


class FakeMsg:
    def __init__(self, text: str = "", *, chat_id: str = ROOM, sender_id: str = UID_A, nickname: str = NAME_A):
        self.text = text
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.is_self = False
        self.data = {"sender_wechat_name": nickname}


class FakeCtx:
    """成员表只登记两个人；未登记的名字抛 KeyError，引擎应回「没认出」。"""

    class _Members:
        table = {NAME_A: UID_A, NAME_B: UID_B}

        def resolve(self, name, room=None):
            if name in self.table:
                return type("M", (), {"wxid": self.table[name]})()
            raise KeyError(name)

    def __init__(self) -> None:
        self.members = self._Members()


class FakeClock:
    def __init__(self) -> None:
        self.ts = int(datetime.fromisoformat("2026-11-01T12:00:00").replace(tzinfo=CN).timestamp())

    def __call__(self) -> int:
        return self.ts

    def advance(self, seconds: int = 1) -> None:
        self.ts += seconds


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self.store = Store(self.tmp / "playhub.sqlite3", account="acct")
        self.store.migrate()
        self.clock = FakeClock()
        self.app = App(store=self.store, rng=FixedRng(), now_fn=self.clock, repo_root=self.tmp)
        self.ctx = FakeCtx()

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def say(self, text: str, *, uid: str = UID_A, nickname: str = NAME_A) -> str:
        reply = self.app.handle_message(self.ctx, FakeMsg(text, sender_id=uid, nickname=nickname))
        self.assertIsNotNone(reply, f"「{text}」应当有回包")
        return reply.text

    def quiet(self, text: str) -> None:
        self.assertIsNone(self.app.handle_message(self.ctx, FakeMsg(text)), f"「{text}」应当静默")

    def write_copy_bank(self, rows: str) -> None:
        content = self.tmp / "content"
        content.mkdir(parents=True, exist_ok=True)
        (content / "copy.jsonl").write_text(rows, encoding="utf-8")


class LoungeCopyTests(Base):
    ROWS = (
        '{"id":"copy-0001","tag":["日常"],"text":"第一条日常"}\n'
        '{"id":"copy-0002","tag":["日常"],"text":"第二条日常"}\n'
        '{"id":"copy-0003","tag":["情话"],"text":"唯一一条情话"}\n'
    )

    def test_rotates_in_order(self) -> None:
        self.write_copy_bank(self.ROWS)
        first = self.say("文案 日常")
        second = self.say("文案 日常")
        self.assertNotEqual(first, second, "同一主题应当轮转，不该重复同一条")
        self.assertTrue(first.startswith("@小明\n📝 文案 · 日常"), first)
        # 游标先自增再取模，所以第一次落在第 2 条；只要在本主题池内即可
        self.assertIn(first.splitlines()[2], ("📝 第一条日常", "📝 第二条日常"))

    def test_tag_filter(self) -> None:
        self.write_copy_bank(self.ROWS)
        for _ in range(3):
            text = self.say("文案 情话")
            self.assertIn("唯一一条情话", text)

    def test_unknown_tag_shows_usage(self) -> None:
        self.write_copy_bank(self.ROWS)
        self.assertIn("用法：文案", self.say("文案 不存在"))

    def test_bare_copy_returns_menu_card(self) -> None:
        """裸发只回说明书，不进引擎——文案库缺失时也不该抛错。"""
        self.assertIn("文案", self.say("文案"))

    def test_missing_bank_says_not_ready(self) -> None:
        self.assertIn("文案库还没准备好", self.say("文案 日常"))


class LoungeSpaceTests(Base):
    def test_counts_and_cache(self) -> None:
        first = self.say("群空间")
        self.assertIn("群空间", first)
        self.assertIn("群成员档案：0 人", first)
        # 同一小时内再发起一次：走缓存，成员数仍为 0
        with self.store.tx() as conn:
            ensure_player(conn, room=ROOM, uid=UID_A, nickname=NAME_A, now=self.clock())
        self.assertIn("群成员档案：0 人", self.say("群空间"))
        # 换一小时：缓存键变了，能看到新成员
        self.clock.advance(3600)
        self.assertIn("群成员档案：1 人", self.say("群空间"))

    def test_sums_wallet(self) -> None:
        with self.store.tx() as conn:
            ensure_player(conn, room=ROOM, uid=UID_A, nickname=NAME_A, now=self.clock())
            credit(conn, room=ROOM, uid=UID_A, currency="coin", amount=70, reason="t",
                   operation_key="t:coin", posting_no=1, now=self.clock())
            credit(conn, room=ROOM, uid=UID_A, currency="diamond", amount=3, reason="t",
                   operation_key="t:diamond", posting_no=1, now=self.clock())
        text = self.say("群空间")
        self.assertIn("群内金币总量：70", text)
        self.assertIn("群内钻石总量：3", text)


class LoungeChatTests(Base):
    def test_bare_chat_returns_menu_card(self) -> None:
        """裸发「聊天」回说明书；带参数才进引擎。"""
        self.assertIn("智能聊天要单独配一家模型服务", self.say("聊天"))

    def test_chat_says_not_connected(self) -> None:
        text = self.say("聊天 今天好累")
        self.assertIn("智能聊天还没接上", text)
        self.assertIn("不假装会聊", text)


class DuelTests(Base):
    def test_challenge_then_accept(self) -> None:
        text = self.say("切磋 @小红")
        self.assertIn("切磋挑战已发出", text)
        self.assertIn("10 分钟内有效", text)
        result = self.say("切磋 同意", uid=UID_B, nickname=NAME_B)
        self.assertIn("切磋结束", result)
        self.assertIn("胜方：", result)
        self.assertIn("败方：", result)

    def test_reject(self) -> None:
        self.say("切磋 @小红")
        self.assertIn("已拒绝这场切磋", self.say("切磋 拒绝", uid=UID_B, nickname=NAME_B))

    def test_expired(self) -> None:
        self.say("切磋 @小红")
        self.clock.advance(601)
        self.assertIn("已经过期", self.say("切磋 同意", uid=UID_B, nickname=NAME_B))

    def test_no_pending(self) -> None:
        self.assertIn("等你应战的切磋", self.say("切磋 同意", uid=UID_B, nickname=NAME_B))

    def test_self_target_and_unknown(self) -> None:
        self.assertIn("自己跟自己", self.say("切磋 @小明"))
        self.assertIn("没认出", self.say("切磋 @张三"))

    def test_duplicate_challenge(self) -> None:
        self.say("切磋 @小红")
        self.assertIn("还没结束", self.say("切磋 @小红"))

    def test_no_coin_moved(self) -> None:
        with self.store.tx() as conn:
            ensure_player(conn, room=ROOM, uid=UID_A, nickname=NAME_A, now=self.clock())
            credit(conn, room=ROOM, uid=UID_A, currency="coin", amount=100, reason="t",
                   operation_key="t:c", posting_no=1, now=self.clock())
        self.say("切磋 @小红")
        self.say("切磋 同意", uid=UID_B, nickname=NAME_B)
        with self.store.tx() as conn:
            row = conn.execute(
                "SELECT balance FROM wallet WHERE room = ? AND uid = ? AND currency = 'coin'", (ROOM, UID_A)
            ).fetchone()
        self.assertEqual(row[0], 100, "切磋不该动金币")


class MusicTests(Base):
    def test_request_and_position(self) -> None:
        text = self.say("点歌 晴天")
        self.assertIn("已点：《晴天》", text)
        self.assertIn("第 1 位", text)
        self.assertIn("不发音频", text)

    def test_cooldown(self) -> None:
        self.say("点歌 晴天")
        self.assertIn("缓一缓", self.say("点歌 七里香"))
        self.clock.advance(31)
        self.assertIn("已点：《七里香》", self.say("点歌 七里香"))

    def test_queue_cap(self) -> None:
        for i in range(5):
            self.say(f"点歌 歌{i}", uid=f"wxid_{i}", nickname=f"人{i}")
        self.assertIn("点歌台满了", self.say("点歌 第六首", uid="wxid_9", nickname="人9"))

    def test_truncates_long_title(self) -> None:
        text = self.say("点歌 " + "长" * 60)
        self.assertIn("长" * 40, text)
        self.assertNotIn("长" * 41, text)


class OutboundWeatherTests(Base):
    def test_offline_says_not_connected(self) -> None:
        text = self.say("天气 成都")
        self.assertIn("天气服务还没配置", text)
        self.assertIn("不编", text.replace("绝不编", "不编"))

    def test_bare_weather_returns_card(self) -> None:
        text = self.say("天气")
        self.assertIn("天气", text)

    def test_switch_does_not_gate_weather(self) -> None:
        with self.store.tx() as conn:
            kv.put(conn, ROOM, switches.kv_key(switches.MARRIAGE), "off", self.clock())
        self.assertIn("天气服务还没配置", self.say("天气 成都"))


if __name__ == "__main__":
    unittest.main()
