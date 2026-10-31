"""面具共振状态机：共振累积、分级、衰减，以及只对老板显形。"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from wxbot_framework import mask


class MaskResonanceTests(unittest.TestCase):
    def state(self):
        return {"chat_id": "room@chatroom", "mask": mask.empty()}

    def test_empty_state_is_always_default_and_silent(self):
        state = self.state()
        self.assertEqual(mask.current(state), ("default", mask.LEVEL_NONE))
        self.assertEqual(mask.note(state, is_owner=True), "")
        self.assertEqual(mask.update(state, "今天吃什么"), "")

    def test_one_contact_is_not_enough_to_show(self):
        state = self.state()
        self.assertEqual(mask.update(state, "她在宗门里修行"), "saintess")
        self.assertEqual(mask.current(state)[1], mask.LEVEL_NONE)
        self.assertEqual(mask.note(state, is_owner=True), "")

    def test_repeated_contact_raises_the_level_step_by_step(self):
        state = self.state()
        text = "她在宗门里修行，道心很稳"  # 一条消息只涨一格，跟命中几组无关
        mask.update(state, text)
        mask.update(state, text)
        self.assertEqual(mask.current(state), ("saintess", mask.LEVEL_FRAGMENT))
        mask.update(state, text)
        mask.update(state, text)
        self.assertEqual(mask.current(state), ("saintess", mask.LEVEL_TRANCE))
        for _ in range(6):
            mask.update(state, text)
        # 圣女没有觉醒态，再高的共振也只到恍惚。
        self.assertEqual(mask.current(state), ("saintess", mask.LEVEL_TRANCE))

    def test_only_emperor_ever_awakens(self):
        state = self.state()
        for _ in range(10):
            mask.update(state, "命运和宿命有什么意义")
        self.assertEqual(mask.current(state), ("emperor", mask.LEVEL_AWAKEN))
        block = mask.note(state, is_owner=True)
        self.assertIn("本座", block)
        self.assertIn("不要说明自己发生了什么", block)

    def test_resonance_is_capped(self):
        state = self.state()
        for _ in range(40):
            mask.update(state, "掌门说宗门要渡劫飞升")
        self.assertLessEqual(state["mask"]["resonance"]["saintess"], mask.RESONANCE_MAX)

    def test_mask_is_hidden_from_everyone_but_the_owner(self):
        state = self.state()
        for _ in range(4):
            mask.update(state, "前任和背叛这种事，算了")
        self.assertEqual(mask.current(state)[0], "divorcee")
        self.assertEqual(mask.note(state, is_owner=False), "")
        self.assertIn("【面具：divorcee（离人）】", mask.note(state, is_owner=True))

    def test_fragment_trance_and_awakening_read_differently(self):
        state = self.state()
        self.assertEqual(mask.update(state, "她一个人在深夜睡不着"), "moonlight")
        self.assertEqual(mask.update(state, "又是深夜"), "moonlight")
        fragment = mask.note(state, is_owner=True)
        self.assertIn(mask.MASK_DATA["moonlight"]["fragment"], fragment)
        for _ in range(3):
            mask.update(state, "深夜还没睡")
        trance = mask.note(state, is_owner=True)
        self.assertIn(mask.MASK_DATA["moonlight"]["trance"], trance)
        self.assertIn("被问起不要承认", trance)

    def test_decay_fades_after_idle_time_and_can_clear(self):
        state = self.state()
        for _ in range(4):
            mask.update(state, "宗门修行")
        self.assertEqual(mask.current(state)[0], "saintess")
        later = datetime.now(timezone.utc) + timedelta(days=3)
        changed = mask.decay(state, now=later)
        self.assertGreater(changed, 0)
        self.assertEqual(mask.current(state), ("default", mask.LEVEL_NONE))

    def test_decay_does_nothing_before_the_idle_window(self):
        state = self.state()
        mask.update(state, "宗门修行")
        mask.update(state, "宗门修行")
        later = datetime.now(timezone.utc) + timedelta(hours=1)
        self.assertEqual(mask.decay(state, now=later), 0)
        self.assertEqual(mask.current(state)[0], "saintess")

    def test_broken_state_is_rebuilt_instead_of_raising(self):
        state = {"mask": "垃圾数据"}
        self.assertEqual(mask.current(state), ("default", mask.LEVEL_NONE))
        self.assertEqual(mask.update(state, "宗门修行"), "saintess")
        self.assertIsInstance(state["mask"], dict)

    def test_ties_stay_where_they_are_instead_of_flipping(self):
        state = self.state()
        mask.update(state, "宗门修行")
        mask.update(state, "宗门修行")          # saintess 2，已经是当前面具
        self.assertEqual(mask.current(state)[0], "saintess")
        mask.update(state, "深夜睡不着")
        mask.update(state, "深夜睡不着")        # moonlight 2，与 saintess 并列
        self.assertEqual(mask.current(state)[0], "saintess")
        mask.update(state, "深夜")              # moonlight 3，明确领先才换
        self.assertEqual(mask.current(state)[0], "moonlight")


if __name__ == "__main__":
    unittest.main()
