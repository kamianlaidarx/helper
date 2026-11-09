"""成员画像：意图识别、取样清洗、提示词与结果清洗，以及插件里的接线。"""
import json
import tempfile
import unittest
from pathlib import Path

from plugins.mention_chat import Plugin as ChatPlugin
from plugins.mention_chat import portrait
from wxbot_framework.members import MemberError, MemberStore, SnapshotExpired
from wxbot_framework.personality import PersonalityState
from wxbot_framework.sdk import Message

ROOM = "12345678@chatroom"
ACCOUNT = "wxid_bot"
CAT = "wxid_cat"
DOG = "wxid_dog"


class IntentTests(unittest.TestCase):
    def test_question_forms_are_recognized(self):
        for text in ("你觉得 是个什么样的人", "他是个怎样的人", "这人怎么样", "什么性格", "人品怎么样"):
            self.assertIsNotNone(portrait.intent(text), text)

    def test_image_forms_want_a_picture(self):
        for text in ("画一张 的画像", "给 画个像", "长什么样"):
            request = portrait.intent(text)
            self.assertIsNotNone(request, text)
            self.assertTrue(request.wants_image, text)

    def test_plain_question_does_not_want_a_picture(self):
        self.assertFalse(portrait.intent("你觉得 是个什么样的人").wants_image)

    def test_unrelated_text_is_ignored(self):
        for text in ("今天天气不错", "", "   ", None, 5, "帮我画一只猫"):
            self.assertIsNone(portrait.intent(text), repr(text))


class SampleTests(unittest.TestCase):
    def rows(self, *texts):
        return [{"text": text} for text in texts]

    def test_noise_and_short_text_are_dropped(self):
        rows = self.rows("哈哈哈", "收到", "？", "嗯", "我昨天去看了一个特别冷的展，馆里只有三个人")
        self.assertEqual(portrait.sample(rows), ["我昨天去看了一个特别冷的展，馆里只有三个人"])

    def test_duplicates_and_links_are_dropped(self):
        rows = self.rows("这个方案我觉得还能再省一半钱", "这个方案我觉得还能再省一半钱",
                         "https://example.com/abc")
        self.assertEqual(portrait.sample(rows), ["这个方案我觉得还能再省一半钱"])

    def test_order_is_oldest_first(self):
        rows = self.rows("第三句有内容的发言", "第二句有内容的发言", "第一句有内容的发言")
        self.assertEqual(portrait.sample(rows), ["第一句有内容的发言", "第二句有内容的发言", "第三句有内容的发言"])

    def test_limit_keeps_the_newest(self):
        # member_messages 给的是新→旧；取样只留最新的，最后再翻回时间顺序。
        rows = self.rows(*[f"第{index}句有内容的发言" for index in range(1, 8)])
        self.assertEqual(portrait.sample(rows, limit=3),
                         ["第3句有内容的发言", "第2句有内容的发言", "第1句有内容的发言"])

    def test_long_line_is_cut(self):
        line = "很长" * 200
        self.assertEqual(len(portrait.sample(self.rows(line))[0]), portrait.MAX_SAMPLE_CHARS)


class ReplyParseTests(unittest.TestCase):
    def test_dict_list_and_text_payloads(self):
        self.assertEqual(portrait.parse_reply({"评价": ["说话直接", "爱抬杠"]}), ["说话直接", "爱抬杠"])
        self.assertEqual(portrait.parse_reply(["说话直接"]), ["说话直接"])
        self.assertEqual(portrait.parse_reply({"评价": "说话直接\n爱抬杠"}), ["说话直接", "爱抬杠"])

    def test_markup_and_numbering_are_stripped(self):
        payload = {"评价": ["- **说话直接**", "2. 爱抬杠", "「句子很短」"]}
        self.assertEqual(portrait.parse_reply(payload), ["说话直接", "爱抬杠", "句子很短"])

    def test_appearance_judgements_are_dropped(self):
        payload = {"评价": ["他颜值很高", "说话直接", "身材不错"]}
        self.assertEqual(portrait.parse_reply(payload), ["说话直接"])

    def test_at_most_max_lines_and_no_duplicates(self):
        payload = {"评价": ["一", "一", "二", "三", "四", "五"]}
        self.assertEqual(portrait.parse_reply(payload), ["一", "二", "三", "四"])

    def test_overlong_lines_and_junk_are_dropped(self):
        payload = {"评价": ["好" * 200, 7, "", "说话直接"]}
        self.assertEqual(portrait.parse_reply(payload), ["说话直接"])

    def test_unknown_payload_shapes_are_empty(self):
        for payload in (None, {}, {"评价": []}, 5, {"其它": ["x"]}):
            self.assertEqual(portrait.parse_reply(payload), [], repr(payload))


class PromptTests(unittest.TestCase):
    def test_prompt_carries_persona_name_and_selection(self):
        state = PersonalityState(ROOM, name="如烟")
        system, user = portrait.build_prompt(state, None, "小红", ["第一句", "第二句"])
        self.assertIn("当前人格：如烟", system)
        self.assertIn("小红", user)
        self.assertIn("- 第一句", user)
        self.assertIn("JSON", user)

    def test_image_prompt_has_no_name_and_no_real_face(self):
        prompt = portrait.image_prompt(["说话直接", "爱抬杠"])
        self.assertIn("说话直接", prompt)
        self.assertIn("不追求与真人相似", prompt)
        for word in ("小红", "wxid", "@"):
            self.assertNotIn(word, prompt)

    def test_image_prompt_works_without_lines(self):
        self.assertIn("话不多", portrait.image_prompt([]))


class MemberMessagesTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.addCleanup(self.close)
        self.stores = []
        self.config = self.build_config()
        self.store = self.new_store()

    def build_config(self, **overrides):
        options = {"groups": {ROOM: []}, "history_enabled": True,
                   "database_file": "member_data.sqlite3", "retention_days": 30}
        options.update(overrides)
        path = Path(self.scratch.name) / "members.json"
        path.write_text(json.dumps(options, ensure_ascii=False), encoding="utf-8")
        return path

    def new_store(self, config=None):
        store = MemberStore(config or self.config, ACCOUNT)
        self.stores.append(store)
        store.initialize()
        return store

    def close(self):
        for store in self.stores:
            closer = getattr(store, "close", None)
            if callable(closer):
                closer()

    def say(self, text, sender=CAT, sequence=0, **data):
        message = Message(text, ROOM, sender_id=sender, is_group=True, session_id="s",
                          sequence=sequence, data=dict(data))
        self.store.record(message)
        return message

    def test_only_target_member_and_newest_first(self):
        self.say("我第一句有内容的话", sequence=1)
        self.say("他说的话不该出现", sender=DOG, sequence=2)
        self.say("我第二句有内容的话", sequence=3)
        rows = self.store.member_messages(ROOM, CAT, days=30, limit=60)
        self.assertEqual([row["text"] for row in rows], ["我第二句有内容的话", "我第一句有内容的话"])

    def test_bot_output_is_excluded(self):
        self.say("这句是机器人说的", bot_generated=True)
        self.say("这句是真人说的")
        rows = self.store.member_messages(ROOM, CAT, days=30, limit=60)
        self.assertEqual([row["text"] for row in rows], ["这句是真人说的"])

    def test_limit_and_unknown_member(self):
        for index in range(5):
            self.say(f"第{index}句有内容的话", sequence=index)
        self.assertEqual(len(self.store.member_messages(ROOM, CAT, days=30, limit=2)), 2)
        self.assertEqual(self.store.member_messages(ROOM, "wxid_nobody", days=30, limit=60), [])

    def test_bad_arguments_are_rejected(self):
        with self.assertRaises(MemberError):
            self.store.member_messages("999@chatroom", CAT)
        with self.assertRaises(MemberError):
            self.store.member_messages(ROOM, "")
        with self.assertRaises(MemberError):
            self.store.member_messages(ROOM, CAT, days=0)
        with self.assertRaises(MemberError):
            self.store.member_messages(ROOM, CAT, limit=0)

    def test_retention_days_must_be_sane(self):
        for value in (-1, 400, "30", True):
            with self.assertRaises(MemberError, msg=repr(value)):
                MemberStore(self.build_config(retention_days=value), ACCOUNT)


class FakeLogger:
    def __init__(self):
        self.lines = []

    def info(self, *args):
        self.lines.append(args)

    def warning(self, *args):
        self.lines.append(args)


class FakeMembers:
    account = ACCOUNT
    groups = {ROOM}

    def __init__(self, rows=None, name="小红"):
        self.rows = rows if rows is not None else [{"text": "我昨天去看了一个特别冷的展"}]
        self.name = name
        self.calls = []

    def display_name(self, uid, room):
        return self.name

    def member_messages(self, room, uid, *, days, limit):
        self.calls.append((room, uid, days, limit))
        return self.rows

    def conversation_snapshot(self, message):
        # 这些用例只关心画像分支是否被走到；普通对话路径用取消来快速收尾。
        raise SnapshotExpired("测试里不跑普通对话")


class FakeSpeaker:
    name = "小明"
    is_owner = False
    kind = "member"


class FakeClient:
    def __init__(self, payload):
        self.payload = payload

    def complete_json(self, system, user):
        return self.payload


class FakeCtx:
    def __init__(self, payload=None, rows=None):
        self.logger = FakeLogger()
        self.sent = []
        self.images = []
        self.members = FakeMembers(rows)
        self._payload = payload if payload is not None else {"评价": ["说话直接", "爱抬杠"]}

    def personality(self, message):
        return PersonalityState(ROOM, name="如烟")

    def plugin_is_active(self, name):
        return False

    def speaker(self, message):
        return FakeSpeaker()

    def llm_for(self, message, *, personality=None, speaker=None):
        return FakeClient(self._payload)

    def reply(self, message, text):
        self.sent.append(text)
        return True

    def reply_image(self, message, path):
        self.images.append(Path(path))
        return True


def at_message(text, *, at=(ACCOUNT, CAT)):
    return Message("@机器人\u2005" + text, ROOM, sender_id=CAT, is_group=True,
                   session_id="s", data={"is_at_self": True, "at_members": list(at)})


class PluginTests(unittest.TestCase):
    def plugin(self, ctx, *, enabled=True):
        plugin = ChatPlugin()
        plugin.members = ctx.members
        plugin.portrait_enabled = enabled
        plugin.portrait_days = 30
        plugin.portrait_limit = 60
        plugin.image_config_file = "image_config.json"
        plugin.project_root = Path(tempfile.gettempdir())
        plugin.portrait_directory = plugin.project_root
        plugin.portraits = {}
        plugin.provider = None
        plugin.portrait_pool = _InlinePool()
        plugin.sleep = lambda seconds: None
        plugin.rng = None
        return plugin

    def test_portrait_reply_has_body_and_disclaimer(self):
        ctx = FakeCtx()
        self.plugin(ctx).on_message(at_message("你觉得 是个什么样的人"), ctx)
        self.assertEqual(len(ctx.sent), 1)
        self.assertIn("小红 给我的感觉是——", ctx.sent[0])
        self.assertIn("说话直接", ctx.sent[0])
        self.assertIn(portrait.DISCLAIMER, ctx.sent[0])
        self.assertEqual(ctx.images, [])

    def test_image_is_queued_only_when_asked(self):
        ctx = FakeCtx()
        self.plugin(ctx).on_message(at_message("画一张 的画像"), ctx)
        self.assertEqual(len(ctx.sent), 1)
        self.assertEqual(ctx.images, [])  # 没有生图配置：只回文字，不报错

    def test_member_without_history_gets_a_soft_reply(self):
        ctx = FakeCtx(rows=[])
        self.plugin(ctx).on_message(at_message("你觉得 是个什么样的人"), ctx)
        self.assertEqual(len(ctx.sent), 1)
        self.assertIn("还没听到", ctx.sent[0])

    def test_empty_evaluation_falls_back(self):
        ctx = FakeCtx(payload={"评价": ["他颜值很高"]})
        self.plugin(ctx).on_message(at_message("你觉得 是个什么样的人"), ctx)
        self.assertIn("还想不好怎么说", ctx.sent[0])

    def test_disabled_portrait_falls_back_to_chat(self):
        ctx = FakeCtx()
        self.plugin(ctx, enabled=False).on_message(at_message("你觉得 是个什么样的人"), ctx)
        self.assertEqual(ctx.sent, [])
        self.assertEqual(ctx.members.calls, [])

    def test_at_only_robot_does_not_trigger(self):
        ctx = FakeCtx()
        self.plugin(ctx).on_message(at_message("你觉得 是个什么样的人", at=(ACCOUNT,)), ctx)
        self.assertEqual(ctx.members.calls, [])

    def test_daily_image_cap(self):
        ctx = FakeCtx()
        plugin = self.plugin(ctx)
        plugin.portraits[ROOM] = (_today(), portrait.IMAGE_PER_DAY)
        plugin._queue_portrait(at_message("画像"), ctx, ["说话直接"])
        self.assertIn("今天已经画过", ctx.sent[0])


class _InlinePool:
    """测试里直接跑，不另开线程；提交失败的行为由 shutdown 后自然表现。"""

    def __init__(self):
        self.closed = False

    def submit(self, fn, *args):
        if self.closed:
            raise RuntimeError("closed")
        fn(*args)

    def shutdown(self, *, wait=False, cancel_futures=False):
        self.closed = True


def _today():
    import time
    return time.strftime("%Y-%m-%d")


if __name__ == "__main__":
    unittest.main()
