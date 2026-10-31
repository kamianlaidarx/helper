"""表达层接线：面具、前世碎片、成长块真的会拼进 system，且非老板看不到。"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from wxbot_framework import evolution, mask, memory
from wxbot_framework.personality import fragment_note
from wxbot_framework.sdk import PluginContext


def message(text="随便聊聊", chat_id="room@chatroom"):
    return SimpleNamespace(chat_id=chat_id, text=text, is_group=True, sender_id="wxid_a")


class ExpressionExtrasTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        patcher = patch.object(memory, "STATE_ROOT", Path(self.folder.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def extras(self, msg, *, is_owner=True):
        speaker = SimpleNamespace(is_owner=is_owner, speaker_id="wxid_a", name="测试成员")
        return PluginContext.expression_extras(None, msg, speaker)

    def test_nothing_is_injected_for_a_fresh_chat(self):
        self.assertEqual(self.extras(message()), [])

    def test_mask_block_only_reaches_the_owner(self):
        state = memory.load("room@chatroom")
        mask.update(state, "她在宗门里修行")
        mask.update(state, "又是宗门的事")
        memory.save("room@chatroom", state)
        self.assertEqual(self.extras(message(), is_owner=False), [])
        self.assertTrue(any("面具" in block for block in self.extras(message(), is_owner=True)))

    def test_growth_block_is_shared_with_everyone(self):
        state = memory.load("room@chatroom")
        evolution.apply(state, "在这群人面前少说两句。", now=datetime.now(timezone.utc))
        memory.save("room@chatroom", state)
        for owner in (True, False):
            blocks = self.extras(message(), is_owner=owner)
            self.assertTrue(any("在这个聊天里慢慢长成" in block for block in blocks), msg=str(owner))

    def test_fragment_only_fires_for_the_owner_and_matching_text(self):
        self.assertEqual(self.extras(message("我记得那年打坐了很久"), is_owner=False), [])
        blocks = self.extras(message("我记得那年打坐了很久"), is_owner=True)
        self.assertTrue(any("前世碎片" in block for block in blocks))

    def test_owner_without_matching_text_gets_no_fragment(self):
        self.assertEqual(fragment_note("今天天气不错"), "")

    def test_everyday_words_do_not_summon_a_past_life(self):
        # 单字关键词（等/月/光/照/刀）会把每句话都判成前世回声，已收紧。
        for text in ("等一下我看看", "这个月有点忙", "照片发你了", "阳光不错"):
            self.assertEqual(fragment_note(text), "", msg=text)

    def test_broken_message_never_raises(self):
        self.assertEqual(self.extras(SimpleNamespace(chat_id=None, text=None), is_owner=True), [])


if __name__ == "__main__":
    unittest.main()
