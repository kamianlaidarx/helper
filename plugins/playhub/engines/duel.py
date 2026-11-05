"""`duel` 引擎：切磋（第 3 批）。

规则（§4.4 没给数值，本文件按「不造币」的落地口径）：

- `切磋 @对象` 发起挑战，10 分钟内有效（`DUEL_TTL`），双方都不得已有待应战；
- 对方发 `切磋 同意` 即开打，胜方由注入的 `rng` 决定（可测试），**不扣金币也不发金币**
  ——§4.4 的金币产出表里只有签到/抽签/开宝箱/竞猜，切磋不在其列；
- `切磋 拒绝` 结束挑战；超时的挑战在下一次任何切磋命令时惰性清理；
- 战绩落 `kv`（`duel:wins:<uid>` / `duel:losses:<uid>`），不发流水。
"""
from __future__ import annotations

import random

from ..core import clock, kv, render
from . import Reply, msg_nickname, msg_room, msg_uid, store_of

DUEL_TTL = 10 * 60
SUB_ACCEPT = "同意"
SUB_REJECT = "拒绝"
USAGE = "💡 用法：切磋 @对象（发起）/ 切磋 同意 / 切磋 拒绝"
NO_PENDING = "眼下没有等你应战的切磋"
SELF_TARGET = "自己跟自己切磋练不出东西"


def _pair_key(low: str, high: str) -> str:
    return f"duel:{low}:{high}"


def _display(conn, room: str, uid: str) -> str:
    """取昵称用于卡面；没建档就用 uid，别让卡面出现空名字。"""
    row = conn.execute(
        "SELECT nickname FROM player WHERE room = ? AND uid = ?", (room, uid)
    ).fetchone()
    name = str(row[0]) if row and row[0] else ""
    return name or uid


def _wins_key(uid: str) -> str:
    return f"duel:wins:{uid}"


def _losses_key(uid: str) -> str:
    return f"duel:losses:{uid}"


class DuelEngine:
    """切磋。`name` 与 `keywords.Trigger.engine` 逐字一致。"""

    name = "duel"

    def __init__(self, *, rng=None, now_fn=None):
        self._rng = rng if rng is not None else random.Random()
        self._now = now_fn if now_fn is not None else clock.now_ts

    def on_command(self, cmd, msg=None, ctx=None, game=None) -> Reply | None:
        if getattr(cmd, "action", "") != "duel.challenge":
            return None
        return self.duel(cmd, msg, ctx, game)

    def duel(self, cmd, msg=None, ctx=None, game=None) -> Reply | None:
        room, uid = msg_room(msg), msg_uid(msg)
        store = store_of(game)
        if not room or not uid or store is None:
            return None
        now = self._now()
        nickname = msg_nickname(msg, ctx)
        args = (getattr(cmd, "args", "") or "").strip()

        if args.startswith(SUB_ACCEPT):
            return self._resolve(store, room, uid, now, accept=True)
        if args.startswith(SUB_REJECT):
            return self._resolve(store, room, uid, now, accept=False)
        if not args:
            return Reply(USAGE, "duel.challenge")

        target, target_name = self._target(ctx, room, uid, args)
        if target is None:
            return Reply(
                render.result(nickname, "❓ 没认出要切磋的人", (), tail=USAGE),
                "duel.challenge",
            )
        if target == uid:
            return Reply(render.result(nickname, f"❌ {SELF_TARGET}", ()), "duel.challenge")

        low, high = sorted((uid, target))
        key = _pair_key(low, high)
        with store.tx() as conn:
            pending = kv.get_json(conn, room, key)
            if pending and int(pending.get("deadline", 0)) > now:
                who = pending.get("from")
                tip = "等你应战" if who != uid else "你已经在等对方应战"
                return Reply(
                    render.result(nickname, "⚠ 这场切磋还没结束", [render.row("├ ", tip)], tail=USAGE),
                    "duel.challenge",
                )
            kv.set_json(
                conn, room, key,
                {"from": uid, "to": target, "at": now, "deadline": now + DUEL_TTL},
                now,
            )
        return Reply(
            render.result(
                nickname,
                "🥊 切磋挑战已发出",
                [
                    render.row("├ ", f"对手：{target_name}"),
                    render.row("├ ", f"等对方发「切磋 {SUB_ACCEPT}」就开打"),
                    render.row("⏳ ", "10 分钟内有效"),
                ],
                tail="💡 切磋不押金币，只记战绩",
            ),
            "duel.challenge",
        )

    def _target(self, ctx, room: str, uid: str, args: str) -> tuple[str | None, str]:
        name = args.lstrip("@＠ ").strip()
        for sep in (" ", "　", "#", "＃"):
            name = name.split(sep)[0]
        if not name:
            return None, ""
        members = getattr(ctx, "members", None)
        if members is None:
            return None, name
        try:
            return members.resolve(name, room).wxid, name
        except Exception:
            return None, name

    def _resolve(self, store, room: str, uid: str, now: int, *, accept: bool) -> Reply | None:
        found: tuple[str, dict] | None = None
        with store.tx() as conn:
            for (key,) in conn.execute(
                "SELECT key FROM kv WHERE room = ? AND key LIKE 'duel:%'", (room,)
            ):
                if not isinstance(key, str) or key.startswith("duel:wins:") or key.startswith("duel:losses:"):
                    continue
                data = kv.get_json(conn, room, key)
                if data and data.get("to") == uid:
                    found = (key, data)
                    break
            if found is None:
                return Reply(render.result("", NO_PENDING, (), at_sender=False), "duel.challenge")

            key, data = found
            if int(data.get("deadline", 0)) <= now:
                kv.clear(conn, room, key)
                return Reply(
                    render.result("", "⌛ 这场挑战已经过期", (), at_sender=False),
                    "duel.challenge",
                )
            if not accept:
                kv.clear(conn, room, key)
                return Reply(
                    render.result("", "🕊 已拒绝这场切磋", (), at_sender=False),
                    "duel.challenge",
                )

            challenger = str(data.get("from") or "")
            winner = challenger if self._rng.random() < 0.5 else uid
            loser = uid if winner == challenger else challenger
            wins = kv.bump(conn, room, _wins_key(winner), now)
            losses = kv.bump(conn, room, _losses_key(loser), now)
            winner_name = _display(conn, room, winner)
            loser_name = _display(conn, room, loser)
            kv.clear(conn, room, key)

        return Reply(
            render.result(
                "",
                "🥊 切磋结束",
                [
                    render.row("🏆 ", f"胜方：{winner_name}（{wins} 胜）"),
                    render.row("🩹 ", f"败方：{loser_name}（{losses} 负）"),
                ],
                tail="💡 切磋不发金币，只记战绩",
                at_sender=False,
            ),
            "duel.challenge",
        )
