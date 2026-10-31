"""记忆里的情绪子系统：九类关键词、否定词、程度副词、光谱加权与情感弧线。"""
from __future__ import annotations

import unittest

from wxbot_framework import memory


class EmotionKeywordTests(unittest.TestCase):
    def state(self):
        return {"chat_id": "room@chatroom"}

    def test_each_of_the_nine_emotions_is_recognised(self):
        samples = {
            "joy": "今天真开心",
            "moved": "谢谢你一直记着我",
            "missing": "好久不见啊",
            "worry": "这个我有点担心",
            "aggrieved": "又要加班",
            "excited": "终于搞定了",
            "tsundere": "哼，随便你",
            "focus": "这份报错我看看",
            "tired": "好累，撑不住",
        }
        for expected, text in samples.items():
            emotion = memory.update_emotion(self.state(), text)
            self.assertEqual(emotion["name"], expected, msg=text)

    def test_neutral_input_starts_neutral(self):
        self.assertEqual(memory.update_emotion(self.state(), "嗯")["name"], "neutral")

    def test_labels_cover_every_emotion(self):
        for name in memory.EMOTION_KEYWORDS:
            self.assertIn(name, memory.EMOTION_LABELS)


class NegationTests(unittest.TestCase):
    def test_a_negated_word_does_not_count(self):
        self.assertEqual(memory._emotion_score("不开心", memory.EMOTION_KEYWORDS["joy"]), 0.0)

    def test_negation_only_reaches_a_short_window(self):
        body = "不" + "这中间隔了很多字" + "开心"
        self.assertGreater(memory._emotion_score(body, memory.EMOTION_KEYWORDS["joy"]), 0.0)

    def test_negated_joy_does_not_start_the_joy_arc(self):
        emotion = memory.update_emotion({}, "一点也不开心")
        self.assertEqual(emotion["name"], "neutral")
        self.assertEqual(emotion["streak"], 0)


class ModifierTests(unittest.TestCase):
    def test_intensifiers_raise_the_weight(self):
        plain = memory._emotion_score("开心", memory.EMOTION_KEYWORDS["joy"])
        strong = memory._emotion_score("非常开心", memory.EMOTION_KEYWORDS["joy"])
        weak = memory._emotion_score("有点开心", memory.EMOTION_KEYWORDS["joy"])
        self.assertGreater(strong, plain)
        self.assertLess(weak, plain)

    def test_spectrum_boost_pushes_some_emotions_higher(self):
        self.assertEqual(memory._spectrum_boost("不存在的情绪"), 1.0)
        self.assertGreater(memory._spectrum_boost("focus"), 1.0)
        self.assertLessEqual(memory._spectrum_boost("joy"), 1.8)


class ArcTests(unittest.TestCase):
    def test_repeating_an_emotion_builds_a_streak_and_stops_at_one(self):
        state = {}
        for _ in range(6):
            emotion = memory.update_emotion(state, "太开心了")
        self.assertEqual(emotion["streak"], 6)
        self.assertLessEqual(emotion["intensity"], 1.0)

    def test_switching_emotions_keeps_a_little_afterglow(self):
        state = {}
        memory.update_emotion(state, "真开心")
        memory.update_emotion(state, "真开心")
        before = state["emotion"]["intensity"]
        emotion = memory.update_emotion(state, "好累")
        self.assertEqual(emotion["name"], "tired")
        self.assertGreater(emotion["intensity"], 0.0)
        self.assertLess(emotion["intensity"], before)

    def test_history_is_bounded(self):
        state = {}
        for _ in range(30):
            memory.update_emotion(state, "真开心")
        self.assertLessEqual(len(state["emotion"]["history"]), 20)
        self.assertLessEqual(len(state["emotion"]["arc_history"]), memory.ARC_HISTORY_LIMIT)

    def test_a_single_emotion_is_not_a_peak(self):
        state = {}
        memory.update_emotion(state, "真开心")
        self.assertEqual(memory._peak_emotion(state["emotion"]), "")

    def test_quiet_fading_returns_to_neutral(self):
        state = {}
        memory.update_emotion(state, "真开心")
        for _ in range(10):
            emotion = memory.update_emotion(state, "嗯")
        self.assertEqual(emotion["name"], "neutral")
        self.assertEqual(emotion["intensity"], 0.0)


class EmotionNoteTests(unittest.TestCase):
    def test_nothing_is_injected_when_calm(self):
        self.assertEqual(memory.emotion_note({"emotion": {"name": "neutral", "intensity": 0.0}}), "")

    def test_weak_emotion_stays_below_the_note_threshold(self):
        state = {}
        memory.update_emotion(state, "有点担心")
        self.assertLess(state["emotion"]["intensity"], memory.EMOTION_NOTE_MIN)
        self.assertEqual(memory.emotion_note(state), "")

    def test_long_streak_asks_for_a_stronger_show_without_self_report(self):
        state = {}
        for _ in range(3):
            memory.update_emotion(state, "真开心")
        note = memory.emotion_note(state)
        self.assertIn("连着 3 次", note)
        self.assertIn("不要自报情绪", note)

    def test_a_different_peak_is_called_out(self):
        state = {}
        memory.update_emotion(state, "真开心")
        memory.update_emotion(state, "真开心")
        memory.update_emotion(state, "好累")
        memory.update_emotion(state, "好累")
        note = memory.emotion_note(state)
        self.assertIn("最近你多半是", note)


if __name__ == "__main__":
    unittest.main()
