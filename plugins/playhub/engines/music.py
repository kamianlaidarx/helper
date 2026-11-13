"""`music` 引擎：点歌（第 3 批，2026-11-13 接上 xiaoapi 曲库）。

两条路径，由 provider 决定：

- **已配 key**（`bot_config.json → plugins.playhub.xiaoapi_key` 或环境变量
  `XIAOAPI_KEY`）：查 <https://xiaoapi.cn/detail/19> 的 QQ 音乐接口，回一张卡：
  歌名 / 歌手 / 音质 / 播放直链。**接口响应里的 `tips` 一律丢弃**（它是推广文案，
  用户 2026-11-13 明确要求处理掉，见 `adapters/xiaoapi.py` 第 1 条硬约束）。
- **没配 key**（`OfflineMusicProvider`）：保持原行为——只登记与报位、不发音频。

共同约束：
- 每人 30 秒冷却（`kv` 键 `music:cd:<uid>`），防止连点刷屏；
- 离线路径队列最多 5 条（`kv` 键 `music:queue`），重启即丢；
- 联网查询走插件的慢活执行器（`adapters/executor.py`），回调线程不被 HTTP 阻塞；
  worker 里只做网络与渲染，**不碰 `Store`**。
"""
from __future__ import annotations

import logging

from ..adapters.xiaoapi import DEFAULT_MUSIC_PROVIDER, MusicProvider, Song
from ..core import clock, kv, render
from . import Reply, msg_nickname, msg_room, msg_uid, store_of

SONG_CD = 30
QUEUE_MAX = 5
QUEUE_KEY = "music:queue"
SONG_MAX_LEN = 40

USAGE = "💡 用法：点歌 歌名（例：点歌 晴天）"
NOT_FOUND_TAIL = "💡 带上歌手名更好找：点歌 晴天 周杰伦"
OFFLINE_TAIL = "💡 队列最多 5 首，机器人重启会清空"


def _cd_key(uid: str) -> str:
    return f"music:cd:{uid}"


class MusicEngine:
    """点歌。`name` 与 `keywords.Trigger.engine` 逐字一致。"""

    name = "music"

    def __init__(self, *, now_fn=None, provider: MusicProvider | None = None,
                 executor=None, logger=None):
        self._now = now_fn if now_fn is not None else clock.now_ts
        self._provider = provider if provider is not None else DEFAULT_MUSIC_PROVIDER
        self._executor = executor
        self._log = logger if logger is not None else logging.getLogger("playhub.music")

    @property
    def online(self) -> bool:
        return bool(getattr(self._provider, "enabled", False))

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

        if self.online:
            cooldown = self._take_cooldown(store, room, uid, now)
            if cooldown is not None:
                return cooldown
            if self._executor is not None and self._executor.submit(
                self._answer_later, ctx, msg, song, nickname, label="music"
            ):
                return None                  # 结果由 worker 直发
            return self._answer_now(song, nickname)

        return self._offline(cmd, msg, store, room, uid, song, nickname, now)

    # -- 联网路径 -----------------------------------------------------------
    def _take_cooldown(self, store, room: str, uid: str, now: int) -> Reply | None:
        with store.tx() as conn:
            try:
                ready_at = int(kv.get(conn, room, _cd_key(uid)) or 0)
            except ValueError:
                ready_at = 0
            if ready_at > now:
                left = ready_at - now
                return Reply(
                    render.result(
                        "", "⏳ 刚点过一首，缓一缓",
                        [render.row("├ ", f"还要等 {left} 秒")],
                        tail="💡 每 30 秒一首，别把歌单一个人包了",
                        at_sender=False,
                    ),
                    "music.request",
                )
            kv.put(conn, room, _cd_key(uid), str(now + SONG_CD), now)
        return None

    def _answer_now(self, song: str, nickname: str) -> Reply:
        found = self._lookup(song)
        return Reply(self._card(found, nickname, song), "music.request")

    def _answer_later(self, ctx, msg, song: str, nickname: str) -> None:
        found = self._lookup(song)
        self._send(ctx, msg, self._card(found, nickname, song), song)

    def _lookup(self, song: str) -> Song | None:
        """provider 已承诺「永不抛」，这里再兜一层，别让一条异常带走整个 worker。"""
        try:
            return self._provider.search(song)
        except Exception:
            self._log.exception("点歌 provider 抛异常（按没找到处理）：song=%s", song)
            return None

    def _card(self, found: Song | None, nickname: str, song: str) -> str:
        if found is None:
            head = f"🎵 没找到《{song}》"
            rows = [render.row("├ ", "曲库和写法都对不上，换个歌名试试")]
            return render.result(
                nickname, head, rows, tail=NOT_FOUND_TAIL, at_sender=bool(nickname)
            )
        rows = [render.row("├ ", f"歌名：{found.name}")]
        if found.singer:
            rows.append(render.row("├ ", f"歌手：{found.singer}"))
        if found.quality:
            rows.append(render.row("├ ", f"音质：{found.quality}"))
        rows.append(render.row("🔗 ", found.url))
        return render.result(
            nickname,
            "🎵 点歌 · 找到了",
            rows,
            tail="💡 直链有时效，点开就能听；再点一首等 30 秒",
            at_sender=bool(nickname),
        )

    def _send(self, ctx, msg, text: str, song: str) -> None:
        reply = getattr(ctx, "reply", None)
        if not callable(reply):
            self._log.warning("点歌结果没法发出去：ctx.reply 不可用（song=%s）", song)
            return
        try:
            reply(msg, text)
        except Exception:
            self._log.exception("点歌结果发送失败：song=%s", song)

    # -- 离线路径（没配 key：只登记、报位）----------------------------------
    def _offline(self, cmd, msg, store, room: str, uid: str, song: str,
                 nickname: str, now: int) -> Reply | None:
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
                note_text="曲库还没接上，现在只登记与报位，暂时不发音频",
                tail=OFFLINE_TAIL,
            ),
            "music.request",
        )
