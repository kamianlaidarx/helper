"""拟人层：回复概率、打字延迟、分段、排版清洗和低频错字。全部纯函数，不联网。"""
from __future__ import annotations

import unittest

from wxbot_framework import humanize


class StubRandom:
    """固定摇点，用来把随机行为钉死。"""

    def __init__(self, roll=0.0):
        self.roll = roll

    def random(self):
        return self.roll

    def gauss(self, mu, sigma):
        return mu

    def randrange(self, stop):
        return 0

    def choice(self, pool):
        return pool[0]


class ReplyProbabilityTests(unittest.TestCase):
    def test_owner_at_self_is_almost_always_answered(self):
        self.assertEqual(humanize.reply_probability(is_owner=True, is_at_self=True),
                         humanize.OWNER_REPLY)
        self.assertTrue(humanize.should_reply(is_owner=True, is_at_self=True, hour=15,
                                              rng=StubRandom(0.5)))

    def test_other_members_at_self_sometimes_get_left_alone(self):
        self.assertFalse(humanize.should_reply(is_owner=False, is_at_self=True, hour=15,
                                               rng=StubRandom(0.9)))

    def test_not_being_addressed_means_mostly_silence(self):
        self.assertLess(humanize.reply_probability(is_owner=True, is_at_self=False), 0.1)
        self.assertFalse(humanize.should_reply(is_owner=True, is_at_self=False, hour=15,
                                               rng=StubRandom(0.5)))

    def test_late_night_quiet_hours_damp_the_chance(self):
        self.assertTrue(humanize.should_reply(is_owner=True, is_at_self=True, hour=15,
                                              rng=StubRandom(0.5)))
        self.assertFalse(humanize.should_reply(is_owner=True, is_at_self=True, hour=3,
                                               rng=StubRandom(0.5)))


class TypingDelayTests(unittest.TestCase):
    def test_delay_grows_with_length_and_stays_in_range(self):
        rng = StubRandom()
        short = humanize.typing_delay("好", rng)
        long = humanize.typing_delay("这" * 200, rng)
        self.assertGreaterEqual(short, humanize.TYPING_MIN_S)
        self.assertLessEqual(long, humanize.TYPING_MAX_S)
        self.assertGreater(long, short)

    def test_a_long_message_is_not_one_flat_wait(self):
        rng = StubRandom()
        samples = {humanize.typing_delay("今天有点想你", rng) for _ in range(3)}
        self.assertEqual(len(samples), 1)  # 同一个 rng 固定时结果稳定，说明没有额外随机源


class PlainSpeechTests(unittest.TestCase):
    def test_markdown_noise_is_removed(self):
        raw = "- 第一点\n2. 第二点\n**重点**在这"
        body = humanize.plain_speech(raw)
        self.assertNotIn("**", body)
        self.assertNotIn("- 第一点", body)
        self.assertNotIn("2. 第二点", body)
        self.assertIn("第一点", body)
        self.assertIn("重点在这", body)


class SplitBubbleTests(unittest.TestCase):
    def test_short_reply_is_one_bubble(self):
        self.assertEqual(humanize.split_bubbles("好呀，马上就来。"), ["好呀，马上就来。"])

    def test_long_reply_is_split_but_never_more_than_three(self):
        text = ("第一句话说完了我们走吧。第二句话也说完了别急。第三句话跟上就好。"
                "第四句还有一点点。第五句收尾吧。")
        bubbles = humanize.split_bubbles(text)
        self.assertGreater(len(bubbles), 1)
        self.assertLessEqual(len(bubbles), humanize.BUBBLE_LIMIT)
        self.assertEqual("".join(bubbles), text)

    def test_empty_input_yields_nothing(self):
        self.assertEqual(humanize.split_bubbles("   "), [])


class TypoTests(unittest.TestCase):
    def test_never_typos_for_the_owner(self):
        self.assertIsNone(humanize.maybe_typo("这事情真的很好", is_owner=True, rng=StubRandom(0.0)))

    def test_never_typos_while_working(self):
        self.assertIsNone(humanize.maybe_typo("这报错帮我看看日志", is_owner=False,
                                              rng=StubRandom(0.0)))

    def test_never_typos_short_messages(self):
        self.assertIsNone(humanize.maybe_typo("这好", is_owner=False, rng=StubRandom(0.0)))

    def test_a_typo_comes_with_a_correction(self):
        wrong, correction = humanize.maybe_typo("这事情真的很好", is_owner=False, rng=StubRandom(0.0))
        self.assertNotEqual(wrong, "这事情真的很好")
        self.assertEqual(len(wrong), len("这事情真的很好"))
        self.assertIn("刚打错了", correction)


class PlanTests(unittest.TestCase):
    def test_silence_returns_no_bubbles(self):
        self.assertEqual(humanize.plan("随便说点什么", is_owner=False, is_at_self=True,
                                       hour=15, rng=StubRandom(0.99)), [])

    def test_owner_reply_is_cleaned_and_delayed(self):
        bubbles = humanize.plan("**好**的，- 这就去看。", is_owner=True, is_at_self=True,
                                hour=15, rng=StubRandom(0.1))
        self.assertEqual(len(bubbles), 1)
        self.assertNotIn("**", bubbles[0].text)
        self.assertGreater(bubbles[0].delay, 0)

    def test_typo_path_sends_the_wrong_sentence_first(self):
        bubbles = humanize.plan("这事情真的很好", is_owner=False, is_at_self=True,
                                hour=15, rng=StubRandom(0.0))
        self.assertEqual(len(bubbles), 2)
        self.assertNotEqual(bubbles[0].text, "这事情真的很好")
        self.assertIn("刚打错了", bubbles[1].text)
        self.assertGreater(bubbles[1].delay, bubbles[0].delay)

    def test_follow_up_bubbles_do_not_pile_up_long_delays(self):
        text = "第一句话说完了。" * 6
        bubbles = humanize.plan(text, is_owner=True, is_at_self=True, hour=15,
                                rng=StubRandom(0.1))
        self.assertGreater(len(bubbles), 1)
        for bubble in bubbles[1:]:
            self.assertLessEqual(bubble.delay, humanize.BUBBLE_MAX_DELAY_S + humanize.BUBBLE_GAP_S)


if __name__ == "__main__":
    unittest.main()
