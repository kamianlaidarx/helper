"""排行榜引擎：金币榜（第 1 批）+ 活跃榜（第 4 批）。
契约（§3.4）：

- `Board = Literal["coin", "activity", "marriage", "invite"]`，
  `Period = Literal["day", "week", "month", "all"]`。
- `top(conn, *, board, room, period=None, now, limit=10)`：
  - `board="coin"` 时 `period` **必须**为 `None`，否则抛 `ValueError`；
  - `board="activity"` 时 `period` 必须是四档之一，分数 = `SUM(activity_daily.messages)`，
    区间由 `clock.range_of()` 算出（周一为周起点，禁止 SQL 里的 `date('now')`）；
  - 婚姻 / 邀请两榜仍抛 `NotImplementedError`；
  - 金币榜取**当前余额**（视图 `v_rank_coin`），钻石不混入；
    同分按 uid 升序稳定排序，名次从 1 连续编号。
- 入口侧（`RankingEngine.on_command`）先判可用性：带「婚姻 / 邀请」参数的请求
  **静默返回 None**，根本不进 `top()`——§7「公开路径静默，不回未开放」。

关于活跃榜与小屋/黑名单的关系：不进排行是靠**不记数**实现的——`app.py` 对被关进
小黑屋或被拉黑的成员不写 `activity_daily`，所以这里不需要（也不应该）再过滤一次；
历史计数照旧保留，否则「解除后历史清零」会比「暂时不上榜」更让人意外。

关于 `grep SELECT plugins/playhub/engines/` 应为空（§7 第 5 条）：
`core/rank.py` 不属本批交付文件清单，本批的 SQL 只能临时放在这里；
`core/rank.py` 落地后把 `top()` 整段搬过去、本文件改为薄封装即可（见报告「与文档的偏离」）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from ..core import activity, clock, render
from . import DEFAULT_LIMIT, Reply, msg_room, store_of

Board = Literal["coin", "activity", "marriage", "invite"]
Period = Literal["day", "week", "month", "all"]

BOARDS: tuple[Board, ...] = ("coin", "activity", "marriage", "invite")
PERIODS: tuple[Period, ...] = ("day", "week", "month", "all")

COIN_BOARD: Board = "coin"
ACTIVITY_BOARD: Board = "activity"
# 仍未实现的两榜：`top()` 抛 NotImplementedError，入口静默。
UNIMPLEMENTED_BOARDS: frozenset[str] = frozenset({"marriage", "invite"})

# 入口参数 → board；认不出的参数静默（不回「未开放」）。
ACCEPTED_ARGS: dict[str, Board] = {
    "金币": COIN_BOARD,
    "活跃": ACTIVITY_BOARD,
    "活跃榜": ACTIVITY_BOARD,
}

# 入口参数 → period（活跃榜专用）；不写就用默认周榜。
PERIOD_ARGS: dict[str, Period] = {
    "日": "day",
    "今日": "day",
    "今天": "day",
    "周": "week",
    "本周": "week",
    "这周": "week",
    "月": "month",
    "本月": "month",
    "这个月": "month",
    "总": "all",
    "总榜": "all",
    "全部": "all",
}

DEFAULT_PERIOD: Period = "week"

PERIOD_LABEL: dict[Period, str] = {"day": "今日", "week": "本周", "month": "本月", "all": "总"}

BOARD_EMPTY = "榜单还没有数据"
ACTIVITY_EMPTY = "还没有发言记录（自开始采集以来）"


@dataclass(frozen=True)
class RankRow:
    """一行榜单。`nickname` 取不到时为空串，展示层回退到 uid。"""

    room: str
    uid: str
    score: int
    rank: int
    nickname: str = ""


def top(
    conn,
    *,
    board: Board,
    room: str,
    period: Period | None = None,
    now: int,
    limit: int = DEFAULT_LIMIT,
) -> tuple[RankRow, ...]:
    """四榜的统一入口（本批只有 `board="coin"` 有实现）。

    `now` 本批不参与金币榜（金币榜是当前余额，与时间无关），
    但签名保留它，后续三榜（日/周/月）必须用它换算 `Asia/Shanghai` 的区间——
    §3.4 明确禁止在 SQL 里用 `date(now)`。
    """
    if board not in BOARDS:
        raise ValueError(f"未知榜单：{board!r}")
    if board == COIN_BOARD:
        if period is not None:
            raise ValueError("金币榜不接受 period，必须为 None（§3.4）")
    elif board == ACTIVITY_BOARD:
        if period is None:
            period = DEFAULT_PERIOD
        if period not in PERIODS:
            raise ValueError(f"未知榜期：{period!r}")
    elif board in UNIMPLEMENTED_BOARDS:
        raise NotImplementedError(f"榜单 {board} 属后续批次，本批未实现")
    else:  # pragma: no cover - BOARDS 已穷举
        raise NotImplementedError(f"榜单 {board} 未实现")

    size = int(limit)
    if size <= 0:
        return ()

    if board == ACTIVITY_BOARD:
        return _top_activity(conn, room=room, period=period or DEFAULT_PERIOD, now=now, size=size)

    rows = conn.execute(
        """
        SELECT r.uid, r.score, COALESCE(p.nickname, '') AS nickname
          FROM v_rank_coin AS r
          LEFT JOIN player AS p ON p.room = r.room AND p.uid = r.uid
         WHERE r.room = ?
         ORDER BY r.score DESC, r.uid ASC
         LIMIT ?
        """,
        (room, size),
    ).fetchall()
    return tuple(
        RankRow(room=room, uid=row[0], score=int(row[1]), rank=i, nickname=row[2] or "")
        for i, row in enumerate(rows, start=1)
    )


def _top_activity(conn, *, room: str, period: Period, now: int, size: int) -> tuple[RankRow, ...]:
    """活跃榜：分数 = 区间内 `SUM(activity_daily.messages)`，同分按 uid 升序。"""
    since, until = clock.range_of(period, now)
    scored = activity.scores(conn, room, since=since, until=until)[:size]
    if not scored:
        return ()
    names = activity.seen_names(conn, room)
    return tuple(
        RankRow(room=room, uid=uid, score=score, rank=i, nickname=names.get(uid, ""))
        for i, (uid, score) in enumerate(scored, start=1)
    )


_MEDALS = ("🥇", "🥈", "🥉")


def format_board(
    rows: tuple[RankRow, ...],
    *,
    icon: str = "🏆",
    title: str = "金币榜",
    unit: str = "金币",
    note_text: str = "取当前余额，花掉的金币不在榜上",
    empty_text: str = BOARD_EMPTY,
) -> str:
    """榜单卡：前三名挂奖牌，其余用 `·`；空榜回一行提示（`§4.5` 卡面规范）。"""
    if not rows:
        return render.tip("⚠", empty_text)
    lines = []
    for row in rows:
        icon_ = _MEDALS[row.rank - 1] if row.rank <= len(_MEDALS) else "·"
        lines.append(render.row(f"{icon_} ", f"{row.rank}. {row.nickname or row.uid} {row.score} {unit}"))
    return render.menu(icon, title, lines, note_text=note_text)


def format_activity(rows: tuple[RankRow, ...], period: Period) -> str:
    return format_board(
        rows,
        icon="🔥",
        title=f"活跃榜 · {PERIOD_LABEL[period]}",
        unit="条",
        note_text="按群消息条数统计，自开始采集以来；同一句只算一次",
        empty_text=ACTIVITY_EMPTY,
    )


class RankingEngine:
    """`ranking` 引擎：金币榜（`ranking.top`）+ 活跃榜（`ranking.activity`）。"""

    name = "ranking"

    def __init__(self, *, now_fn=None, logger=None):
        self._now = now_fn if now_fn is not None else clock.now_ts
        self._log = logger if logger is not None else logging.getLogger("playhub.engines.ranking")

    def on_command(self, cmd, msg=None, ctx=None, game=None) -> Reply | None:
        action = getattr(cmd, "action", "")
        if action not in ("ranking.top", "ranking.activity"):
            return None
        try:
            return self.show(cmd, msg, game)
        except Exception:                  # §2.5：异常不上抛，回一句固定文案
            self._log.exception("榜单查询失败：cmd=%s", getattr(cmd, "raw", ""))
            return Reply("这个玩法暂时出错了", action)

    def show(self, cmd, msg=None, game=None) -> Reply | None:
        room = msg_room(msg)
        if not room:
            return None
        action = getattr(cmd, "action", "ranking.top")
        board = self._board(cmd, action)
        if board is None:                  # 未实现的榜（婚姻/邀请）或看不懂的参数 → 静默
            return None
        store = store_of(game)
        period = self._period(cmd) if board == ACTIVITY_BOARD else None
        with store.tx(immediate=False) as conn:   # 只读，不需要 BEGIN IMMEDIATE
            rows = top(conn, board=board, room=room, period=period, now=self._now())
        if board == ACTIVITY_BOARD:
            return Reply(format_activity(rows, period or DEFAULT_PERIOD), action)
        return Reply(format_board(rows), action)

    def _board(self, cmd, action: str = "ranking.top") -> Board | None:
        """解析榜单：`活跃榜` 直指活跃榜；`排行榜` 裸发 → 金币榜、首个参数决定榜单。"""
        if action == "ranking.activity":
            return ACTIVITY_BOARD
        tokens = tuple(getattr(cmd, "tokens", ()) or ())
        if not tokens:
            return COIN_BOARD
        return ACCEPTED_ARGS.get(tokens[0])

    def _period(self, cmd) -> Period:
        """榜期取第一个能认出来的参数，认不出按默认周榜（不静默：用户已经指名活跃榜）。"""
        for token in tuple(getattr(cmd, "tokens", ()) or ()):
            period = PERIOD_ARGS.get(token)
            if period is not None:
                return period
        return DEFAULT_PERIOD
