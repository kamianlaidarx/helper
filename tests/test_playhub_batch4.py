"""第 4 批单测：活跃榜、潜水、邀请、改名、小黑屋、黑名单（`docs/design/playhub.md:1702-1707`）。

覆盖四件事：

1. **活跃计数**：只有「人话」进 `activity_daily`——命令不算、机器人自己不算、
   小黑屋/黑名单里的人不算。
2. **活跃榜周期**：日/周/月/总，周一为周起点，跨月/跨年边界由 `clock.range_of` 保证。
3. **权限**：`改名`/`小黑屋`/`黑名单` 的写操作只放行 owner/admin，
   `member`/`unknown` 一律拒绝（失败关闭），且不写业务表。
4. **小黑屋口径**：单次 ≤60 分钟、每日 ≤10 次、到期自动解除、`解除` 立刻生效。

不联网、不 sleep、不写仓库外路径：数据库落在 `TemporaryDirectory` 里。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from plugins.playhub.app import App
from plugins.playhub.core import Store
from plugins.playhub.core import activity, clock, kv
from plugins.playhub.core.clock import CN
from plugins.playhub.engines import ranking
from plugins.playhub.engines import moderation
from plugins.playhub.registry import ACTION_PLAN, BINDINGS, CURRENT_BATCH, check_bindings

ROOM = "12345678@chatroom"
UID_A = "wxid_a"
UID_B = "wxid_b"
UID_C = "wxid_c"
NAME_A = "小明"
NAME_B = "小红"
NAME_C = "小刚"


class FakeMsg:
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
    def __init__(self, names: dict[str, str]) -> None:
        self._names = dict(names)

    def resolve(self, name, room=None):
        if name not in self._names:
            raise KeyError(name)
        return FakeMember(self._names[name])


class FakeCtx:
    """带群角色与成员快照的 ctx 替身（`MemberFeed` 读 `group_member_role`）。"""

    def __init__(self, names: dict[str, str], roles: dict[str, str], members: list[dict] | None = None):
        self.members = FakeMembers(names)
        self._roles = dict(roles)
        self._snapshot = members

    def group_member_role(self, room, uid, *, expected_session=None):
        return self._roles.get(uid, "unknown")

    def group_members(self, room, *, expected_session=None):
        return self._snapshot


class FakeClock:
    def __init__(self) -> None:
        self.ts = int(datetime.fromisoformat("2026-11-01T12:00:00").replace(tzinfo=CN).timestamp())

    def __call__(self) -> int:
        return self.ts

    def advance(self, seconds: int) -> None:
        self.ts += seconds


class Batch4Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.store = Store(self.tmp / "playhub.sqlite3", account="acct")
        self.store.migrate()
        self.clock = FakeClock()
        self.ctx = FakeCtx(
            {NAME_A: UID_A, NAME_B: UID_B, NAME_C: UID_C},
            {UID_A: "owner", UID_B: "member", UID_C: "admin"},
        )
        self.app = self.new_app()

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def new_app(self, ctx=None) -> App:
        return App(store=self.store, now_fn=self.clock, repo_root=self.tmp)

    def say(self, text: str, *, uid: str = UID_A, nickname: str = NAME_A, ctx=None):
        return self.app.handle_message(ctx if ctx is not None else self.ctx, FakeMsg(text, sender_id=uid, nickname=nickname))

    # -- 辅助 ---------------------------------------------------------------
    def seed_activity(self, uid: str, day: str, count: int = 1, *, nickname: str = "") -> None:
        with self.store.tx() as conn:
            for _ in range(count):
                activity.record(conn, room=ROOM, uid=uid, day=day, now=self.clock(), nickname=nickname)

    def activity_rows(self) -> list[tuple[str, str, int]]:
        with self.store.tx(immediate=False) as conn:
            rows = conn.execute(
                "SELECT uid, day, messages FROM activity_daily WHERE room = ? ORDER BY uid, day", (ROOM,)
            ).fetchall()
        return [(str(uid), str(day), int(n)) for uid, day, n in rows]

    def seed_name(self, uid: str, name: str) -> None:
        """只写 `member_seen` 里的昵称，顺带一条历史计数（不影响今天的榜）。"""
        self.seed_activity(uid, "2020-01-01", 1, nickname=name)
    def kv(self, key: str) -> str | None:
        with self.store.tx(immediate=False) as conn:
            row = conn.execute("SELECT value FROM kv WHERE room = ? AND key = ?", (ROOM, key)).fetchone()
        return None if row is None else str(row[0])

    def jail_rows(self) -> list[tuple[str, int]]:
        with self.store.tx(immediate=False) as conn:
            rows = conn.execute("SELECT uid, until FROM jail WHERE room = ?", (ROOM,)).fetchall()
        return [(str(uid), int(until)) for uid, until in rows]

    def block_rows(self) -> list[str]:
        with self.store.tx(immediate=False) as conn:
            rows = conn.execute("SELECT uid FROM blocklist WHERE room = ? AND scope = 'play'", (ROOM,)).fetchall()
        return [str(uid) for (uid,) in rows]


# ---------------------------------------------------------------------------
# 活跃计数 + 活跃榜
# ---------------------------------------------------------------------------
class ActivityRecordTests(Batch4Base):
    def test_plain_text_is_counted(self):
        self.say("今晚吃什么")
        self.assertEqual(self.activity_rows(), [(UID_A, "2026-11-01", 1)])

    def test_command_is_not_counted(self):
        # §3.4：命令消息不重复计入
        self.assertIsNotNone(self.say("签到"))
        self.assertEqual(self.activity_rows(), [])

    def test_self_message_is_not_counted(self):
        self.app.handle_message(self.ctx, FakeMsg("今晚吃什么", is_self=True))
        self.assertEqual(self.activity_rows(), [])

    def test_jailed_member_is_silent_and_not_counted(self):
        self.say(f"小黑屋 {NAME_B} 10")              # B 进小黑屋
        self.assertIsNone(self.say("今晚吃什么", uid=UID_B, nickname=NAME_B))
        self.assertEqual(self.activity_rows(), [])

    def test_blocked_member_is_silent_and_not_counted(self):
        self.say(f"黑名单 加 {NAME_B}")
        self.assertIsNone(self.say("签到", uid=UID_B, nickname=NAME_B))
        self.assertIsNone(self.say("今晚吃什么", uid=UID_B, nickname=NAME_B))
        self.assertEqual(self.activity_rows(), [])

    def test_member_seen_records_nickname(self):
        self.say("你好")
        with self.store.tx(immediate=False) as conn:
            row = conn.execute("SELECT nickname FROM member_seen WHERE room = ? AND uid = ?", (ROOM, UID_A)).fetchone()
        self.assertEqual(row[0], NAME_A)


class ActivityPeriodTests(unittest.TestCase):
    """`clock.range_of`：周一为周起点，跨月/跨年由 date 运算保证。"""

    def _ts(self, text: str) -> int:
        return int(datetime.fromisoformat(text).replace(tzinfo=CN).timestamp())

    def test_day(self):
        self.assertEqual(clock.range_of("day", self._ts("2026-11-01T12:00:00")), ("2026-11-01", "2026-11-01"))

    def test_week_starts_monday(self):
        # 2026-11-01 是周日，本周一是 10-26
        self.assertEqual(clock.range_of("week", self._ts("2026-11-01T12:00:00")), ("2026-10-26", "2026-11-01"))
        # 周一当天就是自己的起点
        self.assertEqual(clock.range_of("week", self._ts("2026-10-26T00:30:00")), ("2026-10-26", "2026-10-26"))

    def test_week_across_year(self):
        # 2026-01-01 是周四，那一周从 2025-12-29 开始
        self.assertEqual(clock.range_of("week", self._ts("2026-01-01T09:00:00")), ("2025-12-29", "2026-01-01"))

    def test_month(self):
        self.assertEqual(clock.range_of("month", self._ts("2026-02-28T23:00:00")), ("2026-02-01", "2026-02-28"))
        self.assertEqual(clock.range_of("month", self._ts("2026-03-01T00:10:00")), ("2026-03-01", "2026-03-01"))

    def test_all(self):
        self.assertEqual(clock.range_of("all"), (None, None))

    def test_unknown_period_raises(self):
        with self.assertRaises(ValueError):
            clock.range_of("quarter")


class ActivityBoardTests(Batch4Base):
    def test_week_ignores_previous_week(self):
        self.seed_activity(UID_A, "2026-10-27", 2, nickname=NAME_A)   # 本周
        self.seed_activity(UID_B, "2026-10-20", 9, nickname=NAME_B)   # 上周
        reply = self.say("活跃榜 周")
        self.assertIn(NAME_A, reply.text)
        self.assertNotIn(NAME_B, reply.text)
        self.assertIn("本周", reply.text)

    def test_all_counts_everything(self):
        self.seed_activity(UID_A, "2026-10-27", 2, nickname=NAME_A)
        self.seed_activity(UID_B, "2026-10-20", 9, nickname=NAME_B)
        reply = self.say("活跃榜 总")
        self.assertIn(NAME_B, reply.text)
        self.assertIn("9 条", reply.text)

    def test_day_period(self):
        self.seed_activity(UID_A, "2026-10-31", 5, nickname=NAME_A)
        self.seed_activity(UID_B, "2026-11-01", 1, nickname=NAME_B)
        reply = self.say("活跃榜 日")
        self.assertIn(NAME_B, reply.text)
        self.assertNotIn(NAME_A, reply.text)

    def test_empty_board(self):
        reply = self.say("活跃榜")
        self.assertIn("还没有发言记录", reply.text)

    def test_ties_sorted_by_uid(self):
        self.seed_activity(UID_B, "2026-11-01", 3, nickname=NAME_B)
        self.seed_activity(UID_A, "2026-11-01", 3, nickname=NAME_A)
        with self.store.tx(immediate=False) as conn:
            rows = ranking.top(conn, board="activity", room=ROOM, period="day", now=self.clock())
        self.assertEqual([row.uid for row in rows], [UID_A, UID_B])

    def test_ranking_top_accepts_activity_arg(self):
        self.seed_activity(UID_A, "2026-11-01", 1, nickname=NAME_A)
        reply = self.say("排行榜 活跃 月")
        self.assertIn("活跃榜", reply.text)
        self.assertIn("本月", reply.text)


# ---------------------------------------------------------------------------
# 潜水 / 邀请 / 改名
# ---------------------------------------------------------------------------
class DiveTests(Batch4Base):
    def test_lists_idle_members(self):
        self.seed_activity(UID_B, "2026-10-20", 1, nickname=NAME_B)   # 12 天前
        reply = self.say("潜水 7")
        self.assertIn(NAME_B, reply.text)
        self.assertIn("12 天", reply.text)
        self.assertIn("只统计，不清理", reply.text)

    def test_recent_speaker_not_listed(self):
        self.seed_activity(UID_B, "2026-10-31", 1, nickname=NAME_B)
        reply = self.say("潜水 7")
        self.assertIn("一个都没有", reply.text)

    def test_no_records_at_all(self):
        reply = self.say("潜水")
        self.assertIn("还没有任何发言记录", reply.text)

    def test_bad_days(self):
        self.assertIn("天数要是正整数", self.say("潜水 七天").text)


class InviteTests(Batch4Base):
    def test_group_level_only_when_no_inviter(self):
        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO invite_daily (room, day, joins, leaves, kicked, invite_joins) "
                "VALUES (?, ?, 3, 0, 0, 3)",
                (ROOM, "2026-10-30"),
            )
        reply = self.say("邀请 7")
        self.assertIn("群级共 3 次邀请入群", reply.text)
        self.assertIn("入群 3 次", reply.text)

    def test_per_inviter_list(self):
        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO invite_credit (room, inviter, invitee, day, at) VALUES (?, ?, ?, ?, ?)",
                (ROOM, UID_A, "wxid_x", "2026-10-30", self.clock()),
            )
            conn.execute(
                "INSERT INTO invite_credit (room, inviter, invitee, day, at) VALUES (?, ?, ?, ?, ?)",
                (ROOM, UID_A, "wxid_y", "2026-10-30", self.clock()),
            )
            conn.execute(
                "INSERT INTO invite_daily (room, day, joins, leaves, kicked, invite_joins) "
                "VALUES (?, ?, 2, 0, 0, 2)",
                (ROOM, "2026-10-30"),
            )
            activity.record(conn, room=ROOM, uid=UID_A, day="2026-10-30", now=self.clock(), nickname=NAME_A)
        reply = self.say("邀请")
        self.assertIn(f"1. {NAME_A} — 2 人", reply.text)

    def test_empty(self):
        self.assertIn("没有入群记录", self.say("邀请 7").text)


class RenameTests(Batch4Base):
    def test_member_is_denied(self):
        reply = self.say("改名 开", uid=UID_B, nickname=NAME_B)
        self.assertIn("只有群主和管理员", reply.text)
        self.assertIsNone(self.kv("groupstat.rename_watch"))

    def test_unknown_role_is_denied(self):
        ctx = FakeCtx({NAME_A: UID_A}, {})          # 谁都查不到角色
        reply = self.say("改名 现在", ctx=ctx)
        self.assertIn("只有群主和管理员", reply.text)
        self.assertIn("unknown", reply.text)

    def test_owner_toggles(self):
        self.assertIn("已开启", self.say("改名 开").text)
        self.assertEqual(self.kv("groupstat.rename_watch"), "1")
        self.assertIn("已关闭", self.say("改名 关").text)
        self.assertEqual(self.kv("groupstat.rename_watch"), "0")

    def test_scan_now_detects_change(self):
        # 基线是巡检快照（§4.2），不是 member_seen.nickname：后者会被发言昵称覆盖。
        with self.store.tx() as conn:
            kv.put(conn, ROOM, "groupstat.rename_snapshot", json.dumps({UID_A: "旧名字"}), self.clock())
        ctx = FakeCtx(
            {NAME_A: UID_A},
            {UID_A: "owner"},
            members=[{"wxid": UID_A, "nickname": "新名字"}],
        )
        reply = self.say("改名 现在", ctx=ctx)
        self.assertIn("发现 1 处改名", reply.text)
        self.assertIn("旧名字 → 新名字", reply.text)
        with self.store.tx(immediate=False) as conn:
            rows = conn.execute("SELECT old_name, new_name FROM nickname_log WHERE room = ?", (ROOM,)).fetchall()
        self.assertEqual([tuple(row) for row in rows], [("旧名字", "新名字")])

    def test_scan_without_snapshot(self):
        reply = self.say("改名 现在")               # FakeCtx 默认 members=None
        self.assertIn("读不到群成员快照", reply.text)

    def test_bad_sub(self):
        self.assertIn("只认", self.say("改名 关掉").text)


# ---------------------------------------------------------------------------
# 小黑屋
# ---------------------------------------------------------------------------
class JailTests(Batch4Base):
    def test_owner_can_jail(self):
        reply = self.say(f"小黑屋 {NAME_B} 10")
        self.assertIn("已进小黑屋 10 分钟", reply.text)
        self.assertEqual(self.jail_rows(), [(UID_B, self.clock() + 600)])

    def test_default_ten_minutes(self):
        self.say(f"小黑屋 {NAME_B}")
        until = self.jail_rows()[0][1]
        self.assertEqual(until - self.clock(), 600)

    def test_clamped_to_sixty(self):
        self.say(f"小黑屋 {NAME_B} 999")
        until = self.jail_rows()[0][1]
        self.assertEqual(until - self.clock(), 3600)

    def test_bad_minutes(self):
        self.assertIn("分钟数要是整数", self.say(f"小黑屋 {NAME_B} 十分钟").text)
        self.assertIn("分钟数要大于 0", self.say(f"小黑屋 {NAME_B} 0").text)

    def test_daily_cap(self):
        for _ in range(moderation.JAIL_DAILY_CAP):
            self.assertIn("已进小黑屋", self.say(f"小黑屋 {NAME_B} 1").text)
        reply = self.say(f"小黑屋 {NAME_B} 1")
        self.assertIn("今天的关人次数用完了", reply.text)

    def test_member_is_denied(self):
        reply = self.say(f"小黑屋 {NAME_A} 10", uid=UID_B, nickname=NAME_B)
        self.assertIn("只有群主和管理员", reply.text)
        self.assertEqual(self.jail_rows(), [])

    def test_admin_can_jail(self):
        reply = self.say(f"小黑屋 {NAME_B} 5", uid=UID_C, nickname=NAME_C)
        self.assertIn("已进小黑屋 5 分钟", reply.text)

    def test_self_target_rejected(self):
        self.assertIn("没有意义", self.say(f"小黑屋 {NAME_A} 5").text)

    def test_unknown_target(self):
        self.assertIn("找不到「查无此人」", self.say("小黑屋 查无此人 5").text)

    def test_no_target_asks_for_one(self):
        self.assertIn("没认出要操作谁", self.say("小黑屋 10").text)

    def test_list_and_release(self):
        self.seed_name(UID_B, NAME_B)
        self.say(f"小黑屋 {NAME_B} 10")
        self.assertIn(NAME_B, self.say("小黑屋 列表").text)
        self.assertIn("已解除", self.say(f"小黑屋 解除 {NAME_B}").text)
        self.assertEqual(self.jail_rows(), [])
        self.assertIn("现在不在小黑屋", self.say(f"小黑屋 解除 {NAME_B}").text)

    def test_expires_and_stops_responding(self):
        self.say(f"小黑屋 {NAME_B} 1")
        self.assertIsNone(self.say("今晚吃什么", uid=UID_B, nickname=NAME_B))
        self.clock.advance(61)
        # 到期后恢复：签到会回卡
        reply = self.say("签到", uid=UID_B, nickname=NAME_B)
        self.assertIsNotNone(reply)
        with self.store.tx(immediate=False) as conn:
            self.assertFalse(moderation.is_jailed(conn, ROOM, UID_B, self.clock()))

    def test_list_cleans_expired_rows(self):
        self.say(f"小黑屋 {NAME_B} 1")
        self.clock.advance(120)
        reply = self.say("小黑屋 列表")
        self.assertIn("当前没有人被关", reply.text)
        self.assertIn("顺手清理了 1 条到期记录", reply.text)
        self.assertEqual(self.jail_rows(), [])

    def test_switch_off_is_silent(self):
        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO kv (room, key, value, updated_at) VALUES (?, ?, ?, ?)",
                (ROOM, "feature.jail", "0", self.clock()),
            )
        self.assertIsNone(self.say(f"小黑屋 {NAME_B} 10"))
        self.assertEqual(self.jail_rows(), [])


# ---------------------------------------------------------------------------
# 黑名单
# ---------------------------------------------------------------------------
class BlockTests(Batch4Base):
    def test_add_list_remove(self):
        self.seed_name(UID_B, NAME_B)
        self.assertIn("已加入黑名单", self.say(f"黑名单 加 {NAME_B}").text)
        self.assertEqual(self.block_rows(), [UID_B])
        self.assertIn(NAME_B, self.say("黑名单 列").text)
        self.assertIn("已移出黑名单", self.say(f"黑名单 删 {NAME_B}").text)
        self.assertEqual(self.block_rows(), [])
        self.assertIn("不在黑名单里", self.say(f"黑名单 删 {NAME_B}").text)

    def test_member_is_denied(self):
        reply = self.say(f"黑名单 加 {NAME_A}", uid=UID_B, nickname=NAME_B)
        self.assertIn("只有群主和管理员", reply.text)
        self.assertEqual(self.block_rows(), [])

    def test_empty_list(self):
        self.assertIn("没有人在黑名单里", self.say("黑名单 列").text)

    def test_bare_returns_usage(self):
        # 裸发走触发卡（app 层），不是引擎的 USAGE_BLOCK
        reply = self.say("黑名单")
        self.assertIn("黑名单 加 @对象", reply.text)
        self.assertIn("只是不理他的游戏命令", reply.text)
        self.assertNotIn("插件", reply.text)

    def test_self_target_rejected(self):
        self.assertIn("不能把自己", self.say(f"黑名单 加 {NAME_A}").text)

    def test_blocked_member_gets_nothing(self):
        # 拉黑之后这个人彻底静默：连「黑名单 列」都不回（§8.2）
        self.say(f"黑名单 加 {NAME_B}")
        self.assertIsNone(self.say("黑名单 列", uid=UID_B, nickname=NAME_B))

    def test_other_member_can_list(self):
        self.say(f"黑名单 加 {NAME_B}")
        self.seed_name(UID_B, NAME_B)
        ctx = FakeCtx({NAME_A: UID_A, NAME_B: UID_B, NAME_C: UID_C}, {UID_A: "owner", UID_C: "member"})
        reply = self.say("黑名单 列", uid=UID_C, nickname=NAME_C, ctx=ctx)
        self.assertIn(NAME_B, reply.text)

    def test_switch_off_is_silent(self):
        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO kv (room, key, value, updated_at) VALUES (?, ?, ?, ?)",
                (ROOM, "feature.blocklist", "off", self.clock()),
            )
        self.assertIsNone(self.say(f"黑名单 加 {NAME_B}"))
        self.assertEqual(self.block_rows(), [])


# ---------------------------------------------------------------------------
# 接线自检
# ---------------------------------------------------------------------------
class Batch4BindingTests(unittest.TestCase):
    def test_all_actions_bound(self):
        self.assertEqual(CURRENT_BATCH, 4)
        self.assertEqual(len(BINDINGS), 44)
        self.assertEqual(check_bindings(), 44)

    def test_no_action_left_unplanned(self):
        self.assertEqual([a for a, b in ACTION_PLAN.items() if b is None], [])

    def test_batch4_actions_are_bound(self):
        for action in ("ranking.activity", "moderation.jail", "moderation.block",
                       "groupstat.dive", "groupstat.invite", "groupstat.rename",
                       "daily.fortune_explain"):
            with self.subTest(action=action):
                self.assertIn(action, BINDINGS)
                self.assertEqual(ACTION_PLAN[action], 4)


if __name__ == "__main__":
    unittest.main()
