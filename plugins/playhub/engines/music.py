"""`music` 引擎：点歌（第 3 批）。

§9 明写「音源未接，默认只回文字『已点：《歌名》- 歌手』，**不发音频**」，所以本批
只做**点歌队列**：谁点了什么、排在第几位，回答得清楚；真发歌要等音源选型。

- 每人 30 秒冷却（`kv` 键 `music:cd:<uid>`），防止连点刷屏；
- 队列最多 5 条，满了提示等前面的唱完；
- 队列存 `kv` 键 `music:queue`（JSON：`{"items": [...]}`），重启即丢——本批没有
  播放器，队列只是「点歌记录」，丢了不影响余额与积分（§7「游戏内状态玩完就丢」）。
"""
from __future__ import annotations

from ..core import clock, kv, render
from . import Reply, msg_nickname, msg_room, msg_uid, store_of

SONG_CD = 30
QUEUE_MAX = 5
QUEUE_KEY = "music:queue"
SONG_MAX_LEN = 40

USAGE = "💡 用法：点歌 歌名（例：点歌 晴天）"


def _cd_key(uid: str) -> str:
    return f"music:cd:{uid}"


class MusicEngine:
    """点歌队列。`name` 与 `keywords.Trigger.engine` 逐字一致。"""

    name = "music"

    def __init__(self, *, now_fn=None):
        self._now = now_fn if now_fn is not None else clock.now_ts

    def on_command(self, cmd, msg=None, ctx=None, game=None) -> Reply | None:
        if getattr(cmd, "action", "") != "music.request":
            return None
        return self.request(cmd, msg, ctx, game)

    def request(self, cmd, msg=None, ctx=None, game=None) -> Reply | None:
        room, uid = msg_room(msg), msg_uid(msg)
        store = store_of(game)
        if not room or not uid or store is None:
            return None
        now = self._now()
        nickname = msg_nickname(msg, ctx)
        song = (getattr(cmd, "args", "") or "").strip()
        if not song:
            return Reply(USAGE, "music.request")
        if len(song) > SONG_MAX_LEN:
            song = song[:SONG_MAX_LEN]

        with store.tx() as conn:
            try:
                ready_at = int(kv.get(conn, room, _cd_key(uid)) or 0)
            except ValueError:
                ready_at = 0
            if ready_at > now:
                left = ready_at - now
                return Reply(
                    render.result(
                        nickname, "⏳ 刚点过一首，缓一缓",
                        [render.row("├ ", f"还要等 {left} 秒")],
                        tail="💡 每 30 秒一首，别把歌单一个人包了",
                    ),
                    "music.request",
                )
            queue = kv.get_json(conn, room, QUEUE_KEY) or {}
            items = [str(x) for x in queue.get("items", []) if isinstance(x, str)]
            if len(items) >= QUEUE_MAX:
                return Reply(
                    render.result(
                        nickname, "📻 点歌台满了",
                        [render.row("├ ", f"队列已有 {len(items)} 首，等前面的唱完再点")],
                    ),
                    "music.request",
                )
            items.append(f"{song}（{nickname}）")
            kv.set_json(conn, room, QUEUE_KEY, {"items": items}, now)
            kv.put(conn, room, _cd_key(uid), str(now + SONG_CD), now)

        body = [
            render.row("🎵 ", f"已点：《{song}》"),
            render.field("📻", "排位", f"第 {len(items)} 位（前面还有 {len(items) - 1} 首）"),
        ]
        return Reply(
            render.menu(
                "🎵", "点歌", body,
                note_text="音源还没接（§9 待定），现在只登记与报位，不发音频",
                tail="💡 队列最多 5 首，机器人重启会清空",
            ),
            "music.request",
        )
