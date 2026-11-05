"""`social` 引擎婚姻段单测：开关、求婚、成婚、一人一婚、每日 3 次、度蜜月 520、
离婚 200 + 24h 确认 + 超时全额退费、清求婚次数 15 钻、审计（`docs/design/playhub.md:1359-1362`）。

不联网、不 sleep、不写仓库外路径：数据库落在 `TemporaryDirectory` 里。
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime

from plugins.playhub.app import App
from plugins.playhub.core import Store, balance, credit
from plugins.playhub.core.clock import CN
from plugins.playhub.core import items, switches
from plugins.playhub.core.errors import WalletUninitialized

ROOM = "12345678@chatroom"
UID_A = "wxid_a"
UID_B = "wxid_b"
UID_C = "wxid_c"
NAME_A = "小明"
NAME_B = "小红"
NAME_C = "小刚"
UID_D = "wxid_d"
UID_E = "wxid_e"
NAME_D = "小丽"
NAME_E = "小美"


class FakeMsg:
    """最小 Message 替身：只带 App/引擎真正读的字段。"""

    def __init__(self, text="", *, chat_id=ROOM, sender_id=UID_A, nickname=NAME_A, is_self=False):
        self.text = text
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.is_self = is_self
        self.data = {"sender_wechat_name": nickname}


class FakeMember:
    def __init__(self, wxid: str) -> None:
        self.wxid = wxid


class FakeMembers:
    """成员表替身：`resolve(昵称)` → wxid；未登记的人抛错（引擎应转成「没认出」）。"""

    def __init__(self, names: dict[str, str]) -> None:
        self._names = dict(names)

    def resolve(self, name, room=None):
        if name not in self._names:
            raise KeyError(name)
        return FakeMember(self._names[name])


class FakeCtx:
    def __init__(self, names: dict[str, str]) -> None:
        self.members = FakeMembers(names)


class FakeClock:
    """秒级可控时钟（北京时区 2026-11-01 12:00 起）。"""

    def __init__(self) -> None:
        self.ts = int(datetime.fromisoformat("2026-11-01T12:00:00").replace(tzinfo=CN).timestamp())

    def __call__(self) -> int:
        return self.ts

    def advance(self, seconds: int = 1) -> None:
        self.ts += seconds


class SocialTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = __import__("pathlib").Path(self._tmp.name)
        self.store = Store(self.tmp / "playhub.sqlite3", account="acct")
        self.store.migrate()
        self.clock = FakeClock()
        self.app = App(store=self.store, now_fn=self.clock, repo_root=self.tmp)
        self.ctx = FakeCtx({NAME_A: UID_A, NAME_B: UID_B, NAME_C: UID_C, NAME_D: UID_D, NAME_E: UID_E})

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    # -- 辅助 ---------------------------------------------------------------
    def say(self, text: str, *, uid: str = UID_A, nickname: str = NAME_A):
        msg = FakeMsg(text, sender_id=uid, nickname=nickname)
        return self.app.handle_message(self.ctx, msg)

    def enable(self, name: str = switches.MARRIAGE, value: str = "1") -> None:
        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO kv (room, key, value, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(room, key) DO UPDATE SET value = excluded.value",
                (ROOM, switches.kv_key(name), value, self.clock()),
            )

    def give(self, uid: str, amount: int, *, currency: str = "coin") -> None:
        with self.store.tx() as conn:
            from plugins.playhub.core.wallet import ensure_player

            ensure_player(conn, room=ROOM, uid=uid, nickname="", now=self.clock())
            credit(conn, room=ROOM, uid=uid, currency=currency, amount=amount,
                   reason="test_seed", operation_key=f"seed:{uid}:{currency}:{amount}",
                   posting_no=0, now=self.clock())

    def coins(self, uid: str = UID_A) -> int:
        with self.store.tx(immediate=False) as conn:
            try:
                return balance(conn, room=ROOM, uid=uid, currency="coin")
            except WalletUninitialized:
                return 0

    def diamonds(self, uid: str = UID_A) -> int:
        with self.store.tx(immediate=False) as conn:
            try:
                return balance(conn, room=ROOM, uid=uid, currency="diamond")
            except WalletUninitialized:
                return 0

    def pairs(self, kind: str = "marriage"):
        with self.store.tx(immediate=False) as conn:
            return conn.execute(
                "SELECT id, low_uid, high_uid, state, since, ended_at, by_uid, meta FROM pair "
                "WHERE room = ? AND kind = ? ORDER BY id",
                (ROOM, kind),
            ).fetchall()

    def audits(self) -> int:
        with self.store.tx(immediate=False) as conn:
            return conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]


class SwitchTests(SocialTestBase):
    def test_default_off_is_silent(self) -> None:
        self.assertIsNone(self.say("结婚 @小红"))

    def test_explicit_off_values_stay_off(self) -> None:
        for value in ("0", "off", "false", "no", "disabled"):
            self.enable(value=value)
            self.assertIsNone(self.say("结婚 @小红"), value)

    def test_enabled_runs(self) -> None:
        self.enable()
        reply = self.say("结婚 @小红")
        self.assertIsNotNone(reply)
        self.assertIn("求婚已发出", reply.text)

    def test_engine_level_switch_still_applies(self) -> None:
        """`feature.social` 是引擎级总闸，与 5 个高危开关是两层，互不替代。"""
        self.enable()
        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO kv (room, key, value, updated_at) VALUES (?, 'feature.social', 'off', ?)",
                (ROOM, self.clock()),
            )
        self.assertIsNone(self.say("结婚 @小红"))


class ProposeTests(SocialTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.enable()

    def test_propose_creates_pending(self) -> None:
        self.say("结婚 @小红")
        rows = self.pairs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "pending")
        self.assertEqual(rows[0]["by_uid"], UID_A)
        self.assertEqual((rows[0]["low_uid"], rows[0]["high_uid"]), (UID_A, UID_B))

    def test_unknown_name_is_told_not_guessed(self) -> None:
        reply = self.say("结婚 @查无此人")
        self.assertIn("没认出", reply.text)
        self.assertEqual(len(self.pairs()), 0)

    def test_self_target_refused(self) -> None:
        reply = self.say("结婚 @小明")
        self.assertIn("不能对自己", reply.text)

    def test_bare_mention_without_name_is_usage(self) -> None:
        reply = self.say("结婚 同意")     # 子命令；这里验证非法参数走 USAGE
        self.assertIn("没有待你确认", reply.text)

    def test_duplicate_pending_refused(self) -> None:
        self.say("结婚 @小红")
        reply = self.say("结婚 @小红")
        self.assertIn("待确认的求婚", reply.text)

    def test_daily_cap_three(self) -> None:
        # 每日 3 次按「发起」计，与对方是否重复无关；这里用 3 个不同对象证明确实是第 4 次被拦。
        for name in (NAME_B, NAME_C, NAME_D):
            reply = self.say(f"结婚 @{name}")
            self.assertIn("求婚已发出", reply.text, name)
        reply = self.say(f"结婚 @{NAME_E}")
        self.assertIn("次数用完了", reply.text)
        self.assertEqual(len(self.pairs()), 3)


class AcceptTests(SocialTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.enable()

    def test_accept_activates(self) -> None:
        self.say("结婚 @小红")
        reply = self.say("结婚 同意", uid=UID_B, nickname=NAME_B)
        self.assertIn("已成婚", reply.text)
        row = self.pairs()[0]
        self.assertEqual(row["state"], "active")
        self.assertEqual(row["since"], self.clock())

    def test_reject_ends(self) -> None:
        self.say("结婚 @小红")
        reply = self.say("结婚 拒绝", uid=UID_B, nickname=NAME_B)
        self.assertIn("已拒绝", reply.text)
        self.assertEqual(self.pairs()[0]["state"], "ended")

    def test_accept_without_pending(self) -> None:
        reply = self.say("结婚 同意", uid=UID_B, nickname=NAME_B)
        self.assertIn("没有待你确认", reply.text)

    def test_proposer_cannot_accept_own_request(self) -> None:
        self.say("结婚 @小红")
        reply = self.say("结婚 同意")
        self.assertIn("没有待你确认", reply.text)

    def test_pending_expires_after_24h(self) -> None:
        self.say("结婚 @小红")
        self.clock.advance(24 * 3600 + 1)
        reply = self.say("结婚 同意", uid=UID_B, nickname=NAME_B)
        self.assertIn("没有待你确认", reply.text)
        self.assertEqual(self.pairs()[0]["state"], "ended")

    def test_one_marriage_per_person(self) -> None:
        self.say("结婚 @小红")
        self.say("结婚 同意", uid=UID_B, nickname=NAME_B)
        reply = self.say("结婚 @小刚")
        self.assertIn("已经有婚姻", reply.text)

    def test_married_target_refused(self) -> None:
        self.say("结婚 @小红")
        self.say("结婚 同意", uid=UID_B, nickname=NAME_B)
        reply = self.say("结婚 @小红", uid=UID_C, nickname=NAME_C)
        self.assertIn("对方已经有婚姻", reply.text)


class HoneymoonTests(SocialTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.enable()
        self.say("结婚 @小红")
        self.say("结婚 同意", uid=UID_B, nickname=NAME_B)

    def test_costs_520(self) -> None:
        self.give(UID_A, 1000)
        reply = self.say("结婚 度蜜月")
        self.assertIn("💰 金币：-520", reply.text)
        self.assertEqual(self.coins(), 480)

    def test_insufficient(self) -> None:
        self.give(UID_A, 100)
        reply = self.say("结婚 度蜜月")
        self.assertIn("金币不够", reply.text)
        self.assertEqual(self.coins(), 100)

    def test_cooldown_24h(self) -> None:
        self.give(UID_A, 2000)
        self.say("结婚 度蜜月")
        reply = self.say("结婚 度蜜月")
        self.assertIn("还要等", reply.text)
        self.assertEqual(self.coins(), 1480)
        self.clock.advance(24 * 3600 + 1)
        reply = self.say("结婚 度蜜月")
        self.assertIn("💰 金币：-520", reply.text)

    def test_mine_shows_days_and_count(self) -> None:
        self.give(UID_A, 600)
        self.say("结婚 度蜜月")
        self.clock.advance(3 * 86400)
        reply = self.say("结婚 我的婚姻")
        self.assertIn("📅 婚龄：3 天", reply.text)
        self.assertIn("🌴 蜜月：1 次", reply.text)


class DivorceTests(SocialTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.enable()
        self.say("结婚 @小红")
        self.say("结婚 同意", uid=UID_B, nickname=NAME_B)

    def test_apply_costs_200_and_waits(self) -> None:
        self.give(UID_A, 500)
        reply = self.say("结婚 申请离婚")
        self.assertIn("💰 已付手续费：200 金币", reply.text)
        self.assertEqual(self.coins(), 300)
        self.assertEqual(self.pairs()[0]["state"], "active")   # 还没离

    def test_insufficient(self) -> None:
        self.give(UID_A, 100)
        reply = self.say("结婚 申请离婚")
        self.assertIn("金币不够", reply.text)

    def test_partner_accepts(self) -> None:
        self.give(UID_A, 500)
        self.say("结婚 申请离婚")
        reply = self.say("结婚 同意离婚", uid=UID_B, nickname=NAME_B)
        self.assertIn("已解除婚姻", reply.text)
        self.assertEqual(self.pairs()[0]["state"], "ended")

    def test_initiator_cannot_confirm(self) -> None:
        self.give(UID_A, 500)
        self.say("结婚 申请离婚")
        reply = self.say("结婚 同意离婚")
        self.assertIn("得由对方确认", reply.text)

    def test_refuse_refunds(self) -> None:
        self.give(UID_A, 500)
        self.say("结婚 申请离婚")
        reply = self.say("结婚 拒绝离婚", uid=UID_B, nickname=NAME_B)
        self.assertIn("已退回", reply.text)
        self.assertEqual(self.coins(), 500)
        self.assertEqual(self.pairs()[0]["state"], "active")

    def test_timeout_refunds_and_ends(self) -> None:
        self.give(UID_A, 500)
        self.say("结婚 申请离婚")
        self.assertEqual(self.coins(), 300)
        self.clock.advance(24 * 3600 + 1)
        self.say("结婚 我的婚姻")             # 任意婚姻子命令都会先收超时
        self.assertEqual(self.pairs()[0]["state"], "ended")
        self.assertEqual(self.coins(), 500)

    def test_no_single_side_divorce(self) -> None:
        reply = self.say("结婚 同意离婚", uid=UID_B, nickname=NAME_B)
        self.assertIn("没有待确认的离婚", reply.text)


class ResetAndStatsTests(SocialTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.enable()

    def test_reset_refuses_when_unused(self) -> None:
        reply = self.say("结婚 清求婚次数")
        self.assertIn("当前无需清空", reply.text)

    def test_reset_costs_15_diamonds(self) -> None:
        self.say("结婚 @小红")
        self.give(UID_A, 20, currency="diamond")
        reply = self.say("结婚 清求婚次数")
        self.assertIn("💎 钻石：-15", reply.text)
        self.assertEqual(self.diamonds(), 5)
        # 清零后可再求一次
        reply = self.say("结婚 @小刚")
        self.assertIn("求婚已发出", reply.text)

    def test_reset_without_diamonds(self) -> None:
        self.say("结婚 @小红")
        reply = self.say("结婚 清求婚次数")
        self.assertIn("钻石不够", reply.text)

    def test_stats_reports_numbers(self) -> None:
        self.say("结婚 @小红")
        reply = self.say("结婚 婚姻统计")
        self.assertIn("💍 求婚发起：1 次", reply.text)
        self.assertIn("💔 离婚：0 次", reply.text)


class SisterTests(SocialTestBase):
    """闺蜜：`@对象` / `同意` / `拒绝` / `解除 @对象`；一个人可以有多条。"""

    def setUp(self) -> None:
        super().setUp()
        self.enable(switches.KINSHIP)

    def test_propose_and_accept(self) -> None:
        self.assertIn("闺蜜邀请已发出", self.say("闺蜜 @小红").text)
        reply = self.say("闺蜜 同意", uid=UID_B, nickname=NAME_B)
        self.assertIn("已结成闺蜜关系", reply.text)
        row = self.pairs("sister")[0]
        self.assertEqual((row["state"], row["by_uid"]), ("active", UID_A))

    def test_silent_when_switch_off(self) -> None:
        with self.store.tx() as conn:
            conn.execute("DELETE FROM kv WHERE room = ? AND key = 'feature.kinship'", (ROOM,))
        self.assertIsNone(self.say("闺蜜 @小红"))

    def test_multiple_sisters_allowed(self) -> None:
        for name, uid in ((NAME_B, UID_B), (NAME_C, UID_C)):
            self.say(f"闺蜜 @{name}")
            self.say("闺蜜 同意", uid=uid, nickname=name)
        self.assertEqual(len([r for r in self.pairs("sister") if r["state"] == "active"]), 2)

    def test_reject_and_dissolve(self) -> None:
        self.say("闺蜜 @小红")
        self.assertIn("已拒绝", self.say("闺蜜 拒绝", uid=UID_B, nickname=NAME_B).text)
        self.say("闺蜜 @小红")
        self.say("闺蜜 同意", uid=UID_B, nickname=NAME_B)
        reply = self.say("闺蜜 解除 @小红")
        self.assertIn("闺蜜关系已解除", reply.text)
        self.assertEqual(self.pairs("sister")[-1]["state"], "ended")

    def test_dissolve_without_relation(self) -> None:
        self.assertIn("没有闺蜜关系", self.say("闺蜜 解除 @小红").text)

    def test_proposer_can_withdraw_pending(self) -> None:
        self.say("闺蜜 @小红")
        reply = self.say("闺蜜 解除 @小红")
        self.assertIn("已撤回", reply.text)


class BrotherTests(SocialTestBase):
    """结拜（原「兄弟」词已不注册）：与闺蜜同一套流程，走另一个开关。"""

    def setUp(self) -> None:
        super().setUp()
        self.enable(switches.KINSHIP)

    def test_propose_and_accept(self) -> None:
        self.assertIn("结拜邀请已发出", self.say("结拜 @小刚").text)
        reply = self.say("结拜 同意", uid=UID_C, nickname=NAME_C)
        self.assertIn("已结成结拜关系", reply.text)
        self.assertEqual(self.pairs("brother")[0]["state"], "active")

    def test_dissolve(self) -> None:
        self.say("结拜 @小刚")
        self.say("结拜 同意", uid=UID_C, nickname=NAME_C)
        self.assertIn("结拜关系已解除", self.say("结拜 解除 @小刚").text)


class EmployTests(SocialTestBase):
    """雇佣：`@对象` / `工资` / `解除 @对象`；工资本批只展示，不发放。"""

    def setUp(self) -> None:
        super().setUp()
        self.enable(switches.EMPLOY)

    def test_hire_and_wage(self) -> None:
        self.say("雇佣 @小红")
        self.say("雇佣 同意", uid=UID_B, nickname=NAME_B)
        row = self.pairs("employ")[0]
        self.assertEqual(row["state"], "active")
        self.assertIn('"wage": 20', row["meta"])
        reply = self.say("雇佣 工资")
        self.assertIn("每日 20 金币", reply.text)
        self.assertIn(NAME_B, reply.text)

    def test_employee_sees_employer(self) -> None:
        self.say("雇佣 @小红")
        self.say("雇佣 同意", uid=UID_B, nickname=NAME_B)
        reply = self.say("雇佣 工资", uid=UID_B, nickname=NAME_B)
        self.assertIn("被", reply.text)
        self.assertIn(NAME_A, reply.text)

    def test_wage_without_job(self) -> None:
        self.assertIn("还没有雇佣关系", self.say("雇佣 工资").text)

    def test_dissolve(self) -> None:
        self.say("雇佣 @小红")
        self.say("雇佣 同意", uid=UID_B, nickname=NAME_B)
        self.assertIn("雇佣关系已解除", self.say("雇佣 解除 @小红").text)


class ConcubineTests(SocialTestBase):
    """纳妾：默认永久关闭；开启后仍要求「已有婚姻」+ 30 钻石余额（门槛，不扣款）。"""

    def setUp(self) -> None:
        super().setUp()

    def test_off_by_default(self) -> None:
        self.assertIsNone(self.say("纳妾 @小红"))

    def test_requires_marriage(self) -> None:
        self.enable(switches.CONCUBINE)
        self.give(UID_A, 40, currency="diamond")
        reply = self.say("纳妾 @小红")
        self.assertIn("得先有婚姻", reply.text)

    def test_requires_thirty_diamonds(self) -> None:
        self.enable(switches.CONCUBINE)
        self.enable(switches.MARRIAGE)
        self.say("结婚 @小红")
        self.say("结婚 同意", uid=UID_B, nickname=NAME_B)
        self.give(UID_A, 20, currency="diamond")
        reply = self.say("纳妾 @小刚")
        self.assertIn("钻石余额 ≥ 30", reply.text)
        self.assertEqual(self.pairs("concubine"), [])

    def test_threshold_is_not_a_charge(self) -> None:
        self.enable(switches.CONCUBINE)
        self.enable(switches.MARRIAGE)
        self.say("结婚 @小红")
        self.say("结婚 同意", uid=UID_B, nickname=NAME_B)
        self.give(UID_A, 30, currency="diamond")
        reply = self.say("纳妾 @小刚")
        self.assertIn("纳妾邀请已发出", reply.text)
        self.assertEqual(self.diamonds(), 30)       # 门槛不扣款
        self.say("纳妾 同意", uid=UID_C, nickname=NAME_C)
        self.assertEqual(self.pairs("concubine")[0]["state"], "active")
        self.assertEqual(self.diamonds(), 30)


class AuditTests(SocialTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.enable()

    def test_relationship_ops_are_audited(self) -> None:
        self.say("结婚 @小红")
        self.say("结婚 同意", uid=UID_B, nickname=NAME_B)
        self.assertGreaterEqual(self.audits(), 2)
        with self.store.tx(immediate=False) as conn:
            row = conn.execute(
                "SELECT action, actor, target, detail FROM audit ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(row["action"], "cmd:结婚")
        self.assertEqual(row["actor"], UID_B)
        self.assertEqual(row["target"], UID_A)
        self.assertIn("accept", row["detail"])


if __name__ == "__main__":
    unittest.main()

# ---------------------------------------------------------------------------
# 关系卡 / 配对 / 礼物 / 宝宝（第 3 批社交段剩余四项）
# ---------------------------------------------------------------------------
class CardTests(SocialTestBase):
    def test_self_card_when_unmarried(self):
        reply = self.say("关系卡")
        self.assertIn("@小明", reply.text)
        self.assertIn("🪪 关系卡", reply.text)
        self.assertIn("💤 未婚", reply.text)
        self.assertIn("这张卡几乎是空的", reply.text)

    def test_card_shows_marriage_days(self):
        self.enable(switches.MARRIAGE)
        self.say("结婚 @小红")
        self.say("结婚 同意", uid=UID_B, nickname=NAME_B)
        self.clock.advance(3 * 86400)
        reply = self.say("关系卡")
        self.assertIn("💍 婚姻：小红 · 3 天", reply.text)

    def test_card_of_someone_else(self):
        self.enable(switches.MARRIAGE)
        self.say("结婚 @小红", uid=UID_A, nickname=NAME_A)
        self.say("结婚 同意", uid=UID_B, nickname=NAME_B)
        reply = self.say("关系卡 @小红")
        self.assertIn("@小红", reply.text)
        self.assertIn("💍 婚姻：小明", reply.text)

    def test_card_unknown_target(self):
        reply = self.say("关系卡 @查无此人")
        self.assertIn("没认出", reply.text)

    def test_card_lists_kinship(self):
        self.enable(switches.KINSHIP)
        self.say("闺蜜 @小红")
        self.say("闺蜜 同意", uid=UID_B, nickname=NAME_B)
        self.say("结拜 @小刚")
        self.say("结拜 同意", uid=UID_C, nickname=NAME_C)
        reply = self.say("关系卡")
        self.assertIn("👭 闺蜜：小红", reply.text)
        self.assertIn("🤝 结拜：小刚", reply.text)


class MatchTests(SocialTestBase):
    def test_needs_target(self):
        # 裸发关键词走触发卡（`needs_arg=True`），不进引擎
        reply = self.say("配对")
        self.assertIn("=====💞配对💞=====", reply.text)
        self.assertIn("配对 @对象", reply.text)

    def test_deterministic_same_day(self):
        first = self.say("配对 @小红")
        second = self.say("配对 @小红")
        self.assertEqual(first.text, second.text)
        self.assertIn("💞 配对结果", first.text)
        self.assertIn("对象：小红", first.text)

    def test_symmetric(self):
        a = self.say("配对 @小红", uid=UID_A, nickname=NAME_A)
        b = self.say("配对 @小明", uid=UID_B, nickname=NAME_B)
        score_a = [line for line in a.text.splitlines() if "缘分指数" in line][0]
        score_b = [line for line in b.text.splitlines() if "缘分指数" in line][0]
        self.assertEqual(score_a, score_b)

    def test_changes_next_day(self):
        today = self.say("配对 @小红").text
        self.clock.advance(86400)
        tomorrow = self.say("配对 @小红").text
        self.assertNotEqual(today, tomorrow)

    def test_score_in_range(self):
        reply = self.say("配对 @小红")
        score = int([line for line in reply.text.splitlines() if "缘分指数" in line][0].split("：")[1].split("/")[0])
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 100)


class GiftTests(SocialTestBase):
    def test_buys_when_not_in_bag(self):
        self.give(UID_A, 100)
        reply = self.say("礼物 @小红 玫瑰")
        self.assertIn("🎁 礼物已送到", reply.text)
        self.assertIn("代买，20 金币", reply.text)
        self.assertEqual(self.coins(UID_A), 80)

    def test_takes_from_bag_first(self):
        self.give(UID_A, 100)
        with self.store.tx() as conn:
            items.seed(conn)
            items.add(conn, ROOM, UID_A, "rose", 1)
        reply = self.say("礼物 @小红 玫瑰")
        self.assertIn("背包取 1 件", reply.text)
        self.assertEqual(self.coins(UID_A), 100)
        with self.store.tx(immediate=False) as conn:
            self.assertEqual(items.qty(conn, ROOM, UID_A, "rose"), 0)

    def test_unknown_gift(self):
        reply = self.say("礼物 @小红 钻石")
        self.assertIn("没有叫「钻石」的礼物", reply.text)
        self.assertIn("玫瑰 20", reply.text)

    def test_not_enough_coins(self):
        reply = self.say("礼物 @小红 戒指")
        self.assertIn("金币不够", reply.text)
        self.assertIn("520", reply.text)
        self.assertEqual(self.coins(UID_A), 0)

    def test_missing_item(self):
        reply = self.say("礼物 @小红")
        self.assertIn("礼物要写清楚送给谁", reply.text)

    def test_records_count_and_audit(self):
        self.give(UID_A, 100)
        before = self.audits()
        self.say("礼物 @小红 巧克力")
        with self.store.tx(immediate=False) as conn:
            row = conn.execute(
                "SELECT value FROM kv WHERE room = ? AND key = ?", (ROOM, "gifts:wxid_a:wxid_b")
            ).fetchone()
        self.assertEqual(str(row["value"]), "1")
        self.assertEqual(self.audits(), before + 1)


class BabyTests(SocialTestBase):
    def test_switch_off_is_silent(self):
        self.assertIsNone(self.say("宝宝 看"))

    def test_bear_on_first_look(self):
        self.enable(switches.BABY)
        reply = self.say("宝宝 看")
        self.assertIn("🍼 我的宝宝", reply.text)
        self.assertIn("小明的宝宝", reply.text)
        self.assertIn("胎儿", reply.text)

    def test_feed_costs_and_grows(self):
        self.enable(switches.BABY)
        self.give(UID_A, 100)
        self.say("宝宝 看")
        reply = self.say("宝宝 喂养")
        self.assertIn("🍼 喂饱了", reply.text)
        self.assertIn("金币：-20", reply.text)
        self.assertEqual(self.coins(UID_A), 80)
        with self.store.tx(immediate=False) as conn:
            row = conn.execute("SELECT care, exp FROM baby WHERE room = ?", (ROOM,)).fetchone()
        self.assertEqual(int(row["care"]), 100)
        self.assertEqual(int(row["exp"]), 5)

    def test_feed_daily_cap(self):
        self.enable(switches.BABY)
        self.give(UID_A, 200)
        self.say("宝宝 看")
        for _ in range(3):
            self.say("宝宝 喂养")
        reply = self.say("宝宝 喂养")
        self.assertIn("今天喂过 3 次了", reply.text)
        self.assertEqual(self.coins(UID_A), 140)

    def test_feed_without_coins(self):
        self.enable(switches.BABY)
        self.say("宝宝 看")
        reply = self.say("宝宝 喂养")
        self.assertIn("金币不够", reply.text)

    def test_stage_up(self):
        self.enable(switches.BABY)
        self.give(UID_A, 1000)
        self.say("宝宝 看")
        with self.store.tx() as conn:
            conn.execute("UPDATE baby SET exp = 95, stage = 0 WHERE room = ?", (ROOM,))
        reply = self.say("宝宝 喂养")
        self.assertIn("长到「婴儿」了", reply.text)

    def test_rename(self):
        self.enable(switches.BABY)
        self.say("宝宝 看")
        reply = self.say("宝宝 改名 小豆丁")
        self.assertIn("🏷 改名成功", reply.text)
        self.assertIn("小豆丁", reply.text)

    def test_rename_too_long(self):
        self.enable(switches.BABY)
        self.say("宝宝 看")
        reply = self.say("宝宝 改名 一二三四五六七八九")
        self.assertIn("最多 8 个字", reply.text)

    def test_rename_duplicate(self):
        self.enable(switches.BABY)
        self.say("宝宝 看")
        self.say("宝宝 改名 小豆丁")
        reply = self.say("宝宝 看", uid=UID_B, nickname=NAME_B)
        self.assertIn("小红的宝宝", reply.text)
        reply = self.say("宝宝 改名 小豆丁", uid=UID_B, nickname=NAME_B)
        self.assertIn("已经有叫「小豆丁」的宝宝了", reply.text)

    def test_super_is_v2(self):
        self.enable(switches.BABY)
        reply = self.say("宝宝 超极")
        self.assertIn("v2 才开放", reply.text)

    def test_rename_without_baby(self):
        self.enable(switches.BABY)
        reply = self.say("宝宝 改名 小豆丁")
        self.assertIn("你还没有宝宝", reply.text)

    def test_bear_uses_marriage_parents(self):
        self.enable(switches.MARRIAGE)
        self.enable(switches.BABY)
        self.say("结婚 @小红")
        self.say("结婚 同意", uid=UID_B, nickname=NAME_B)
        self.say("宝宝 看", uid=UID_B, nickname=NAME_B)
        with self.store.tx(immediate=False) as conn:
            row = conn.execute("SELECT parent_low, parent_high FROM baby WHERE room = ?", (ROOM,)).fetchone()
        self.assertEqual(row["parent_low"], UID_A)
        self.assertEqual(row["parent_high"], UID_B)
