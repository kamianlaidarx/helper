"""活跃度与成员在场记录（第 4 批）。

两张表分工（`core/schema.sql`）：

- `activity_daily(room, uid, day, messages, chars, first_at, last_at)`：按天的发言计数，
  活跃榜与潜水的唯一数据源。**只数人话**——机器人自己的消息、命令消息都在
  `app.py` 里过滤掉（§3.4「沿用既有『有效消息』判定，命令消息不重复计入」）。
- `member_seen(room, uid, nickname, role, first_seen, last_seen, left_at)`：最后一次露面。
  潜水榜按它算「多少天没说话」，改名巡检也靠它比对旧昵称。

为什么不在这里判权限：本模块只读写事实，谁能看榜、谁能禁言由引擎 +
`core/permissions.py` 决定（§8.2「权限判定统一走 `ctx.group_member_role()`」）。

日切一律由调用方用 `clock.day_of()` 生成后传参，**禁止 SQL 的 `date('now')`**
（SQLite 默认 UTC，会错 8 小时，见 `clock.py` 模块注释）。
"""
from __future__ import annotations

import sqlite3


def record(
    conn: sqlite3.Connection,
    *,
    room: str,
    uid: str,
    day: str,
    now: int,
    nickname: str = "",
    role: str = "",
    chars: int = 0,
) -> None:
    """记一条发言：`activity_daily` 累加，`member_seen` 刷新在场。

    `nickname` / `role` 允许为空串：取不到昵称时不能让这条计数失败，
    否则「活跃榜」会因为一个改过名的成员整天不记数。空串不覆盖已有值。
    """
    if not room or not uid or not day:
        return
    conn.execute(
        """
        INSERT INTO activity_daily (room, uid, day, messages, chars, first_at, last_at)
        VALUES (?, ?, ?, 1, ?, ?, ?)
        ON CONFLICT(room, uid, day) DO UPDATE SET
            messages = messages + 1,
            chars    = chars + excluded.chars,
            last_at  = excluded.last_at
        """,
        (room, uid, day, max(0, int(chars)), now, now),
    )
    conn.execute(
        """
        INSERT INTO member_seen (room, uid, nickname, role, first_seen, last_seen, left_at)
        VALUES (?, ?, ?, ?, ?, ?, NULL)
        ON CONFLICT(room, uid) DO UPDATE SET
            nickname  = CASE WHEN excluded.nickname <> '' THEN excluded.nickname
                             ELSE member_seen.nickname END,
            role      = CASE WHEN excluded.role <> '' THEN excluded.role
                             ELSE member_seen.role END,
            last_seen = excluded.last_seen,
            left_at   = NULL
        """,
        (room, uid, nickname or "", role or "", now, now),
    )


def scores(
    conn: sqlite3.Connection,
    room: str,
    *,
    since: str | None = None,
    until: str | None = None,
) -> list[tuple[str, int]]:
    """`[(uid, messages)]`，按发言数降序；`since`/`until` 是闭区间的 `YYYY-MM-DD`。

    `until=None` 表示不限上界（「总榜」），`since=None` 只跳过下界——
    两个都为空就是全量总和。
    """
    sql = "SELECT uid, SUM(messages) AS n FROM activity_daily WHERE room = ?"
    params: list[object] = [room]
    if since:
        sql += " AND day >= ?"
        params.append(since)
    if until:
        sql += " AND day <= ?"
        params.append(until)
    sql += " GROUP BY uid HAVING n > 0 ORDER BY n DESC, uid ASC"
    return [(str(row[0]), int(row[1])) for row in conn.execute(sql, params)]


def last_seen(conn: sqlite3.Connection, room: str) -> dict[str, str]:
    """`{uid: 最后一次发言的 day}`，没有发言记录的成员不在结果里（表里没有 day 列）。"""
    rows = conn.execute(
        """
        SELECT s.uid, MAX(a.day) AS day
        FROM member_seen AS s
        LEFT JOIN activity_daily AS a ON a.room = s.room AND a.uid = s.uid
        WHERE s.room = ?
        GROUP BY s.uid
        """,
        (room,),
    )
    return {str(uid): str(day) for uid, day in rows if day}


def seen_names(conn: sqlite3.Connection, room: str) -> dict[str, str]:
    """`{uid: 昵称}`，来自 `member_seen`；供改名巡检比对与榜单显示兜底。"""
    rows = conn.execute(
        "SELECT uid, nickname FROM member_seen WHERE room = ? AND nickname <> ''",
        (room,),
    )
    return {str(uid): str(name) for uid, name in rows}


def record_nickname_change(
    conn: sqlite3.Connection,
    *,
    room: str,
    uid: str,
    old: str,
    new: str,
    now: int,
) -> None:
    """写一行 `nickname_log` 并把 `member_seen.nickname` 更新成新名字。"""
    conn.execute(
        "INSERT INTO nickname_log (room, uid, old_name, new_name, at) VALUES (?, ?, ?, ?, ?)",
        (room, uid, old, new, now),
    )
    conn.execute(
        "UPDATE member_seen SET nickname = ? WHERE room = ? AND uid = ?",
        (new, room, uid),
    )


def recent_names(conn: sqlite3.Connection, room: str, *, limit: int = 10) -> list[tuple[str, str, str]]:
    """最近 `limit` 条改名记录：`[(uid, old, new)]`，新的在前。"""
    rows = conn.execute(
        """
        SELECT uid, old_name, new_name FROM nickname_log
        WHERE room = ? ORDER BY id DESC LIMIT ?
        """,
        (room, max(1, int(limit))),
    )
    return [(str(r[0]), str(r[1]), str(r[2])) for r in rows]
