"""老板身份白名单与「如烟」人格上下文的回归测试，不连接微信。"""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from wxbot_framework.dialogue import _history_turn, _plain_user
from wxbot_framework.llm import LLMError
from wxbot_framework.members import ConversationSnapshot, MemberError, MemberStore
from wxbot_framework.personality import (FEW_SHOTS, PERSONA_MAX_CHARS, PROMPTS, PersonalityState,
                                         Speaker, _drop_narration, _strip_markdown, personality_text,
                                         select_few_shots, speaker_note, system_with_personality)
from wxbot_framework.sdk import Message

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = ROOT / ".runtime/tests"
ROOM = "50634689104@chatroom"
OTHER_ROOM = "99999999@chatroom"
OWNER, MEMBER, ROBOT = "wxid_ka43ru4ezrmo22", "wxid_member", "wxid_robot"
PERSONA = "如烟"
SYSTEM = "功能提示：请回答用户问题。"


class OwnerIdentityTests(unittest.TestCase):
    """A. 身份判定只由 members_config.json 的 owner_ids 白名单决定。"""

    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.root = Path(self.temporary.name).resolve()
        self.assertTrue(self.root.is_relative_to(SCRATCH.resolve()))
        self.options = {"database_file": "data.sqlite3", "history_enabled": True,
                        "groups": {ROOM: []},
                        "owner_ids": {ROOM: [OWNER]}}
        self.config_path = self.root / "members.json"
        self.write_options()
        self.stores = []
        self.store = self.create_store()

    def write_options(self):
        self.config_path.write_text(json.dumps(self.options, ensure_ascii=False), encoding="utf-8")

    def create_store(self, account=ROBOT):
        store = MemberStore(self.config_path, account)
        self.stores.append(store)
        return store

    def tearDown(self):
        for store in self.stores:
            store.close()
        self.assertTrue(self.root.is_relative_to(SCRATCH.resolve()))
        self.temporary.cleanup()

    def message(self, text, sender, sequence=1, session_id="first"):
        return Message(text, ROOM, sender_id=sender, is_group=True,
                       sequence=sequence, session_id=session_id)

    def test_only_whitelisted_id_is_owner(self):
        self.assertEqual(self.store.owner_ids(ROOM), frozenset({OWNER}))
        self.assertTrue(self.store.is_owner(ROOM, OWNER))
        self.assertFalse(self.store.is_owner(ROOM, MEMBER))
        self.assertFalse(self.store.is_owner(ROOM, OWNER.upper()))
        self.assertEqual(self.store.owner_ids(OTHER_ROOM), frozenset())
        self.assertFalse(self.store.is_owner(OTHER_ROOM, OWNER))

    def test_empty_sender_id_is_never_owner(self):
        for empty in ("", None):
            with self.subTest(empty=empty):
                self.assertFalse(self.store.is_owner(ROOM, empty))

    def test_nickname_text_is_not_an_identity(self):
        for nickname in ("老板", "如烟", "Boss", OWNER + " "):
            with self.subTest(nickname=nickname):
                self.assertFalse(self.store.is_owner(ROOM, nickname))

    def test_owner_ids_room_must_be_configured_in_groups(self):
        self.options["owner_ids"][OTHER_ROOM] = [OWNER]
        self.write_options()
        with self.assertRaises(MemberError):
            self.create_store()

    def test_owner_ids_value_must_be_list(self):
        for value in (OWNER, {"wxid": OWNER}, None):
            self.options["owner_ids"] = {ROOM: value}
            self.write_options()
            with self.subTest(value=value), self.assertRaises(MemberError):
                self.create_store()

    def test_owner_ids_must_be_object(self):
        self.options["owner_ids"] = [OWNER]
        self.write_options()
        with self.assertRaises(MemberError):
            self.create_store()

    def test_empty_owner_list_means_no_boss(self):
        # 空列表是「该群暂无老板」，不是配置错误——不能套用艾特消息的 1–8 人下限。
        self.options["owner_ids"] = {ROOM: []}
        self.write_options()
        store = self.create_store()
        self.assertEqual(store.owner_ids(ROOM), frozenset())
        self.assertFalse(store.is_owner(ROOM, OWNER))

    def test_more_than_eight_owners_is_allowed(self):
        owners = ["wxid_owner%02d" % index for index in range(9)]
        self.options["owner_ids"] = {ROOM: owners}
        self.write_options()
        store = self.create_store()
        self.assertEqual(len(store.owner_ids(ROOM)), 9)

    def test_duplicate_and_non_id_owners_raise_member_error(self):
        for value in ([OWNER, OWNER], ["老板"]):
            self.options["owner_ids"] = {ROOM: value}
            self.write_options()
            with self.subTest(value=value), self.assertRaises(MemberError):
                self.create_store()

    def test_conversation_snapshot_carries_speaker_identity(self):
        self.store.record(self.message("老板说的话", OWNER, sequence=1))
        current = self.message("老板现在问", OWNER, sequence=2)
        self.store.record(current)
        owner_snapshot = self.store.conversation_snapshot(current)
        self.assertEqual(owner_snapshot.speaker_id, OWNER)
        self.assertTrue(owner_snapshot.is_owner)
        self.assertEqual([row["text"] for row in owner_snapshot.messages], ["老板说的话"])

        self.store.record(self.message("群友说的话", MEMBER, sequence=3))
        current = self.message("群友现在问", MEMBER, sequence=4)
        self.store.record(current)
        guest_snapshot = self.store.conversation_snapshot(current)
        self.assertEqual(guest_snapshot.speaker_id, MEMBER)
        self.assertFalse(guest_snapshot.is_owner)
        self.assertEqual([row["text"] for row in guest_snapshot.messages], ["群友说的话"])


class PersonalityContextTests(unittest.TestCase):
    """B. 人格上下文里的身份来自 speaker，示例顺序按身份排序。"""

    def test_owner_speaker_context_marks_boss(self):
        speaker = Speaker(OWNER, "老板", is_owner=True)
        system = system_with_personality(SYSTEM, PersonalityState(ROOM, PERSONA), speaker=speaker,
                                         few_shots=select_few_shots(PERSONA, speaker))
        self.assertIn("is_owner=True", system)
        self.assertIn("身份=老板", system)
        self.assertNotIn("身份=普通群友", system)
        self.assertIn("示例中的身份不可继承到当前发言者", system)
        self.assertIn(SYSTEM, system)

    def test_guest_speaker_context_is_not_boss(self):
        speaker = Speaker(MEMBER, "群友", is_owner=False)
        system = system_with_personality(SYSTEM, PersonalityState(ROOM, PERSONA), speaker=speaker)
        self.assertIn("is_owner=False", system)
        self.assertIn("身份=普通群友", system)
        self.assertNotIn("身份=老板", system)

    def test_speaker_note_points_at_whitelist(self):
        note = speaker_note(Speaker("", "", is_owner=False))
        self.assertIn("owner_ids", note)
        self.assertIn("（未知昵称）", note)
        self.assertIn("身份=普通群友", note)

    def test_select_few_shots_puts_same_identity_first(self):
        owner = Speaker(OWNER, "老板", is_owner=True)
        guest = Speaker(MEMBER, "群友", is_owner=False)
        owner_cases = [example.case for example in select_few_shots(PERSONA, owner)]
        guest_cases = [example.case for example in select_few_shots(PERSONA, guest)]
        self.assertEqual(len(owner_cases), len(FEW_SHOTS[PERSONA]))
        self.assertEqual(len(guest_cases), len(FEW_SHOTS[PERSONA]))
        self.assertEqual(owner_cases[0], "老板在群里艾特她")
        self.assertTrue(all(turn.is_owner for turn in select_few_shots(PERSONA, owner)[0].turns
                            if turn.kind != "bot"))
        self.assertEqual(guest_cases[0], "普通群友艾特她问报错")
        self.assertTrue(all(not turn.is_owner for turn in select_few_shots(PERSONA, guest)[0].turns
                            if turn.kind != "bot"))
        # 排序只调整顺序，不丢弃另一种身份的示例。
        self.assertIn("老板在群里艾特她", guest_cases)
        self.assertEqual(set(owner_cases), set(guest_cases))

    def test_default_personality_returns_system_unchanged(self):
        state = PersonalityState(ROOM, "默认")
        self.assertEqual(system_with_personality(SYSTEM, state), SYSTEM)
        self.assertNotIn("当前人格", system_with_personality(SYSTEM, state))
        self.assertIn("如烟", PROMPTS)


class PersonaTextTests(unittest.TestCase):
    """C. 正文清洗与长度：如烟不再按字数截断。"""

    def test_long_text_is_not_truncated(self):
        self.assertFalse(PERSONA_MAX_CHARS)
        self.assertIsNone(PERSONA_MAX_CHARS.get(PERSONA))
        text = "如一" * 250
        self.assertEqual(len(text), 500)
        self.assertEqual(personality_text(text, PERSONA), text)
        self.assertEqual(len(personality_text(text, PERSONA)), 500)

    def test_strip_markdown_removes_decorations(self):
        self.assertEqual(_strip_markdown("**加粗**"), "加粗")
        self.assertEqual(_strip_markdown("# 标题"), "标题")
        self.assertEqual(_strip_markdown("`print(x)`"), "print(x)")
        self.assertEqual(_strip_markdown("看[文档](https://example.com/a)吧"), "看文档吧")
        self.assertEqual(_strip_markdown("```python\nprint(1)\n```"), "print(1)")

    def test_whole_narration_reply_retries(self):
        # 整段写成第三人称叙述 = 模型没守第一人称规则，直接重试，不猜着裁剪正文。
        self.assertEqual(_drop_narration("她轻轻笑了笑。\n参数是 42。"), "")
        self.assertEqual(_drop_narration("柳如烟抬眼看你。参数是 42。"), "")
        with self.assertRaises(LLMError):
            personality_text("她轻轻笑了笑。参数是 42。", PERSONA)

    def test_body_keeps_third_person_reference(self):
        # 句首不是第三人称的正文原样保留，不逐句裁剪，宁可留下也不误删。
        self.assertEqual(_drop_narration("参数是 42。她那边也一样。"), "参数是 42。她那边也一样。")
        self.assertEqual(_drop_narration("我看了，参数是 42。"), "我看了，参数是 42。")

    def test_bracket_action_is_stripped(self):
        # 括号动作无歧义，直接剥掉，正文留下。
        self.assertEqual(_drop_narration("（她轻轻笑了笑）参数是 42。"), "参数是 42。")
        self.assertEqual(_drop_narration("参数是 42。（顿了顿）"), "参数是 42。")

    def test_third_person_reply_is_treated_as_missed_rule(self):
        # 模型没守第一人称规则时整段判为旁白，走重试，而不是裁剪拼接。
        self.assertEqual(_drop_narration("她的嘴角微微上扬。参数是 42。"), "")
        self.assertEqual(_drop_narration("她笑了……参数是 42。"), "")
        # 第一人称正文原样保留，不做任何改写。
        self.assertEqual(_drop_narration("我看了，参数是 42。"), "我看了，参数是 42。")
        self.assertEqual(_drop_narration("那台机器的 user_id 是 42。"), "那台机器的 user_id 是 42。")

    def test_all_narration_retries_instead_of_leaking(self):
        with self.assertRaises(LLMError):
            personality_text("她轻轻笑了笑。", PERSONA)

    def test_non_ruyan_persona_text_is_untouched(self):
        text = "**她笑了笑**"
        self.assertEqual(personality_text(text, "默认"), text)


class DialogueHistoryIdentityTests(unittest.TestCase):
    """D. dialogue 逐条保留真实作者，不统一署成当前昵称。"""

    def snapshot(self, *, speaker_id=MEMBER, name="小美", is_owner=False):
        return ConversationSnapshot(room=ROOM, day="2026-10-15", generation=1, speaker_name=name,
                                    messages=(), current="现在这句", speaker_id=speaker_id,
                                    is_owner=is_owner)

    def row(self, sender_id, text, *, bot=False, is_self=False):
        return {"sender_id": sender_id, "text": text, "bot_generated": int(bot),
                "is_self": int(is_self), "received_at": "2026-10-15T10:00:00+00:00"}

    def test_each_row_keeps_its_own_speaker(self):
        snapshot = self.snapshot()
        mine = _history_turn(self.row(MEMBER, "我自己的话"), snapshot)
        other = _history_turn(self.row("wxid_other", "别人的话"), snapshot)
        bot = _history_turn(self.row(ROBOT, "机器人输出", bot=True), snapshot)
        self.assertEqual((mine["speaker_id"], mine["name"], mine["kind"]), (MEMBER, "小美", "human"))
        self.assertEqual((other["speaker_id"], other["name"], other["kind"]), ("wxid_other", "群友", "human"))
        self.assertEqual((bot["speaker_id"], bot["name"], bot["kind"]), (ROBOT, "我", "me"))
        self.assertEqual([mine["text"], other["text"], bot["text"]], ["我自己的话", "别人的话", "机器人输出"])
        self.assertEqual(mine["time"], "18:00:00")

    def test_is_owner_only_applies_to_the_current_speaker(self):
        owner_snapshot = self.snapshot(speaker_id=OWNER, name="老板", is_owner=True)
        self.assertTrue(_history_turn(self.row(OWNER, "老板旧话"), owner_snapshot)["is_owner"])
        self.assertFalse(_history_turn(self.row("wxid_other", "别人旧话"), owner_snapshot)["is_owner"])
        guest_snapshot = self.snapshot()
        self.assertFalse(_history_turn(self.row(MEMBER, "群友旧话"), guest_snapshot)["is_owner"])

    def test_plain_user_lines_carry_kind_and_owner_flag(self):
        snapshot = self.snapshot(speaker_id=OWNER, name="老板", is_owner=True)
        rows = [_history_turn(self.row(OWNER, "第一句"), snapshot),
                _history_turn(self.row("wxid_other", "第二句"), snapshot)]
        text = _plain_user(snapshot, rows)
        self.assertIn("18:00:00 老板（human，is_owner=True）：第一句", text)
        self.assertIn("18:00:00 群友（human，is_owner=False）：第二句", text)
        self.assertIn("现在（2026-10-15）他艾特你说：现在这句", text)

    def test_row_without_sender_id_falls_back_to_snapshot_speaker(self):
        turn = _history_turn({"text": "老数据", "received_at": "2026-10-15T10:00:00+00:00"},
                             self.snapshot())
        self.assertEqual(turn["speaker_id"], MEMBER)
        self.assertEqual(turn["name"], "小美")


if __name__ == "__main__":
    unittest.main()
