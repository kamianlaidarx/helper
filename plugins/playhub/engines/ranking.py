"""排行榜引擎：本批只实现金币榜（`docs/design/playhub.md:1133-1157`、`§7 第 1 批` 第 3 条）。

契约（§3.4）：

- `Board = Literal["coin", "activity", "marriage", "invite"]`，
  `Period = Literal["day", "week", "month", "all"]`。
- `top(conn, *, board, room, period=None, now, limit=10)`：
  - `board="coin"` 时 `period` **必须**为 `None`，否则抛 `ValueError`；
  - 其余三榜本批未实现，抛 `NotImplementedError`；
  - 金币榜取**当前余额**（视图 `v_rank_coin`），钻石不混入；
    同分按 uid 升序稳定排序，名次从 1 连续编号。
- 入口侧（`RankingEngine.on_command`）先判可用性：带「活跃 / 婚姻 / 邀请」参数的请求
  **静默返回 None**，根本不进 `top()`——§7「公开路径静默，不回未开放」。

关于 `grep SELECT plugins/playhub/engines/` 应为空（§7 第 5 条）：
`core/rank.py` 不属本批交付文件清单，本批的 SQL 只能临时放在这里；
`core/rank.py` 落地后把 `top()` 整段搬过去、本文件改为薄封装即可（见报告「与文档的偏离」）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from ..core import clock, render
from . import DEFAULT_LIMIT, Reply, msg_room, store_of

Board = Literal["coin", "activity", "marriage", "invite"]
Period = Literal["day", "week", "month", "all"]

BOARDS: tuple[Board, ...] = ("coin", "activity", "marriage", "invite")
PERIODS: tuple[Period, ...] = ("day", "week", "month", "all")

COIN_BOARD: Board = "coin"
# 本批未实现的三榜：`top()` 抛 NotImplementedError，入口静默。
UNIMPLEMENTED_BOARDS: frozenset[str] = frozenset({"activity", "marriage", "invite"})

# 入口参数 → board；只有金币榜可用，其余走了也不进 top()。
ACCEPTED_ARGS: dict[str, Board] = {"金币": COIN_BOARD}

BOARD_EMPTY = "榜单还没有数据"


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
    elif board in UNIMPLEMENTED_BOARDS:
        raise NotImplementedError(f"榜单 {board} 属后续批次，本批未实现")
    else:  # pragma: no cover - BOARDS 已穷举
        raise NotImplementedError(f"榜单 {board} 未实现")

    size = int(limit)
    if size <= 0:
        return ()

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


_MEDALS = ("🥇", "🥈", "🥉")


def format_board(rows: tuple[RankRow, ...]) -> str:
    """金币榜卡：前三名挂奖牌，其余用 `·`；空榜回一行提示（`§4.5` 卡面规范）。"""
    if not rows:
        return render.tip("⚠", BOARD_EMPTY)
    lines = []
    for row in rows:
        icon = _MEDALS[row.rank - 1] if row.rank <= len(_MEDALS) else "·"
        lines.append(render.row(f"{icon} ", f"{row.rank}. {row.nickname or row.uid} {row.score} 金币"))
    return render.menu("🏆", "金币榜", lines, note_text="取当前余额，花掉的金币不在榜上")


class RankingEngine:
    """`ranking` 引擎：本批只接 `ranking.top`，且只接金币榜。"""

    name = "ranking"

    def __init__(self, *, now_fn=None, logger=None):
        self._now = now_fn if now_fn is not None else clock.now_ts
        self._log = logger if logger is not None else logging.getLogger("playhub.engines.ranking")

    def on_command(self, cmd, msg=None, ctx=None, game=None) -> Reply | None:
        if getattr(cmd, "action", "") != "ranking.top":
            return None
        try:
            return self.show(cmd, msg, game)
        except Exception:                  # §2.5：异常不上抛，回一句固定文案
            self._log.exception("金币榜查询失败：cmd=%s", getattr(cmd, "raw", ""))
            return Reply("这个玩法暂时出错了", "ranking.top")

    def show(self, cmd, msg=None, game=None) -> Reply | None:
        room = msg_room(msg)
        if not room:
            return None
        board = self._board(cmd)
        if board is None:                  # 未实现的榜（活跃/婚姻/邀请）或看不懂的参数 → 静默
            return None
        store = store_of(game)
        with store.tx(immediate=False) as conn:   # 只读，不需要 BEGIN IMMEDIATE
            rows = top(conn, board=board, room=room, period=None, now=self._now())
        return Reply(format_board(rows), "ranking.top")

    def _board(self, cmd) -> Board | None:
        """从参数解析榜单：裸发 → 金币榜；`金币` → 金币榜；其余 → None（静默）。"""
        tokens = tuple(getattr(cmd, "tokens", ()) or ())
        if not tokens:
            return COIN_BOARD
        return ACCEPTED_ARGS.get(tokens[0])
