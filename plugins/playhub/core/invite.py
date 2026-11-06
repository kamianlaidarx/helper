"""进退群与邀请归属（`invite_daily` / `invite_credit`，§8.7 C1）。

框架只在**已确认**的群成员事件上调用这里（`GroupMemberEvent`，来自
`wxbot_framework/group_events.py`）。两类记录的口径不同：

- `invite_daily(room, day)`：按天累加的群级计数。凡是有成员 ID 的 join / leave 都记，
  不需要邀请人——`邀请` 功能的主数字就是它（`engines/groupstat.py`）。
- `invite_credit(room, inviter, invitee, day)`：按人归属，只有同时满足下面三条才写：
  ①`kind == "join"`；②`actor_id` 非空且不等于被邀请人；③`source != "membership"`。
  第 ③ 条是硬约束：成员快照差分（`source == "membership"`）是**补录**，它只知道
  「某人的名单状态变了」，不知道是谁拉进来的。让补录产生邀请归属，等于凭空造一个
  邀请人——`邀请榜` 会给出错误的榜单，所以宁可不记。

日切一律由调用方用 `clock.day_of()` 生成后传参（同 `activity.py`，禁止 SQL 的
`date('now')`）。
"""
from __future__ import annotations

import sqlite3

#: 能产生邀请归属的事件来源；`membership`（名单差分）不在其中。
CREDITED_SOURCES = ("system_structured", "system_text")


def record(
    conn: sqlite3.Connection,
    *,
    room: str,
    kind: str,
    member_id: str,
    now: int,
    day: str,
    actor_id: str = "",
    source: str = "",
) -> bool:
    """记一条进退群；返回「这条是否算进了群级计数」。参数不全时返回 False。

    `actor_id` 只对入群有意义；退休群时它记的是「谁把他移出去的」，目前不写表。
    """
    if not room or not member_id or kind not in ("join", "leave"):
        return False
    if not day:
        return False

    if kind == "leave":
        conn.execute(
            """
            INSERT INTO invite_daily (room, day, leaves) VALUES (?, ?, 1)
            ON CONFLICT(room, day) DO UPDATE SET leaves = leaves + 1
            """,
            (room, day),
        )
        return True

    credited = (
        bool(actor_id)
        and actor_id != member_id
        and (not source or source in CREDITED_SOURCES)
    )
    conn.execute(
        """
        INSERT INTO invite_daily (room, day, joins, invite_joins) VALUES (?, ?, 1, ?)
        ON CONFLICT(room, day) DO UPDATE SET
            joins        = joins + 1,
            invite_joins = invite_joins + excluded.invite_joins
        """,
        (room, day, 1 if credited else 0),
    )
    if credited:
        # 同一个人同一天被「邀请」两次（退群再进）只记一次：
        # 榜单要的是人头，不是人次（`engines/groupstat.py` 按 inviter 去重）。
        conn.execute(
            """
            INSERT OR IGNORE INTO invite_credit (room, inviter, invitee, day, at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (room, actor_id, member_id, day, now),
        )
    return True
