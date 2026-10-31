"""成长层：到期判定、覆盖写、整句截断、回滚，以及扫盘时读真实 chat_id。"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from wxbot_framework import evolution, memory


def state_with(facts=6, growth=None):
    return {"chat_id": "room@chatroom",
            "facts": [{"text": "事实%d" % index, "importance": 3, "last_seen": "2026-01-01"}
                      for index in range(facts)],
            "people": {},
            "growth": growth or evolution.empty()}


class DueTests(unittest.TestCase):
    def test_not_due_without_enough_material(self):
        now = datetime.now(timezone.utc)
        self.assertFalse(evolution.due(state_with(facts=2), now=now))

    def test_due_when_never_ran_and_there_is_material(self):
        self.assertTrue(evolution.due(state_with(), now=datetime.now(timezone.utc)))

    def test_not_due_again_within_the_interval(self):
        now = datetime.now(timezone.utc)
        state = state_with()
        evolution.apply(state, "第一版", now=now)
        self.assertFalse(evolution.due(state, now=now + timedelta(hours=2)))
        self.assertTrue(evolution.due(state, now=now + timedelta(hours=25)))

    def test_broken_timestamp_counts_as_never_ran(self):
        state = state_with(growth={"text": "旧的", "v": 1, "updated_at": "不是时间", "prev": ""})
        self.assertTrue(evolution.due(state, now=datetime.now(timezone.utc)))


class ApplyTests(unittest.TestCase):
    def test_apply_overwrites_and_bumps_version(self):
        state = state_with()
        evolution.apply(state, "第一版", now=datetime.now(timezone.utc))
        growth = evolution.apply(state, "第二版", now=datetime.now(timezone.utc))
        self.assertEqual(growth["text"], "第二版")
        self.assertEqual(growth["prev"], "第一版")
        self.assertEqual(growth["v"], 2)

    def test_long_text_is_cut_on_a_sentence_boundary(self):
        state = state_with()
        body = "这句话还算正常。" * 100  # 800 字，远超上限
        growth = evolution.apply(state, body, now=datetime.now(timezone.utc))
        self.assertLessEqual(len(growth["text"]), evolution.GROWTH_MAX_CHARS)
        self.assertTrue(growth["text"].endswith("。"))

    def test_whitespace_only_input_changes_nothing(self):
        state = state_with()
        growth = evolution.apply(state, "   \n  ", now=datetime.now(timezone.utc))
        self.assertEqual(growth["text"], "")
        self.assertEqual(growth["v"], 0)

    def test_rollback_restores_the_previous_version(self):
        state = state_with()
        evolution.apply(state, "第一版", now=datetime.now(timezone.utc))
        evolution.apply(state, "第二版", now=datetime.now(timezone.utc))
        self.assertTrue(evolution.rollback(state))
        self.assertEqual(state["growth"]["text"], "第一版")
        self.assertEqual(state["growth"]["v"], 1)

    def test_rollback_without_history_reports_false(self):
        self.assertFalse(evolution.rollback(state_with()))


class NoteTests(unittest.TestCase):
    def test_empty_growth_adds_nothing_to_the_prompt(self):
        self.assertEqual(evolution.note(state_with()), "")

    def test_note_frames_growth_as_self_learned(self):
        state = state_with()
        evolution.apply(state, "在这群人面前话短一点。", now=datetime.now(timezone.utc))
        block = evolution.note(state)
        self.assertIn("自己琢磨出来的", block)
        self.assertIn("在这群人面前话短一点。", block)


class MachineTests(unittest.TestCase):
    def test_build_user_lists_facts_and_people(self):
        state = state_with()
        state["people"] = {"wxid_a": {"name": "老张", "affinity": 0.8, "interactions": 12}}
        text = evolution.build_user(state)
        self.assertIn("老张", text)
        self.assertIn("很熟", text)
        self.assertIn("事实0", text)

    def test_build_user_adds_my_own_recent_lines(self):
        members = Mock()
        members.said.return_value = ["行", "这不太对吧？"]
        text = evolution.build_user(state_with(), members=members)
        members.said.assert_called_once_with("room@chatroom", evolution.SAMPLE_SAID)
        self.assertIn("你最近在这个聊天里说过的话", text)
        self.assertIn("- 行", text)
        self.assertIn("- 这不太对吧？", text)

    def test_build_user_without_members_or_samples_changes_nothing(self):
        quiet = Mock()
        quiet.said.return_value = []
        self.assertNotIn("你最近在这个聊天里说过的话", evolution.build_user(state_with()))
        self.assertNotIn("你最近在这个聊天里说过的话", evolution.build_user(state_with(), members=quiet))

    def test_build_user_survives_a_members_failure(self):
        broken = Mock()
        broken.said.side_effect = RuntimeError("读历史挂了")
        text = evolution.build_user(state_with(), members=broken)
        self.assertNotIn("你最近在这个聊天里说过的话", text)
        self.assertIn("事实0", text)

    def test_consolidate_skips_when_not_due(self):
        client = self.client({"growth": "不该被用到"})
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(memory, "STATE_ROOT", Path(folder)):
            memory.save("room@chatroom", state_with(facts=2))
            self.assertFalse(evolution.consolidate(client, "room@chatroom"))
            self.assertEqual(client.calls, 0)

    def test_consolidate_writes_once_and_never_twice(self):
        client = self.client({"growth": "在这群人面前话短一点。"})
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(memory, "STATE_ROOT", Path(folder)):
            memory.save("room@chatroom", state_with())
            self.assertTrue(evolution.consolidate(client, "room@chatroom"))
            self.assertEqual(client.calls, 1)
            saved = memory.load("room@chatroom")
            self.assertIn("话短一点", saved["growth"]["text"])
            self.assertFalse(evolution.consolidate(client, "room@chatroom"))
            self.assertEqual(client.calls, 1)

    def test_sweep_reads_the_real_chat_id_from_disk(self):
        client = self.client({"growth": "写点新的。"})
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(memory, "STATE_ROOT", Path(folder)):
            memory.save("room@chatroom", state_with())
            evolution.sweep(client)
            saved = json.loads((Path(folder) / memory._safe("room@chatroom") / "memory.json")
                               .read_text(encoding="utf-8"))
            self.assertEqual(saved["chat_id"], "room@chatroom")
            self.assertIn("写点新的", saved["growth"]["text"])

    def test_consolidate_and_sweep_pass_members_into_the_material(self):
        client = self.client({"growth": "写点新的。"})
        members = Mock()
        members.said.return_value = ["嗯"]
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(memory, "STATE_ROOT", Path(folder)):
            memory.save("room@chatroom", state_with())
            self.assertTrue(evolution.consolidate(client, "room@chatroom", members=members))
            members.said.assert_called_once_with("room@chatroom", evolution.SAMPLE_SAID)
            memory.save("room@chatroom", state_with())
            members.said.reset_mock()
            evolution.sweep(client, members=members)
            self.assertTrue(members.said.called)

    def test_client_failure_is_swallowed(self):
        client = self.client(None, error=RuntimeError("接口挂了"))
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(memory, "STATE_ROOT", Path(folder)):
            memory.save("room@chatroom", state_with())
            self.assertFalse(evolution.consolidate(client, "room@chatroom"))

    @staticmethod
    def client(result, error=None):
        class Client:
            def __init__(self):
                self.calls = 0

            def complete_json(self, system, user):
                self.calls += 1
                if error is not None:
                    raise error
                return result

        return Client()


if __name__ == "__main__":
    unittest.main()
