"""群成员事件 → 邀请统计（`docs/design/playhub.md` §8.7 C1 消费侧）。

框架给出的是**已确认**的群成员事件（`GroupMemberEvent`），这里只验证消费侧口径：

- 群级计数（`invite_daily`）：凡是有成员 ID 的 join / leave 都记。
- 按人归属（`invite_credit`）：只有「结构化模板里带邀请人占位符」的入群才记；
  名单差分补录（`source == "membership"`）、角色词（「群主将…移出」）、
  邀请人等于被邀请人，一律不产生归属。
- 这些系统消息不回复、不进活跃榜、也不消耗关键词路由。

不联网、不 sleep，数据库落在 `TemporaryDirectory` 里。
"""
from __future__ import annotations

import unittest

from test_playhub_batch4 import Batch4Base, FakeMsg, NAME_A, ROOM, UID_A, UID_B, UID_C


def event(kind="join", member_id=UID_C, *, actor_id="", source="system_structured", room=ROOM):
    """框架塞进 `message.data["group_event"]` 的那份字典。"""
    return {
        "kind": kind,
        "room_id": room,
        "member_id": member_id,
        "nickname": "新来的",
        "source": source,
        "actor_id": actor_id,
    }


class EventMsg(FakeMsg):
    """10002 群系统消息：正文是模板文本，`group_event` 由框架解析后填好。"""

    def __init__(self, group_event, *, chat_id=ROOM):
        super().__init__("", chat_id=chat_id, sender_id="wxid_system")
        self.type = 10002
        self.data = {"group_event": group_event}


class InviteFeedTests(Batch4Base):
    def feed(self, group_event, *, chat_id=ROOM):
        return self.app.handle_message(self.ctx, EventMsg(group_event, chat_id=chat_id))

    def daily(self):
        with self.store.tx(immediate=False) as conn:
            row = conn.execute(
                "SELECT joins, leaves, invite_joins FROM invite_daily WHERE room = ?", (ROOM,)
            ).fetchone()
        return None if row is None else tuple(int(value) for value in row)

    def credits(self):
        with self.store.tx(immediate=False) as conn:
            rows = conn.execute(
                "SELECT inviter, invitee, day FROM invite_credit WHERE room = ?"
                " ORDER BY inviter, invitee",
                (ROOM,),
            ).fetchall()
        return [(str(inviter), str(invitee)) for inviter, invitee, _day in rows]

    def activity_rows(self) -> int:
        with self.store.tx(immediate=False) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM activity_daily").fetchone()[0])

    # -- 群级 ---------------------------------------------------------------
    def test_structured_invite_counts_group_and_person(self):
        self.assertIsNone(self.feed(event(actor_id=UID_A)))
        self.assertEqual(self.daily(), (1, 0, 1))
        self.assertEqual(self.credits(), [(UID_A, UID_C)])

    def test_join_without_actor_counts_group_only(self):
        self.feed(event())
        self.assertEqual(self.daily(), (1, 0, 0))
        self.assertEqual(self.credits(), [])

    def test_leave_counts_leaves_only(self):
        self.feed(event(kind="leave", member_id=UID_B, source="membership"))
        self.assertEqual(self.daily(), (0, 1, 0))
        self.assertEqual(self.credits(), [])

    # -- 按人归属的硬约束 ---------------------------------------------------
    def test_membership_backfill_never_invents_an_inviter(self):
        self.feed(event(actor_id=UID_A, source="membership"))
        self.assertEqual(self.daily(), (1, 0, 0))
        self.assertEqual(self.credits(), [])

    def test_actor_equal_to_member_is_dropped(self):
        self.feed(event(actor_id=UID_C))
        self.assertEqual(self.daily(), (1, 0, 0))
        self.assertEqual(self.credits(), [])

    def test_redelivered_event_is_counted_once(self):
        self.feed(event(actor_id=UID_A))
        self.feed(event(actor_id=UID_A))
        self.assertEqual(self.daily(), (1, 0, 1))
        self.assertEqual(self.credits(), [(UID_A, UID_C)])

    def test_two_newcomers_on_the_same_day_both_count(self):
        self.feed(event(actor_id=UID_A, member_id=UID_C))
        self.feed(event(actor_id=UID_A, member_id=UID_B))
        self.assertEqual(self.daily(), (2, 0, 2))
        self.assertEqual(self.credits(), [(UID_A, UID_B), (UID_A, UID_C)])

    # -- 不吃别的通路 -------------------------------------------------------
    def test_event_does_not_count_activity(self):
        self.feed(event(actor_id=UID_A))
        self.assertEqual(self.activity_rows(), 0)

    def test_event_for_another_room_is_not_recorded(self):
        self.assertIsNone(self.feed(event(actor_id=UID_A, room="9999@chatroom")))
        self.assertIsNone(self.daily())

    def test_non_member_change_system_message_is_swallowed(self):
        """是群系统消息但不含进退群：吃掉，不落到关键词路由。"""
        self.assertIsNone(self.feed({"room_id": ROOM, "kind": "notice"}))
        self.assertIsNone(self.daily())

    def test_plain_message_path_is_untouched(self):
        reply = self.say("签到")
        self.assertIsNotNone(reply)


class InviteShowsUpInTheEngineTests(Batch4Base):
    def feed(self, group_event):
        return self.app.handle_message(self.ctx, EventMsg(group_event))

    def test_invite_report_uses_recorded_events(self):
        self.feed(event(actor_id=UID_A))
        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO member_seen (room, uid, nickname, role, first_seen, last_seen, left_at)"
                " VALUES (?, ?, ?, 'member', ?, ?, NULL)"
                " ON CONFLICT(room, uid) DO UPDATE SET nickname = excluded.nickname",
                (ROOM, UID_A, NAME_A, self.clock(), self.clock()),
            )
        reply = self.say("邀请")
        self.assertIn("入群 1 次 · 邀请入群 1 次", reply.text)
        self.assertIn(f"1. {NAME_A} — 1 人", reply.text)


if __name__ == "__main__":
    unittest.main()
