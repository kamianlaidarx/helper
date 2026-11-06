"""`outbound` 引擎：需要外部服务的功能（目前只有天气）。

天气在 §9 里是「无 key / 未选型」，所以本合同是：**能接就接，接不上就说接不上**。
默认 provider 是 `OfflineProvider`（恒返回 None），此时回一张说明卡——写明城市必填、
服务未接入、以及接上之后会看到什么。故意不编天气：编一次「今天多云 12~20℃」比不回复
更糟，用户会当真。

执行模型（§2.5 第 3、4 条）：

- **有执行器时走异步**：`provider.forecast()` 两跳 HTTP 交给 `adapters/executor`，
  回调线程立刻返回（不回包，结果由 worker 直接发一张卡）。宁可安静几秒，
  也不回两条「正在查 + 结果」的消息。
- **队列满或没有执行器时退化成同步**：慢一点，但不丢查询。
- **worker 不碰 `Store`**：命中缓存仍在回调线程读；写缓存用 `executor.defer()` 交回
  插件线程（下一次 `handle_message` 的 `flush()` 落库）。所以「刚查完立刻再问一次」
  可能还没写进缓存——多花一次额度，好过跨线程写同一条 SQLite 连接。

缓存：同一个城市的成功结果缓存 10 分钟（`kv` 键 `weather:<城市>`），
失败**不缓存**，免得一次网络抖动把城市锁 10 分钟。
"""
from __future__ import annotations

import logging

from ..adapters.outbound import DEFAULT_PROVIDER, Weather, WeatherProvider
from ..core import clock, kv, render
from . import Reply, msg_nickname, msg_room, msg_uid, store_of

WEATHER_TTL = 10 * 60
CITY_MAX_LEN = 12
USAGE = "💡 用法：天气 城市（例：天气 成都）"


class OutboundEngine:
    """出站能力。`name` 必须与 `Trigger.engine` 的 `'outbound'` 一致。"""

    name = "outbound"

    def __init__(
        self,
        *,
        provider: WeatherProvider | None = None,
        now_fn=None,
        executor=None,
        logger=None,
    ):
        self._provider = provider if provider is not None else DEFAULT_PROVIDER
        self._now = now_fn if now_fn is not None else clock.now_ts
        self._executor = executor
        self._log = logger if logger is not None else logging.getLogger("playhub.outbound")

    def on_command(self, cmd, msg=None, ctx=None, game=None) -> Reply | None:
        if getattr(cmd, "action", "") != "outbound.weather":
            return None
        return self.weather(cmd, msg, ctx, game)

    def weather(self, cmd, msg=None, ctx=None, game=None) -> Reply | None:
        room, uid = msg_room(msg), msg_uid(msg)
        store = store_of(game)
        if not room or not uid or store is None:
            return None
        now = self._now()
        nickname = msg_nickname(msg, ctx)
        city = (getattr(cmd, "args", "") or "").strip().lstrip("@＠")
        if not city:
            return Reply(USAGE, "outbound.weather")
        if len(city) > CITY_MAX_LEN:
            city = city[:CITY_MAX_LEN]

        cached = self._cached(store, room, city, now)
        if cached is not None:
            return Reply(self._card(nickname, cached), "outbound.weather")

        if self._executor is not None and self._executor.submit(
            self._answer_later, msg, ctx, game, room, city, nickname, label="weather"
        ):
            return None                      # 结果由 worker 直发（§2.5 第 3 条主路径）
        return self._answer_now(store, room, city, nickname, now)

    # -- 同步路径（测试、未接执行器、队列满）--------------------------------
    def _answer_now(self, store, room: str, city: str, nickname: str, now: int) -> Reply:
        result = self._forecast(city)
        if result is None:
            return Reply(self._offline_card(nickname, city), "outbound.weather")
        self._remember(store, room, city, result, now)
        return Reply(self._card(nickname, result), "outbound.weather")

    # -- 异步路径 -----------------------------------------------------------
    def _answer_later(self, msg, ctx, game, room: str, city: str, nickname: str) -> None:
        """worker 线程：只做网络 + 渲染，绝不碰 `Store`（§2.5 第 4 条）。"""
        result = self._forecast(city)
        text = (
            self._card(nickname, result)
            if result is not None
            else self._offline_card(nickname, city)
        )
        # 先排队落库再发送：发送可能被限流卡住，但它一旦成功，缓存必须已经在队列里，
        # 否则「刚查完再问一次」会稳定地打一次外部接口。
        if result is not None:
            executor = self._executor
            if executor is not None:
                executor.defer(
                    self._remember_cached, store_of(game), room, city, result,
                    label="weather-cache",
                )
        self._send(ctx, msg, text, city)

    def _remember_cached(self, store, room: str, city: str, result: Weather) -> None:
        """插件线程里的落库（`Executor.defer` → `flush`）；库已关时静默作废。"""
        if store is None:
            return
        try:
            self._remember(store, room, city, result, self._now())
        except Exception:
            self._log.warning("天气缓存落库失败（已忽略）：city=%s", city, exc_info=True)

    def _send(self, ctx, msg, text: str, city: str) -> None:
        reply = getattr(ctx, "reply", None)
        if not callable(reply):
            self._log.warning("天气结果没法发出去：ctx.reply 不可用（city=%s）", city)
            return
        try:
            reply(msg, text)
        except Exception:
            self._log.exception("天气结果发送失败：city=%s", city)

    def _forecast(self, city: str) -> Weather | None:
        """provider 已经承诺「永不抛」，这里再兜一层，别让一条异常带走整个 worker。"""
        try:
            return self._provider.forecast(city)
        except Exception:
            self._log.exception("天气 provider 抛异常（按不可用处理）：city=%s", city)
            return None

    # -- 缓存 ---------------------------------------------------------------
    def _cached(self, store, room: str, city: str, now: int) -> Weather | None:
        with store.tx(immediate=False) as conn:
            cached = kv.get_json(conn, room, f"weather:{city}")
        if not cached or int(cached.get("at", 0)) + WEATHER_TTL <= now:
            return None
        return self._to_weather(cached)

    def _remember(self, store, room: str, city: str, result: Weather, now: int) -> None:
        payload = {
            "city": result.city,
            "days": list(result.days),
            "source": result.source,
            "at": now,
        }
        with store.tx() as conn:
            kv.set_json(conn, room, f"weather:{city}", payload, now)

    @staticmethod
    def _to_weather(payload: dict) -> Weather:
        days = payload.get("days")
        return Weather(
            city=str(payload.get("city") or ""),
            days=tuple(str(d) for d in days) if isinstance(days, list) else (),
            source=str(payload.get("source") or "offline"),
        )

    def _card(self, nickname: str, weather: Weather) -> str:
        rows = [render.row("📍 ", f"城市：{weather.city}")]
        rows.extend(render.row("├ ", day) for day in weather.days[:3])
        return render.menu(
            "🌤", "天气", rows,
            note_text=f"数据来源：{weather.source}，缓存 10 分钟",
        )

    def _offline_card(self, nickname: str, city: str) -> str:
        rows = [
            render.row("📍 ", f"城市：{city}"),
            render.row("🔌 ", "天气服务还没配置（bot_config.json → plugins.playhub）"),
            render.row("📝 ", "配置好以后这里会显示未来三天的天气"),
        ]
        return render.menu(
            "🌤", "天气", rows,
            note_text="现在不编数据：宁可说接不上，也不给你一个假预报",
            tail="💡 要开的话填 weather_provider=qweather 与 weather_api_key，见 adapters/outbound.py",
        )
