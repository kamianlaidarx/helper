"""playhub 路由与关键词层单测（`docs/design/playhub.md:580-594` 的等价类表 + 批次约束）。"""
import unittest

from plugins.playhub.keywords import (
    ALIAS_COUNT,
    ALIASES,
    KEYWORD_COUNT,
    PRIMARY_COUNT,
    TABLE,
    TRIGGERS,
    KeywordConflict,
    Trigger,
    build_table,
)
from plugins.playhub.registry import (
    ACTION_PLAN,
    BINDINGS,
    CURRENT_BATCH,
    BindingError,
    check_bindings,
    dispatch,
)
from plugins.playhub.router import Command, route
from plugins.playhub.textkit import Mention, normalize, normalize_with_mentions


class NormalizeCase(unittest.TestCase):
    """§2.6.1 归一化：NFKC、零宽、NBSP、全角空格。"""

    def test_zero_width_removed(self):
        self.assertEqual(normalize("签\u200b到"), "签到")
        self.assertEqual(normalize("签\u200c到"), "签到")
        self.assertEqual(normalize("签\u200d到"), "签到")
        self.assertEqual(normalize("\ufeff签到"), "签到")
        self.assertIsNotNone(route("签\u200b到"))

    def test_nbsp_is_plain_space(self):
        # NBSP 折成普通空格：中间 NBSP 等于分词，不是隐形字符 -> 不命中。
        self.assertEqual(normalize("签\u00a0到"), "签 到")
        self.assertIsNone(route("签\u00a0到"))
        # 尾部 NBSP 被 strip 掉 -> 命中。
        self.assertEqual(normalize("签到\u00a0"), "签到")
        self.assertIsNotNone(route("签到\u00a0"))

    def test_ideographic_space_is_plain_space(self):
        self.assertEqual(normalize("签\u3000到"), "签 到")
        self.assertIsNone(route("签\u3000到"))
        self.assertEqual(normalize("签到\u3000"), "签到")
        self.assertIsNotNone(route("签到\u3000"))

    def test_fullwidth_hash_folds_to_ascii(self):
        self.assertEqual(normalize("宝宝＃改名 小明"), "宝宝#改名 小明")
        cmd = route("宝宝＃改名 小明")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd.args, "改名 小明")
        self.assertEqual(cmd.separator, "#")

    def test_normalize_is_idempotent(self):
        for t in TRIGGERS:
            self.assertEqual(normalize(t.keyword), t.keyword)


class EquivalentClassCase(unittest.TestCase):
    """§2.6.2 等价类表：9 行逐行断言。"""

    def test_space_separator(self):
        cmd = route("宝宝 改名 小明")
        self.assertIsNotNone(cmd)
        self.assertEqual((cmd.name, cmd.args, cmd.separator), ("宝宝", "改名 小明", "space"))

    def test_hash_equals_space(self):
        cmd = route("宝宝#改名 小明")
        self.assertIsNotNone(cmd)
        self.assertEqual((cmd.args, cmd.separator), ("改名 小明", "#"))

    def test_fullwidth_hash_equals_hash(self):
        cmd = route("宝宝＃改名 小明")
        self.assertIsNotNone(cmd)
        self.assertEqual((cmd.args, cmd.separator), ("改名 小明", "#"))

    def test_hash_split_only_once(self):
        cmd = route("宝宝#改名#姓名")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd.args, "改名#姓名")
        self.assertEqual(cmd.tokens, ("改名#姓名",))

    def test_hash_in_args_kept(self):
        cmd = route("宝宝 改名 A#B")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd.args, "改名 A#B")

    def test_fullwidth_hash_in_args_folded_but_not_split(self):
        cmd = route("宝宝#改名 A＃B")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd.args, "改名 A#B")

    def test_mention_goes_into_args_with_hash(self):
        cmd = route("结婚@昵称#甲", [Mention(2, 5, "wxid_target")])
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd.args, "@昵称#甲")
        self.assertEqual(cmd.separator, "@")
        self.assertEqual(len(cmd.mentions), 1)

    def test_leading_mention_does_not_trigger(self):
        self.assertIsNone(route("@昵称#甲 宝宝 看", [Mention(0, 3, "wxid_target")]))

    def test_no_prefix_match(self):
        self.assertIsNone(route("宝宝看"))


class KeywordCase(unittest.TestCase):
    """60 词表：全命中、长度、四字排除、唯一性、词账。"""

    def test_all_60_keywords_hit(self):
        self.assertEqual(len(TRIGGERS), KEYWORD_COUNT)
        for t in TRIGGERS:
            cmd = route(t.keyword)
            self.assertIsNotNone(cmd, f"{t.keyword} 未命中")
            self.assertEqual(cmd.action, t.action)
            self.assertTrue(cmd.action, f"{t.keyword} 的 action 为空")

    def test_all_keywords_at_most_three_chars(self):
        for t in TRIGGERS:
            self.assertLessEqual(len(t.keyword), 3, t.keyword)

    def test_four_chinese_keyword_not_registered(self):
        self.assertNotIn("成语接龙", TABLE.head_index)
        self.assertNotIn("合并接龙", TABLE.head_index)
        self.assertNotIn("退出接龙", TABLE.head_index)
        self.assertNotIn("看图猜成语", TABLE.head_index)

    def test_framework_words_not_registered(self):
        for word in ("提示", "换一题", "公布答案", "退出猜成语"):
            self.assertNotIn(word, TABLE.head_index)

    def test_word_account(self):
        primary = [t for t in TRIGGERS if t.keyword not in ALIASES]
        alias = [t for t in TRIGGERS if t.keyword in ALIASES]
        self.assertEqual(len(primary), PRIMARY_COUNT)
        self.assertEqual(len(alias), ALIAS_COUNT)
        self.assertEqual(len(TABLE.head_index), KEYWORD_COUNT)

    def test_daka_equals_qiandao(self):
        self.assertEqual(route("打卡").action, "daily.signin")
        self.assertEqual(TABLE.trigger("打卡").action, TABLE.trigger("签到").action)

    def test_conflict_reports_both_features(self):
        bad = tuple(
            Trigger("签到", "daily", "daily.fortune") if t.keyword == "打卡" else t
            for t in TRIGGERS
        )
        with self.assertRaises(KeywordConflict) as ctx:
            build_table(bad)
        message = str(ctx.exception)
        self.assertIn("daily.signin", message)
        self.assertIn("daily.fortune", message)

    def test_duplicate_same_feature_also_rejected(self):
        bad = tuple(
            Trigger("签到", "daily", "daily.signin") if t.keyword == "打卡" else t
            for t in TRIGGERS
        )
        with self.assertRaises(KeywordConflict):
            build_table(bad)

    def test_four_char_word_rejected(self):
        bad = TRIGGERS + (Trigger("成语接龙", "quiz", "quiz.chengyu"),)
        with self.assertRaises(KeywordConflict):
            build_table(bad)

    def test_over_length_word_rejected(self):
        bad = TRIGGERS + (Trigger("签到一下", "daily", "daily.signin"),)
        with self.assertRaises(KeywordConflict):
            build_table(bad)


class RouteSemanticsCase(unittest.TestCase):
    """包含匹配、艾特、契约字段。"""

    def test_substring_does_not_match(self):
        self.assertIsNone(route("今天签到了吗"))
        self.assertIsNone(route("帮我签到一下"))

    def test_command_contract_fields_and_order(self):
        self.assertEqual(
            tuple(Command.__dataclass_fields__),
            ("name", "action", "raw", "args", "tokens", "had_arg", "separator", "mentions"),
        )
        cmd = route("宝宝 看")
        self.assertEqual(cmd.name, "宝宝")
        self.assertEqual(cmd.raw, "宝宝 看")
        self.assertEqual(cmd.args, "看")
        self.assertEqual(cmd.tokens, ("看",))
        self.assertTrue(cmd.had_arg)
        self.assertEqual(cmd.separator, "space")
        self.assertEqual(cmd.mentions, ())

    def test_bare_command_has_no_separator(self):
        cmd = route("签到")
        self.assertEqual((cmd.args, cmd.had_arg, cmd.separator), ("", False, None))

    def test_leading_mention_without_span_still_rejected(self):
        self.assertIsNone(route("@机器人 签到"))

    def test_mention_at_tail_has_one_span(self):
        cmd = route("签到@某人", [Mention(2, 5, "wxid_someone")])
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd.name, "签到")
        self.assertEqual(cmd.args, "@某人")
        self.assertEqual(len(cmd.mentions), 1)
        self.assertEqual(cmd.mentions[0].uid, "wxid_someone")

    def test_mention_span_is_remapped_after_nfkc(self):
        # 全角字符折半会改变长度，span 必须跟着重映射。
        text = "结婚＠昵称 同意"
        s, spans = normalize_with_mentions(text, [Mention(2, 5, "wxid_a")])
        self.assertEqual(s, "结婚@昵称 同意")
        self.assertEqual((spans[0].start, spans[0].end), (2, 5))

    def test_hash_in_param_kept_for_song(self):
        cmd = route("点歌#青花瓷#live")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd.args, "青花瓷#live")

    def test_punctuation_not_stripped(self):
        # §2.6.1 取舍 4：签到！/签到。不触发。
        self.assertIsNone(route("签到！"))
        self.assertIsNone(route("签到。"))

    def test_too_long_text_rejected(self):
        self.assertIsNone(route("签到" + "啊" * 80))


class FakeEngine:
    name = "daily"

    def __init__(self):
        self.calls = []

    def on_command(self, cmd, msg, ctx, game):
        self.calls.append((cmd, msg, ctx, game))
        return "ok"


class BindingCase(unittest.TestCase):
    """绑定表按批次增长：已绑定 == 计划批次 ≤ CURRENT_BATCH 的动作；未绑定动作静默。"""

    def _expected_bound(self):
        return {a for a, b in ACTION_PLAN.items() if b is not None and b <= CURRENT_BATCH}

    def test_bound_actions_match_batches(self):
        self.assertEqual(set(BINDINGS), self._expected_bound())
        for action, binding in BINDINGS.items():
            self.assertEqual(binding.batch, ACTION_PLAN[action], action)

    def test_action_plan_covers_every_keyword_action(self):
        self.assertEqual(set(ACTION_PLAN), {t.action for t in TRIGGERS})
        self.assertEqual(ACTION_PLAN["daily.signin"], 1)
        self.assertEqual(ACTION_PLAN["social.marry"], 3)
        self.assertEqual(ACTION_PLAN["groupstat.dive"], 4)

    def test_check_bindings_passes(self):
        self.assertEqual(check_bindings(), len(BINDINGS))

    def test_missing_current_batch_action_raises(self):
        # 只要求"当前批次"的动作必须绑定：删掉任一 CURRENT_BATCH 的动作就报错。
        victim = next(a for a, b in ACTION_PLAN.items() if b == CURRENT_BATCH)
        partial = {k: v for k, v in BINDINGS.items() if k != victim}
        with self.assertRaises(BindingError) as ctx:
            check_bindings(bindings=partial)
        self.assertIn(victim, str(ctx.exception))

    def test_strict_mode_rejects_every_unbound(self):
        # 第 4 批之后全量绑定，严格模式只在真缺绑定时报错，所以注入缺一条的现场。
        partial = {k: v for k, v in BINDINGS.items() if k != "groupstat.dive"}
        with self.assertRaises(BindingError) as ctx:
            check_bindings(bindings=partial, allow_unbound=False)
        self.assertIn("groupstat.dive", str(ctx.exception))

    def test_unbound_action_is_silent(self):
        engine = FakeEngine()
        cmd = route("点歌 晴天")
        self.assertIsNone(dispatch(engine, "music.request", cmd))
        self.assertEqual(engine.calls, [])

    def test_bound_action_dispatches(self):
        engine = FakeEngine()
        cmd = route("签到")
        self.assertEqual(dispatch(engine, cmd.action, cmd), "ok")
        self.assertEqual(len(engine.calls), 1)

    def test_engine_mismatch_is_silent(self):
        engine = FakeEngine()

        class Other(FakeEngine):
            name = "ranking"

        self.assertIsNone(dispatch(Other(), "daily.signin", route("签到")))

    def test_string_engine_is_silent(self):
        self.assertIsNone(dispatch("daily", "daily.signin", route("签到")))


if __name__ == "__main__":
    unittest.main()
