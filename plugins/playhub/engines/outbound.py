"""`outbound` 引擎：需要外部服务的功能（第 3 批只有天气）。

天气在 §9 里是「无 key / 未选型」，所以本批的合同是：
**能接就接，接不上就说接不上**。默认 provider 是 `OfflineProvider`（恒返回 None），
此时回一张说明卡——写明城市必填、服务未接入、以及接上之后会看到什么。
故意不编天气：编一次「今天多云 12~20℃」比不回复更糟，用户会当真。

缓存：同一个城市的成功结果缓存 10 分钟（`kv` 键 `weather:<城市>`），
失败**不缓存**，免得一次网络抖动把城市锁 10 分钟。
"""
from __future__ import annotations

from ..adapters.outbound import DEFAULT_PROVIDER, Weather, WeatherProvider
from ..core import clock, kv, render
from . import Reply, msg_nickname, msg_room, msg_uid, store_of

WEATHER_TTL = 10 * 60
CITY_MAX_LEN = 12
USAGE = "💡 用法：天气 城市（例：天气 成都）"


class OutboundEngine:
    """出站能力。`name` 必须与 `Trigger.engine` 的 `'outbound'` 一致。"""

    name = "outbound"

    def __init__(self, *, provider: WeatherProvider | None = None, now_fn=None):
        self._provider = provider if provider is not None else DEFAULT_PROVIDER
        self._now = now_fn if now_fn is not None else clock.now_ts

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

        cache_key = f"weather:{city}"
        with store.tx(immediate=False) as conn:
            cached = kv.get_json(conn, room, cache_key)
        if cached and int(cached.get("at", 0)) + WEATHER_TTL > now:
            reply = self._card(nickname, self._to_weather(cached))
            return Reply(reply, "outbound.weather")

        result = self._provider.forecast(city)
        if result is None:
            return Reply(self._offline_card(nickname, city), "outbound.weather")

        payload = {"city": result.city, "days": list(result.days), "source": result.source, "at": now}
        with store.tx() as conn:
            kv.set_json(conn, room, cache_key, payload, now)
        return Reply(self._card(nickname, result), "outbound.weather")

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
            render.row("🔌 ", "天气服务还没接入（§9 待定：未选供应商、也没有 key）"),
            render.row("📝 ", "接上以后这里会显示未来三天的天气"),
        ]
        return render.menu(
            "🌤", "天气", rows,
            note_text="现在不编数据：宁可说接不上，也不给你一个假预报",
            tail="💡 想接的话要配一个 WeatherProvider，见 plugins/playhub/adapters/outbound.py",
        )
