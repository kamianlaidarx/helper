"""群务统计：潜水榜、邀请榜、改名巡检（第 4 批，§4 第 41/42/45 行）。

三件事分别是三种「读法」，所以放在一个引擎里而不是拆三个：

- `潜水 7`：读 `activity_daily` + `member_seen`，算「多少天没说话」。**只统计不清人**
  ——本插件没有踢人能力（`backend.py` 的群管理接口不支持），别在卡面上暗示会清理。
- `邀请 7` / `邀请榜 7`：读 `invite_credit`（按人）与 `invite_daily`（按群）。
  框架的入群事件拿不到邀请人时（`group_events.py` 的 `GroupMemberEvent` 只有
  `kind/room_id/member_id/nickname/source`），`invite_credit` 是空的，
  此时只报群级数字并明说「按人统计需要框架带邀请人」。
- `改名 开/关/现在`：**群管理开关**，`开`/`关`/`现在` 三个子命令都要 owner/admin
  门禁（§8.2「权限判定统一走 `ctx.group_member_role()`，失败关闭」）；
  `现在` 拿 `ctx.group_members()` 快照与 `member_seen` 比对，写 `nickname_log`。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from ..adapters.memberfeed import MemberFeed
from ..core import activity, clock, kv, permissions, render
from . import Reply, msg_nickname, msg_room, msg_uid, store_of

DIVE_DAYS_DEFAULT = 7
DIVE_DAYS_MAX = 90
INVITE_DAYS_DEFAULT = 7
INVITE_DAYS_MAX = 90
LIST_LIMIT = 10

RENAME_KEY = "groupstat.rename_watch"
#: 上次巡检看到的「群昵称」基线。必须与 `member_seen.nickname` 分开：后者会被
#: 发言时的微信名覆盖，拿它当基线会把「换过群昵称的人」反复误报成刚改名。
RENAME_SNAPSHOT_KEY = "groupstat.rename_snapshot"
RENAME_SUBS = ("开", "关", "现在")

DENIED = "这个操作只有群主和管理员能用"
NO_GROUP = "这个玩法只能在群里用"

USAGE_DIVE = "💡 用法：潜水 天数（例：潜水 7）"
USAGE_INVITE = "💡 用法：邀请 天数（例：邀请 7，看法是「邀请榜」）"
USAGE_RENAME = "💡 用法：改名 开 / 改名 关 / 改名 现在"


@dataclass(frozen=True)
class Idle:
    uid: str
    name: str
    days: int


def _parse_days(text: str, default: int, cap: int) -> tuple[int, str]:
    """`(天数, 错误文案)`；`text` 为空用默认值，非法给错误。"""
    raw = (text or "").strip()
    if not raw:
        return default, ""
    if not raw.isdigit():
        return 0, "天数要是正整数"
    value = int(raw)
    if value <= 0:
        return 0, "天数要大于 0"
    return min(value, cap), ""


def _name_of(names: dict[str, str], uid: str) -> str:
    return names.get(uid) or uid


# ---------------------------------------------------------------------------
# 改名巡检的基线（前台「改名 现在」与后台定时动作共用，`§4.2`）
# ---------------------------------------------------------------------------
def watch_on(conn, room: str) -> bool:
    """改名播报开关；读不到=开（`§4.2`：默认开着，`改名 关` 才停）。"""
    return kv.get(conn, room, RENAME_KEY) != "0"


def snapshot_of(rows) -> dict[str, str]:
    """`[(uid, 名字)]` → 基线 `{uid: 名字}`；没名字的成员不写进基线。"""
    return {uid: name for uid, name in rows if uid and name}


def member_rows(ctx, room) -> list[tuple[str, str]] | None:
    """`ctx.group_members(room)` → `[(uid, 昵称)]`；读不到快照返回 None。

    后台改名巡检（`scheduler.run_rename`）与前台「改名 现在」共用这一份：
    两处各写一遍，迟早会在「dict 用 wxid 还是 uid」上分叉。
    """
    getter = getattr(ctx, "group_members", None) if ctx is not None else None
    if not callable(getter):
        return None
    try:
        raw = getter(room)
    except Exception:
        return None
    if raw is None:                     # 框架缓存未就绪（sdk：`group_members` 返回 None）
        return None
    rows: list[tuple[str, str]] = []
    for row in raw or ():
        if isinstance(row, dict):
            uid = str(row.get("wxid") or row.get("uid") or "")
            name = str(row.get("nickname") or row.get("wechat_name") or "")
        else:
            uid = str(getattr(row, "wxid", "") or "")
            name = str(getattr(row, "nickname", "") or "")
        if uid and name:
            rows.append((uid, name))
    return rows


def diff_names(prev, rows) -> list[tuple[str, str, str]]:
    """与基线比对，返回 `[(uid, 旧名, 新名)]`。

    只报「两边都非空且不同」：新人只进基线不播报，读不到名字的成员也不误报。
    """
    changes: list[tuple[str, str, str]] = []
    for uid, name in rows:
        old = (prev or {}).get(uid, "")
        if old and name and old != name:
            changes.append((uid, old, name))
    return changes


class GroupStatEngine:
    name = "groupstat"

    def __init__(self, *, now_fn=None, logger=None):
        self._now = now_fn if now_fn is not None else clock.now_ts
        self._log = logger
        self._feed = None

    # ------------------------------------------------------------------ 分派
    def on_command(self, cmd, msg=None, ctx=None, game=None):
        if cmd.action == "groupstat.dive":
            return self.dive(cmd, msg, ctx, game)
        if cmd.action == "groupstat.invite":
            return self.invite(cmd, msg, ctx, game)
        if cmd.action == "groupstat.rename":
            return self.rename(cmd, msg, ctx, game)
        return None

    # ------------------------------------------------------------------ 潜水
    def dive(self, cmd, msg, ctx, game):
        room = msg_room(msg)
        if not room:
            return Reply(NO_GROUP, "groupstat.dive")
        days, err = _parse_days(cmd.args, DIVE_DAYS_DEFAULT, DIVE_DAYS_MAX)
        if err:
            return Reply(f"❌ {err}。\n{USAGE_DIVE}", "groupstat.dive")

        now = self._now()
        today = clock.day_of(now)
        store = store_of(game)
        with store.tx(immediate=False) as conn:
            seen = activity.last_seen(conn, room)
            names = activity.seen_names(conn, room)

        if not seen:
            return Reply(
                render.tip("⚠", "这个群还没有任何发言记录，潜水榜要到明天才有数据"),
                "groupstat.dive",
            )

        idle: list[Idle] = []
        for uid, last in seen.items():
            gap = _days_between(last, today)
            if gap >= days:
                idle.append(Idle(uid=uid, name=_name_of(names, uid), days=gap))
        idle.sort(key=lambda item: (-item.days, item.name))

        if not idle:
            return Reply(
                render.result(
                    msg_nickname(msg, ctx),
                    f"🫧 {days} 天以上没说话的：一个都没有",
                    [render.row("👥 ", f"在场成员共 {len(seen)} 人")],
                    tail="📊 只统计，不清理任何人",
                ),
                "groupstat.dive",
            )

        rows: list[str] = [render.head("🫧", f"{days} 天以上没说话")]
        for item in idle[:LIST_LIMIT]:
            rows.append(render.row("🚿 ", f"{item.name} — {item.days} 天"))
        if len(idle) > LIST_LIMIT:
            rows.append(render.row("…  ", f"还有 {len(idle) - LIST_LIMIT} 人没列出来"))
        return Reply(
            render.result(
                msg_nickname(msg, ctx),
                f"🫧 潜水 {len(idle)} 人",
                rows,
                tail="📊 只统计，不清理任何人；天数按最后一次发言算",
            ),
            "groupstat.dive",
        )

    # ------------------------------------------------------------------ 邀请
    def invite(self, cmd, msg, ctx, game):
        room = msg_room(msg)
        if not room:
            return Reply(NO_GROUP, "groupstat.invite")
        days, err = _parse_days(cmd.args, INVITE_DAYS_DEFAULT, INVITE_DAYS_MAX)
        if err:
            return Reply(f"❌ {err}。\n{USAGE_INVITE}", "groupstat.invite")

        now = self._now()
        since = clock.day_of(now - days * 86400)
        until = clock.day_of(now)
        store = store_of(game)
        with store.tx(immediate=False) as conn:
            credited = conn.execute(
                """
                SELECT inviter, COUNT(*) AS n
                FROM invite_credit
                WHERE room = ? AND day >= ? AND day <= ?
                GROUP BY inviter ORDER BY n DESC, inviter ASC
                """,
                (room, since, until),
            ).fetchall()
            group_row = conn.execute(
                "SELECT COALESCE(SUM(invite_joins), 0), COALESCE(SUM(joins), 0) "
                "FROM invite_daily WHERE room = ? AND day >= ? AND day <= ?",
                (room, since, until),
            ).fetchone()
            names = activity.seen_names(conn, room)

        credited_rows = [(str(uid), int(n)) for uid, n in credited]
        invite_joins = int(group_row[0]) if group_row else 0
        joins = int(group_row[1]) if group_row else 0

        if not credited_rows and not invite_joins:
            return Reply(
                render.result(
                    msg_nickname(msg, ctx),
                    f"📨 近 {days} 天没有入群记录",
                    [render.row("🚪 ", "群里没人被邀请进来，或者框架没记到入群事件")],
                    tail="ℹ 邀请榜靠框架的入群事件，缺事件时这里就是空的",
                ),
                "groupstat.invite",
            )

        rows: list[str] = []
        if credited_rows:
            for index, (uid, count) in enumerate(credited_rows[:LIST_LIMIT], start=1):
                rows.append(render.row("👤 ", f"{index}. {_name_of(names, uid)} — {count} 人"))
        else:
            rows.append(
                render.row(
                    "🫥 ",
                    f"按人统计暂缺：框架的入群事件没带邀请人（群级共 {invite_joins} 次邀请入群）",
                )
            )
        rows.append(render.row("🚪 ", f"入群 {joins} 次 · 邀请入群 {invite_joins} 次"))
        return Reply(
            render.result(
                msg_nickname(msg, ctx),
                f"📨 邀请榜 · 近 {days} 天",
                rows,
                tail=f"📅 {since} ~ {until}｜同一个人被同一个人拉进来只算 1 次；缺邀请人时只报群级",
            ),
            "groupstat.invite",
        )

    # ------------------------------------------------------------------ 改名
    def rename(self, cmd, msg, ctx, game):
        room = msg_room(msg)
        uid = msg_uid(msg)
        if not room:
            return Reply(NO_GROUP, "groupstat.rename")

        sub = (cmd.args or "").strip()
        if not sub:
            return Reply(USAGE_RENAME, "groupstat.rename")
        if sub not in RENAME_SUBS:
            return Reply(f"❌ 只认「开」「关」「现在」。\n{USAGE_RENAME}", "groupstat.rename")

        denied = self._deny(ctx, room, uid, "cmd:改名")
        if denied is not None:
            return denied

        if sub == "现在":
            return self._scan_now(room, msg, ctx, game)

        store = store_of(game)
        now = self._now()
        with store.tx() as conn:
            kv.put(conn, room, RENAME_KEY, "1" if sub == "开" else "0", now)
        if sub == "开":
            body = render.result(
                msg_nickname(msg, ctx),
                "🔍 改名播报已开启",
                [render.row("📅 ", "每天巡检一次，谁改了群昵称就播一条")],
                tail="发「改名 现在」立刻扫一次；发「改名 关」停止播报",
            )
        else:
            body = render.result(
                msg_nickname(msg, ctx),
                "🔇 改名播报已关闭",
                [render.row("📅 ", "巡检停了，但历史昵称记录还留着")],
                tail="要重新开启发「改名 开」",
            )
        return Reply(body, "groupstat.rename")

    def _scan_now(self, room, msg, ctx, game):
        rows = self._member_rows(ctx, room)
        if rows is None:
            return Reply(
                render.tip("⚠", "暂时读不到群成员名单，过一会儿再发一次"),
                "groupstat.rename",
            )
        store = store_of(game)
        now = self._now()
        with store.tx() as conn:
            # 基线用后台巡检写的同一份快照，两边谁先扫都不会把同一处改名报两遍。
            prev = kv.get_json(conn, room, RENAME_SNAPSHOT_KEY) or {}
            changes = diff_names(prev, rows)
            for uid, old, new in changes:
                activity.record_nickname_change(
                    conn, room=room, uid=uid, old=old, new=new, now=now
                )
            kv.set_json(conn, room, RENAME_SNAPSHOT_KEY, snapshot_of(rows), now)
            for uid, name in rows:
                activity.record(conn, room=room, uid=uid, day=clock.day_of(now), now=now, nickname=name)

        if not changes:
            return Reply(
                render.result(
                    msg_nickname(msg, ctx),
                    "🔍 扫完了，没有改名",
                    [render.row("👥 ", f"核对 {len(rows)} 人")],
                    tail="下次巡检会自动比对",
                ),
                "groupstat.rename",
            )
        body: list[str] = [render.head("🔍", f"发现 {len(changes)} 处改名")]
        for _uid, old, new in changes[:LIST_LIMIT]:
            body.append(render.row("✏️ ", f"{old} → {new}"))
        return Reply(
            render.result(msg_nickname(msg, ctx), "🔍 改名播报", body),
            "groupstat.rename",
        )

    # ------------------------------------------------------------------ 内部
    def _member_rows(self, ctx, room) -> list[tuple[str, str]] | None:
        """`[(uid, 昵称)]`；读不到快照返回 None（后台巡检共用 `member_rows`）。"""
        return member_rows(ctx, room)

    def _deny(self, ctx, room, uid, action) -> Reply | None:
        if self._feed is None:
            self._feed = MemberFeed(ctx, logger=self._log)
        decision = permissions.gate(self._feed.role(room, uid))
        if decision.allowed:
            return None
        return Reply(
            render.result("", f"🔒 {DENIED}", [render.row("👤 ", f"你的角色：{decision.role}")], at_sender=False),
            action,
        )


def _days_between(day: str, today: str) -> int:
    """`today - day` 的天数；解析失败按 0（宁可不把脏数据算成潜水）。"""
    try:
        return (date.fromisoformat(today) - date.fromisoformat(day)).days
    except ValueError:
        return 0
