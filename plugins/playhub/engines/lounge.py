"""`lounge` 引擎：文案馆、群空间、群聊（第 3 批）。

三条都不造币、不写流水：

- `文案`：本地文案库按主题轮转，游标存在 `kv`（`copy:cursor:<主题>`），
  轮完一圈从头再来；库缺失回「文案库未就绪」，不现编。
- `群空间`：只读聚合（`player` / `pair` / `baby` / `wallet` 的 COUNT 与 SUM），
  结果按小时缓存在 `kv`（键 `space:<YYYY-MM-DDTHH>`），避免刷屏时反复扫表。
- `聊天`：智能聊天在 §9 里是**外部依赖未定**（没有接 LLM），所以这里只回一张
  说明卡，把已有的互动玩法列出来，不假装会聊天。
"""
from __future__ import annotations

import json
from pathlib import Path

from ..core import clock, kv, render
from . import Reply, msg_nickname, msg_room, msg_uid, store_of

COPY_BANK = "copy.jsonl"
COPY_CURSOR_PREFIX = "copy:cursor:"
SPACE_CACHE_PREFIX = "space:"
SPACE_TTL = 3600

USAGE_COPY = "💡 用法：文案 / 文案 情话（主题：日常·情话·挽尊·打气·傲娇·沙雕）"


def _copy_paths(store, *, repo_root: Path | None = None) -> tuple[Path, Path]:
    repo = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[3]
    return (
        Path(store.db_path).parent / "content" / COPY_BANK,
        repo / "content" / "playhub" / COPY_BANK,
    )


def load_copy_bank(store, *, repo_root: Path | None = None) -> tuple[dict, ...]:
    """读文案库；两种路径依次尝试，坏行跳过。"""
    for path in _copy_paths(store, repo_root=repo_root):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        rows: list[dict] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            text = row.get("text") if isinstance(row, dict) else None
            if isinstance(text, str) and text.strip():
                rows.append(row)
        if rows:
            return tuple(rows)
    return ()


def _tags_of(row: dict) -> tuple[str, ...]:
    tags = row.get("tag")
    if isinstance(tags, str):
        return (tags,)
    if isinstance(tags, (list, tuple)):
        return tuple(str(t) for t in tags)
    return ()


class LoungeEngine:
    """文案馆 / 群空间 / 群聊。`name` 与 `keywords.Trigger.engine` 逐字一致。"""

    name = "lounge"

    def __init__(self, *, now_fn=None, repo_root=None, logger=None):
        self._now = now_fn if now_fn is not None else clock.now_ts
        self._repo_root = repo_root

    def on_command(self, cmd, msg=None, ctx=None, game=None) -> Reply | None:
        action = getattr(cmd, "action", "")
        if action == "lounge.copy":
            return self.copy(cmd, msg, ctx, game)
        if action == "lounge.space":
            return self.space(cmd, msg, ctx, game)
        if action == "lounge.chat":
            return self.chat(cmd, msg, ctx, game)
        return None

    # -- 文案馆 -------------------------------------------------------------
    def copy(self, cmd, msg=None, ctx=None, game=None) -> Reply | None:
        room = msg_room(msg)
        store = store_of(game)
        if not room or store is None:
            return None
        now = self._now()
        args = (getattr(cmd, "args", "") or "").strip()
        bank = load_copy_bank(store, repo_root=self._repo_root)
        if not bank:
            return Reply("📝 文案库还没准备好：缺 content/playhub/copy.jsonl", "lounge.copy")

        pool = [row for row in bank if not args or args in _tags_of(row)]
        if not pool:
            return Reply(USAGE_COPY, "lounge.copy")

        tag = args or "全部"
        cursor_key = COPY_CURSOR_PREFIX + tag
        with store.tx() as conn:
            index = kv.bump(conn, room, cursor_key, now) % len(pool)
        row = pool[index]
        tags = _tags_of(row)
        text = str(row.get("text") or "").strip()
        return Reply(
            render.result(
                msg_nickname(msg, ctx),
                f"📝 文案 · {tags[0] if tags else tag}",
                [render.quote("📝", text)],
                tail=f"📚 共 {len(pool)} 条，按顺序轮着来；再发一次换下一条",
            ),
            "lounge.copy",
        )

    # -- 群空间 -------------------------------------------------------------
    def space(self, cmd, msg=None, ctx=None, game=None) -> Reply | None:
        room = msg_room(msg)
        store = store_of(game)
        if not room or store is None:
            return None
        now = self._now()
        hour = clock.day_of(now) + "T" + f"{clock._local(now).hour:02d}"  # noqa: SLF001
        cache_key = SPACE_CACHE_PREFIX + hour

        with store.tx(immediate=False) as conn:
            stats = kv.get_json(conn, room, cache_key)
            fresh = stats is None
            if fresh:
                stats = self._collect(conn, room)

        # 写缓存放在只读事务**外面**：SQLite 不允许事务套事务（`BEGIN` 会直接报错），
        # 也不能在只读事务里升级成写事务。
        if fresh:
            with store.tx() as wconn:
                kv.set_json(wconn, room, cache_key, stats, now)

        body = [
            render.field("👥", "群成员档案", f"{stats.get('players', 0)} 人"),
            render.field("💍", "在册关系", f"{stats.get('pairs', 0)} 对"),
            render.field("🍼", "宝宝", f"{stats.get('babies', 0)} 个"),
            render.field("💰", "群内金币总量", f"{stats.get('coin_total', 0)}"),
            render.field("💎", "群内钻石总量", f"{stats.get('diamond_total', 0)}"),
            render.field("📦", "背包物品件数", f"{stats.get('items', 0)} 件"),
        ]
        return Reply(
            render.menu(
                "🏠", "群空间", body,
                note_text="统计每小时刷新一次，只读，不改任何账",
            ),
            "lounge.space",
        )

    def _collect(self, conn, room: str) -> dict[str, int]:
        """只读聚合；都是 COUNT/SUM，没有写入。"""
        def one(sql: str, default: int = 0) -> int:
            row = conn.execute(sql, (room,)).fetchone()
            return int(row[0] or 0) if row and row[0] is not None else default

        coin_total = 0
        diamond_total = 0
        for currency, total in conn.execute(
            "SELECT currency, SUM(balance) FROM wallet WHERE room = ? GROUP BY currency", (room,)
        ):
            if currency == "coin":
                coin_total = int(total or 0)
            elif currency == "diamond":
                diamond_total = int(total or 0)
        return {
            "players": one("SELECT COUNT(*) FROM player WHERE room = ?"),
            "pairs": one("SELECT COUNT(*) FROM pair WHERE room = ? AND state = 'active'"),
            "babies": one("SELECT COUNT(*) FROM baby WHERE room = ?"),
            "items": one("SELECT COALESCE(SUM(qty), 0) FROM inventory WHERE room = ?"),
            "coin_total": coin_total,
            "diamond_total": diamond_total,
        }

    # -- 群聊 ---------------------------------------------------------------
    def chat(self, cmd, msg=None, ctx=None, game=None) -> Reply | None:
        room, uid = msg_room(msg), msg_uid(msg)
        if not room or not uid:
            return None
        args = (getattr(cmd, "args", "") or "").strip()
        if not args:
            return Reply(
                "💡 用法：聊天 你想说的话（例：聊天 今天好累）",
                "lounge.chat",
            )
        body = [
            render.row("🤖 ", "智能聊天还没接上（要单独配一家模型服务）"),
            render.row("🎮 ", "现在能陪你的：13 类猜题、成语接龙、看图猜成语"),
            render.row("💬 ", "想聊天就先 @我 直接说话，走的是另一个通道"),
        ]
        return Reply(
            render.menu(
                "🤖", "智能聊天", body,
                note_text="§9 待定：LLM 服务未选型，选好之前这里只回说明，不假装会聊",
            ),
            "lounge.chat",
        )
