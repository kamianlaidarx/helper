"""小V 风格卡面单测：`core/render.py` 的排版原语、`core/menus.py` 的 47 张触发卡、
以及 `app.py`「裸发回卡」的接线（`docs/design/playhub.md` §4.5）。

覆盖四块：

1. `render` 原子：`banner` / `current` / `cmd` / `card` / `limit` 的输出形状；
2. 卡面自检：每个动作一张卡、行数 ≤ 10、**任何一行都不等于触发词**（`§2.6.5` 自触发不变量）；
3. 覆盖完整性：凡是 `needs_arg` 或 `bare_behavior="guide"` 的动作都必须有卡，
   否则裸发还是静默，用户看不出有这个玩法；
4. `app` 行为：开关打开 → 裸发回卡；开关关闭 → 仍然静默；不需要参数的词（签到/排行榜）
   裸发照常执行，不被卡片挡住。
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from plugins.playhub.app import App
from plugins.playhub.core import Store
from plugins.playhub.core import menus, render
from plugins.playhub.core.clock import CN
from plugins.playhub.keywords import TRIGGERS

ROOM = "12345678@chatroom"
UID = "wxid_player_a"
NICK = "小明"
NOW = int(datetime.fromisoformat("2026-11-01").replace(hour=12, tzinfo=CN).timestamp())


class FakeMsg:
    def __init__(self, text="", *, chat_id=ROOM, sender_id=UID, nickname=NICK, is_self=False):
        self.text = text
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.is_self = is_self
        self.data = {"sender_wechat_name": nickname}


class RenderTests(unittest.TestCase):
    def test_banner(self):
        self.assertEqual(render.banner("💘", "结婚"), "=====💘结婚💘=====")
        self.assertEqual(render.banner("📅", "签到", pad=3), "===📅签到📅===")

    def test_current_always_says_current(self):
        # 小V 的每条结果都回显「当前 X：N」，只报增量无法判断是否到账。
        self.assertEqual(render.current("💰", "金币", 130), "💰当前金币：130")

    def test_cmd_row(self):
        self.assertEqual(render.cmd("❤", "@对象 求婚"), "❤【@对象 求婚】")
        self.assertEqual(render.cmd("❤", "求婚", "向对方求婚"), "❤【求婚】向对方求婚")

    def test_at(self):
        self.assertEqual(render.at("小明"), "@小明")
        self.assertEqual(render.at(""), "")

    def test_card_shape(self):
        text = render.Card("📅", "签到", ("📅【签到】每天一次",), note="一天一次").render()
        lines = text.split("\n")
        self.assertEqual(lines[0], "=====📅签到📅=====")
        self.assertEqual(lines[1], "📅【签到】每天一次")
        self.assertEqual(lines[2], render.rule())
        self.assertEqual(lines[3], "⚠注：一天一次")

    def test_limit_keeps_head_and_tail(self):
        lines = [f"第{i}行" for i in range(1, 21)]
        out = render.limit("\n".join(lines), max_lines=6).split("\n")
        self.assertEqual(len(out), 6)
        self.assertEqual(out[0], "第1行")
        self.assertIn("略", out[3])
        self.assertEqual(out[-1], "第20行")

    def test_limit_chars(self):
        out = render.limit("啊" * 2000, max_chars=100)
        self.assertEqual(len(out), 100)
        self.assertTrue(out.endswith(render.ELLIPSIS))


class MenuTests(unittest.TestCase):
    def test_every_menu_has_banner_and_bounded_rows(self):
        for action, text in menus.MENUS.items():
            with self.subTest(action=action):
                lines = text.split("\n")
                self.assertTrue(lines[0].startswith("====="), lines[0])
                # 10 行正文 + 装饰行；超出说明该拆子命令，而不是发更长的卡。
                self.assertLessEqual(len(lines), 14, f"{action} 卡太长")

    def test_no_menu_line_is_a_trigger(self):
        """§2.6.5 自触发不变量：卡面任何一行归一化后都不等于触发词。"""
        from plugins.playhub.textkit import normalize

        words = {normalize(t.keyword) for t in TRIGGERS}
        for action, text in menus.MENUS.items():
            for line in text.split("\n"):
                with self.subTest(action=action, line=line):
                    self.assertNotIn(normalize(line), words)

    def test_menu_actions_are_real_actions(self):
        known = {t.action for t in TRIGGERS}
        for action in menus.MENUS:
            with self.subTest(action=action):
                self.assertIn(action, known)

    def test_every_arg_or_guide_action_has_a_card(self):
        for trigger in TRIGGERS:
            if trigger.owner != "playhub":
                continue
            if trigger.needs_arg or trigger.bare_behavior == "guide":
                with self.subTest(keyword=trigger.keyword):
                    self.assertIn(trigger.action, menus.MENUS)

    def test_pending_flag(self):
        # 礼物 / 配对 / 关系卡 / 宝宝 第 3 批已上线，只剩批内其他玩法还挂着施工中
        self.assertTrue(menus.is_pending("music.request"))
        self.assertFalse(menus.is_pending("social.marry"))
        self.assertFalse(menus.is_pending("social.gift"))
        self.assertIsNone(menus.menu_for("no.such.action"))

    def test_every_playhub_action_has_a_card(self):
        """43 个 playhub 动作一人一张卡；缺卡的动作裸发就是静默，用户看不出有这玩法。"""
        playhub_actions = {t.action for t in TRIGGERS if t.owner == "playhub"}
        self.assertEqual(set(menus.MENUS), playhub_actions)


class BareTriggerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = Store(self.root / "playhub.db", account="acct")
        self.store.migrate()
        self.addCleanup(self.store.close)
        self.app = App(store=self.store, now_fn=lambda: NOW, repo_root=self.root / "repo")

    def say(self, text: str):
        return self.app.handle_message(None, FakeMsg(text))

    def enable(self, name: str) -> None:
        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO kv (room, key, value, updated_at) VALUES (?, ?, ?, ?)",
                (ROOM, f"feature.{name}", "on", NOW),
            )

    def test_switch_off_stays_silent(self):
        # 五个开关默认关 → 裸发静默；礼物/配对没有开关，裸发回触发卡（见 test_gift_bare_shows_card）
        for word in ("结婚", "闺蜜", "结拜", "雇佣", "纳妾", "宝宝"):
            with self.subTest(word=word):
                self.assertIsNone(self.say(word))

    def test_switch_on_returns_card(self):
        self.enable("marriage")
        reply = self.say("结婚")
        self.assertIsNotNone(reply)
        self.assertEqual(reply.action, "social.marry")
        self.assertIn("求婚", reply.text)
        self.assertIn("520", reply.text)
        # 结果卡必须能区分「这是说明书」与「这是执行结果」：卡面带注释行。
        self.assertIn("⚠注：", reply.text)

    def test_run_type_words_still_execute(self):
        # 不需要参数的词裸发是执行，不是回卡：签到要给钱、排行榜要给榜。
        reply = self.say("签到")
        self.assertIsNotNone(reply)
        self.assertIn("金币", reply.text)
        self.assertNotIn("⚠注：", reply.text)

        board = self.say("排行榜")
        self.assertIsNotNone(board)
        # 排行榜裸发是查榜（空榜也给一行提示），不是触发卡：卡面里没有 `【子命令】` 行。
        self.assertNotIn("【", board.text)

    def test_arg_word_with_argument_is_not_a_card(self):
        self.enable("kinship")
        reply = self.say("闺蜜 @小红")
        self.assertIsNotNone(reply)
        self.assertNotIn("=====", reply.text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
