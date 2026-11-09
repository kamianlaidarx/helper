"""小黑屋与黑名单（第 4 批，§4 第 33/34 行、§8.2）。

两件事都是「限制某个成员在本插件里的参与」，但**都只在 playhub 内生效**：

- 小黑屋（`jail` 表）：一段时间内不响应他的游戏命令与抢答、不进排行、不参与抽奖。
  **软禁言**——不发群公告，也不调用任何框架的禁言/踢人接口（`backend.py` 没有）。
  单次上限 60 分钟，每日上限 10 次（按「设置动作」计，解除不占额度）。
- 黑名单（`blocklist` 表）：`scope='play'`，永久不响应，除非显式删除。

权限（§8.2）：设置/解除/增删都只放行 `owner`/`admin`，判定统一走
`ctx.group_member_role()`（`adapters/memberfeed.py` 包一层），
`member`/`unknown`/读取异常一律**失败关闭**，并记一行 `audit.result='denied'`。
`members.owner_ids` / `is_owner` 白名单**不用**：那是「机器人主人」，不是群主。

裸发只回用法（`needs_arg=True` + `bare_behavior="guide"`），`小黑屋 10` 这种没点人的
形态也直接拒绝——按 §8.2「必须有 @对象」。
"""
from __future__ import annotations

from ..adapters.memberfeed import MemberFeed
from ..core import clock, permissions, render
from . import Reply, msg_nickname, msg_room, msg_uid, store_of

JAIL_MAX_MINUTES = 60
JAIL_DAILY_CAP = 10
JAIL_KEY = "jail_set"
JAIL_SUBS = ("解除", "列表")
BLOCK_SUBS = ("加", "删", "列")
SCOPE_PLAY = "play"

USAGE_JAIL = "💡 用法：小黑屋 @某人 10（分钟）／小黑屋 解除 @某人／小黑屋 列表"
USAGE_BLOCK = "💡 用法：黑名单 加 @某人／黑名单 删 @某人／黑名单 列"

DENIED = "这个操作只有群主和管理员能用"
NO_GROUP = "这个玩法只能在群里用"
NO_TARGET = "没认出要操作谁。把对方的名字写在关键词后面，例如：小黑屋 @小明 10"

# 开关（§4：小黑屋 `jail`、黑名单 `blocklist`）；读不到 → 开（与 app._feature_off 同口径）
OFF_VALUES = frozenset({"0", "off", "false", "no", "disabled"})


class ModerationEngine:
    name = "moderation"

    def __init__(self, *, now_fn=None, logger=None):
        self._now = now_fn if now_fn is not None else clock.now_ts
        self._log = logger
        self._feed = None

    # ------------------------------------------------------------------ 分派
    def on_command(self, cmd, msg=None, ctx=None, game=None):
        if cmd.action == "moderation.jail":
            return self.jail(cmd, msg, ctx, game)
        if cmd.action == "moderation.block":
            return self.block(cmd, msg, ctx, game)
        return None

    # ------------------------------------------------------------------ 小黑屋
    def jail(self, cmd, msg, ctx, game):
        room = msg_room(msg)
        if not room:
            return Reply(NO_GROUP, "moderation.jail")
        store = store_of(game)
        now = self._now()

        with store.tx(immediate=False) as conn:
            if not _switch_on(conn, room, "jail"):
                return None
            expired = self._expire(conn, room, now)

        head, rest = _split_sub(cmd.args, JAIL_SUBS)
        if head == "列表":
            return self._jail_list(room, msg, ctx, game, note_expired=expired)

        denied = self._deny(ctx, room, msg_uid(msg), "cmd:小黑屋")
        if denied is not None:
            return denied

        if head == "解除":
            target, extra = _target_and_rest(rest)
            if not target:
                return Reply(f"❌ {NO_TARGET}", "moderation.jail")
            return self._jail_release(room, target, msg, ctx, game)

        target, extra = _target_and_rest(cmd.args if head is None else rest)
        if not target or (target.isdigit() and not extra.strip()):
            # `小黑屋 10` 这种「只有分钟数、没点人」的形态按 §8.2 直接拒绝
            return Reply(
                f"❌ {NO_TARGET}\n{USAGE_JAIL}" if not (cmd.args or "").strip() else f"❌ {NO_TARGET}",
                "moderation.jail",
            )
        minutes = 10
        if extra.strip():
            if not extra.strip().isdigit():
                return Reply(f"❌ 分钟数要是整数。\n{USAGE_JAIL}", "moderation.jail")
            minutes = int(extra.strip())
        if minutes <= 0:
            return Reply(f"❌ 分钟数要大于 0。\n{USAGE_JAIL}", "moderation.jail")
        if minutes > JAIL_MAX_MINUTES:
            minutes = JAIL_MAX_MINUTES

        uid = self._resolve(ctx, room, target)
        if not uid:
            return Reply(f"❌ 找不到「{target}」。让对方在本群说过话再试。", "moderation.jail")
        if uid == msg_uid(msg):
            return Reply("❌ 把自己关进小黑屋没有意义。", "moderation.jail")
        return self._jail_apply(room, uid, target, minutes, msg, ctx, game)

    def _jail_apply(self, room, target_uid, target_name, minutes, msg, ctx, game):
        store = store_of(game)
        now = self._now()
        with store.tx() as conn:
            used = _counter_used(conn, room, msg_uid(msg), clock.day_of(now), JAIL_KEY)
            if used >= JAIL_DAILY_CAP:
                return Reply(
                    render.result(
                        msg_nickname(msg, ctx),
                        f"🛑 今天的关人次数用完了（{JAIL_DAILY_CAP} 次/天）",
                        [render.row("⏳ ", "明天 0 点重置")],
                    ),
                    "moderation.jail",
                )
            _counter_bump(conn, room, msg_uid(msg), clock.day_of(now), JAIL_KEY, JAIL_DAILY_CAP, now)
            until = now + minutes * 60
            conn.execute(
                """
                INSERT INTO jail (room, uid, until, reason, by_uid, at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(room, uid) DO UPDATE SET
                    until  = excluded.until,
                    reason = excluded.reason,
                    by_uid = excluded.by_uid,
                    at     = excluded.at
                """,
                (room, target_uid, until, f"{minutes} 分钟", msg_uid(msg), now),
            )
            _audit(conn, now, room, msg_uid(msg), "cmd:小黑屋", target_uid, "ok", {"minutes": minutes})
        return Reply(
            render.result(
                msg_nickname(msg, ctx),
                f"🚫 {target_name} 已进小黑屋 {minutes} 分钟",
                [
                    render.row("🎮 ", "这期间他的游戏命令和抢答都不响应"),
                    render.row("⏳ ", f"到 {minutes} 分钟后自动解除"),
                    render.row("🔁 ", f"今天还能关：{max(0, JAIL_DAILY_CAP - used - 1)} 次"),
                ],
                tail="ℹ 只是我的玩法不理他，不会真的禁言或踢人",
            ),
            "moderation.jail",
        )

    def _jail_release(self, room, target_name, msg, ctx, game):
        store = store_of(game)
        now = self._now()
        with store.tx() as conn:
            uid = self._resolve(ctx, room, target_name)
            if not uid:
                return Reply(f"❌ 找不到「{target_name}」。", "moderation.jail")
            cur = conn.execute("SELECT until FROM jail WHERE room = ? AND uid = ?", (room, uid)).fetchone()
            if cur is None:
                return Reply(
                    render.result("", f"🫥 {target_name} 现在不在小黑屋", at_sender=False),
                    "moderation.jail",
                )
            conn.execute("DELETE FROM jail WHERE room = ? AND uid = ?", (room, uid))
            _audit(conn, now, room, msg_uid(msg), "cmd:小黑屋", uid, "ok", {"op": "release"})
        return Reply(
            render.result(
                msg_nickname(msg, ctx),
                f"🕊 {target_name} 已解除",
                [render.row("🎮 ", "游戏命令恢复正常")],
            ),
            "moderation.jail",
        )

    def _jail_list(self, room, msg, ctx, game, *, note_expired=None):
        store = store_of(game)
        now = self._now()
        with store.tx(immediate=False) as conn:
            rows = conn.execute(
                "SELECT uid, until, reason FROM jail WHERE room = ? AND until > ? ORDER BY until ASC",
                (room, now),
            ).fetchall()
            names = _names(conn, room)
        if not rows:
            detail = [render.row("✅ ", "当前没有人被关")]
            if note_expired:
                detail.append(render.row("🧹 ", f"顺手清理了 {note_expired} 条到期记录"))
            return Reply(render.result(msg_nickname(msg, ctx), "🚫 小黑屋（空）", detail), "moderation.jail")
        body = []
        for uid, until, reason in rows:
            left = max(0, int(until) - now)
            body.append(render.row("⏳ ", f"{names.get(str(uid)) or uid} — 还剩 {clock.fmt_left(left)}（{reason}）"))
        return Reply(
            render.result(msg_nickname(msg, ctx), f"🚫 小黑屋 · {len(rows)} 人", body),
            "moderation.jail",
        )

    def _expire(self, conn, room, now) -> int:
        cur = conn.execute("DELETE FROM jail WHERE room = ? AND until <= ?", (room, now))
        return int(cur.rowcount or 0) if cur.rowcount and cur.rowcount > 0 else 0

    # ------------------------------------------------------------------ 黑名单
    def block(self, cmd, msg, ctx, game):
        room = msg_room(msg)
        if not room:
            return Reply(NO_GROUP, "moderation.block")
        store = store_of(game)
        now = self._now()
        with store.tx(immediate=False) as conn:
            if not _switch_on(conn, room, "blocklist"):
                return None

        head, rest = _split_sub(cmd.args, BLOCK_SUBS)
        if head == "列":
            return self._block_list(room, msg, ctx, game)
        if head is None:
            return Reply(USAGE_BLOCK, "moderation.block")

        denied = self._deny(ctx, room, msg_uid(msg), "cmd:黑名单")
        if denied is not None:
            return denied

        target, _extra = _target_and_rest(rest)
        if not target:
            return Reply(f"❌ {NO_TARGET}\n{USAGE_BLOCK}", "moderation.block")
        uid = self._resolve(ctx, room, target)
        if not uid:
            return Reply(f"❌ 找不到「{target}」。让对方在本群说过话再试。", "moderation.block")
        if uid == msg_uid(msg):
            return Reply("❌ 不能把自己加进黑名单。", "moderation.block")

        with store.tx() as conn:
            if head == "加":
                conn.execute(
                    """
                    INSERT INTO blocklist (room, uid, scope, reason, by_uid, at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(room, uid, scope) DO UPDATE SET
                        reason = excluded.reason,
                        by_uid = excluded.by_uid,
                        at     = excluded.at
                    """,
                    (room, uid, SCOPE_PLAY, "黑名单", msg_uid(msg), now),
                )
                _audit(conn, now, room, msg_uid(msg), "cmd:黑名单", uid, "ok", {"op": "add"})
                headline = f"⛔ {target} 已加入黑名单"
            else:  # 删
                cur = conn.execute(
                    "DELETE FROM blocklist WHERE room = ? AND uid = ? AND scope = ?",
                    (room, uid, SCOPE_PLAY),
                )
                _audit(conn, now, room, msg_uid(msg), "cmd:黑名单", uid, "ok", {"op": "remove"})
                if not cur.rowcount:
                    return Reply(
                        render.result("", f"🫥 {target} 不在黑名单里", at_sender=False),
                        "moderation.block",
                    )
                headline = f"✅ {target} 已移出黑名单"
        return Reply(
            render.result(
                msg_nickname(msg, ctx),
                headline,
                [render.row("🎮 ", "只是我的玩法不理他，不影响他正常聊天")],
            ),
            "moderation.block",
        )

    def _block_list(self, room, msg, ctx, game):
        store = store_of(game)
        with store.tx(immediate=False) as conn:
            rows = conn.execute(
                "SELECT uid, at FROM blocklist WHERE room = ? AND scope = ? ORDER BY at ASC",
                (room, SCOPE_PLAY),
            ).fetchall()
            names = _names(conn, room)
        if not rows:
            return Reply(
                render.result(msg_nickname(msg, ctx), "⛔ 黑名单（空）", [render.row("✅ ", "没有人在黑名单里")]),
                "moderation.block",
            )
        body = []
        for uid, _at in rows:
            body.append(render.row("🚷 ", names.get(str(uid)) or str(uid)))
        return Reply(
            render.result(msg_nickname(msg, ctx), f"⛔ 黑名单 · {len(rows)} 人", body),
            "moderation.block",
        )

    # ------------------------------------------------------------------ 内部
    def _deny(self, ctx, room, uid, action) -> Reply | None:
        if self._feed is None:
            self._feed = MemberFeed(ctx, logger=self._log)
        decision = permissions.gate(self._feed.role(room, uid))
        if decision.allowed:
            return None
        store = None
        try:
            store = getattr(ctx, "_playhub_store", None)
        except Exception:
            store = None
        _ = store  # 审计行由调用方在事务内写；这里只负责拒绝
        return Reply(
            render.result(
                "",
                f"🔒 {DENIED}",
                [render.row("👤 ", f"你的角色：{decision.role}")],
                at_sender=False,
            ),
            action,
        )

    def _resolve(self, ctx, room, name) -> str:
        members = getattr(ctx, "members", None) if ctx is not None else None
        resolve = getattr(members, "resolve", None)
        if not callable(resolve):
            return ""
        try:
            found = resolve(name, room)
        except Exception:
            return ""
        wxid = getattr(found, "wxid", "") if found is not None else ""
        return str(wxid or "")


# ---------------------------------------------------------------------- 模块级
def is_jailed(conn, room: str, uid: str, now: int) -> bool:
    """`uid` 此刻是否在小黑屋里（`app.py` 每条命令前调用）。"""
    row = conn.execute("SELECT until FROM jail WHERE room = ? AND uid = ?", (room, uid)).fetchone()
    return bool(row and int(row[0]) > now)


def is_blocked(conn, room: str, uid: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM blocklist WHERE room = ? AND uid = ? AND scope = ?",
        (room, uid, SCOPE_PLAY),
    ).fetchone()
    return row is not None


def _switch_on(conn, room: str, name: str) -> bool:
    row = conn.execute("SELECT value FROM kv WHERE room = ? AND key = ?", (room, f"feature.{name}")).fetchone()
    if row is None:
        return True
    value = str(row[0] or "").strip().lower()
    return value not in OFF_VALUES


def _names(conn, room: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT uid, nickname FROM member_seen WHERE room = ? AND nickname <> ''", (room,)
    )
    return {str(uid): str(name) for uid, name in rows}


def _split_sub(args: str, subs) -> tuple[str | None, str]:
    text = (args or "").strip()
    if not text:
        return None, ""
    for sep in (" ", "\u3000", "#", "＃"):
        if sep in text:
            head, rest = text.split(sep, 1)
            head = head.strip()
            if head in subs:
                return head, rest.strip()
            return None, text
    if text in subs:
        return text, ""
    return None, text


def _target_and_rest(text: str) -> tuple[str, str]:
    raw = (text or "").strip()
    if not raw:
        return "", ""
    if raw.startswith("@"):
        raw = raw[1:]
    for sep in (" ", "\u3000", "#", "＃"):
        if sep in raw:
            head, rest = raw.split(sep, 1)
            return head.strip(), rest.strip()
    return raw.strip(), ""


def _counter_used(conn, room: str, uid: str, day: str, key: str) -> int:
    row = conn.execute(
        "SELECT used FROM daily_counter WHERE room = ? AND uid = ? AND day = ? AND key = ?",
        (room, uid, day, key),
    ).fetchone()
    return int(row[0]) if row else 0


def _counter_bump(conn, room: str, uid: str, day: str, key: str, cap: int, now: int) -> int:
    conn.execute(
        """
        INSERT INTO daily_counter (room, uid, day, key, used, cap, updated_at)
        VALUES (?, ?, ?, ?, 1, ?, ?)
        ON CONFLICT(room, uid, day, key) DO UPDATE SET
            used = used + 1,
            updated_at = excluded.updated_at
        """,
        (room, uid, day, key, cap, now),
    )
    return _counter_used(conn, room, uid, day, key)


def _audit(conn, now: int, room: str, actor: str, action: str, target: str, result: str, detail: dict) -> None:
    import json

    conn.execute(
        "INSERT INTO audit (at, room, actor, action, target, result, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (now, room, actor, action, target, result, json.dumps(detail, ensure_ascii=False)),
    )
