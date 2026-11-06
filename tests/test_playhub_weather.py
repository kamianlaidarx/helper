"""天气（第 3 批 `outbound`）联调：和风天气 provider + 引擎缓存 + 离线降级。

只测协议与装配，不联网：`QWeatherProvider` 的传输层可注入，测试用假实现喂固定
JSON。引擎侧用 `StubProvider`，验证 10 分钟缓存与「离线不编数据」两条硬约束。
"""
from __future__ import annotations

import gzip
import json
import pathlib
import tempfile
import unittest
import urllib.error
from datetime import datetime

from plugins.playhub.adapters.outbound import (
    BREAKER_SECONDS,
    DEFAULT_HOST,
    GEO_HOST,
    MAX_RESPONSE_BYTES,
    OfflineProvider,
    QWeatherProvider,
    Weather,
    build_provider,
)
from plugins.playhub.app import App
from plugins.playhub.core import Store
from plugins.playhub.core.clock import CN
from plugins.playhub.engines import build_engines
from plugins.playhub.engines.outbound import WEATHER_TTL, OutboundEngine

ROOM = "12345678@chatroom"
UID_A = "wxid_a"
NAME_A = "小明"

GEO_OK = {"code": "200", "location": [{"id": "101270101", "name": "成都"}]}
GEO_EMPTY = {"code": "200", "location": []}
FORECAST_OK = {
    "code": "200",
    "daily": [
        {"fxDate": "2026-11-01", "textDay": "多云", "tempMin": "12", "tempMax": "20"},
        {"fxDate": "2026-11-02", "textDay": "晴", "tempMin": "10", "tempMax": "21"},
        {"fxDate": "2026-11-03", "textDay": "小雨", "tempMin": "9", "tempMax": "18"},
        {"fxDate": "2026-11-04", "textDay": "阴", "tempMin": "8", "tempMax": "17"},
    ],
}


def dump(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class FakeTransport:
    """按顺序吐出预设响应；同时记录每次请求的 URL 与超时。"""

    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, float]] = []

    def __call__(self, url: str, timeout: float) -> bytes | None:
        self.calls.append((url, timeout))
        if not self.responses:
            raise AssertionError(f"多余的请求：{url}")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item  # type: ignore[return-value]


def provider(transport: FakeTransport, **kwargs) -> QWeatherProvider:
    return QWeatherProvider("test-key", transport=transport, **kwargs)


class QWeatherParseTests(unittest.TestCase):
    def test_lookup_then_forecast_is_parsed(self) -> None:
        transport = FakeTransport(dump(GEO_OK), dump(FORECAST_OK))
        weather = provider(transport).forecast("成都")
        self.assertIsNotNone(weather)
        assert weather is not None
        self.assertEqual(weather.city, "成都")
        self.assertEqual(weather.source, "qweather")
        self.assertEqual(
            weather.days,
            ("今天 多云 12~20℃", "明天 晴 10~21℃", "后天 小雨 9~18℃"),
            "只取三天，且缺一天就截断",
        )
        self.assertEqual(len(transport.calls), 2, "冷启动两跳：城市名 → ID → 预报")
        self.assertIn("location=%E6%88%90%E9%83%BD", transport.calls[0][0])
        self.assertIn("location=101270101", transport.calls[1][0])
        self.assertTrue(transport.calls[0][0].startswith(f"https://{GEO_HOST}/geo/v2/city/lookup?"))
        self.assertTrue(transport.calls[1][0].startswith(f"https://{DEFAULT_HOST}/v7/weather/3d?"))
        self.assertIn("key=test-key", transport.calls[0][0])

    def test_location_id_is_cached_per_process(self) -> None:
        transport = FakeTransport(dump(GEO_OK), dump(FORECAST_OK), dump(FORECAST_OK))
        client = provider(transport)
        client.forecast("成都")
        client.forecast("成都")
        self.assertEqual(len(transport.calls), 3, "第二次只查预报，不重复解析城市名")

    def test_days_stop_at_first_incomplete_row(self) -> None:
        broken = {"code": "200", "daily": [FORECAST_OK["daily"][0], {"fxDate": "x"}, FORECAST_OK["daily"][2]]}
        weather = provider(FakeTransport(dump(GEO_OK), dump(broken))).forecast("成都")
        assert weather is not None
        self.assertEqual(weather.days, ("今天 多云 12~20℃",), "第二天不完整，不能把第三天标成明天")

    def test_non_200_code_returns_none(self) -> None:
        self.assertIsNone(provider(FakeTransport(dump({"code": "401"}))).forecast("成都"))

    def test_empty_location_list_returns_none(self) -> None:
        self.assertIsNone(provider(FakeTransport(dump(GEO_EMPTY))).forecast("成都"))

    def test_transport_error_returns_none(self) -> None:
        transport = FakeTransport(urllib.error.URLError("断网"))
        self.assertIsNone(provider(transport).forecast("成都"), "网络异常必须被吞成 None")

    def test_timeout_returns_none(self) -> None:
        transport = FakeTransport(TimeoutError("超时"))
        self.assertIsNone(provider(transport).forecast("成都"))

    def test_bad_json_returns_none(self) -> None:
        self.assertIsNone(provider(FakeTransport(b"{not json")).forecast("成都"))

    def test_oversize_body_returns_none(self) -> None:
        big = b"{" + b" " * MAX_RESPONSE_BYTES + b"}"
        self.assertIsNone(provider(FakeTransport(big)).forecast("成都"))

    def test_empty_city_does_not_call_provider(self) -> None:
        transport = FakeTransport()
        self.assertIsNone(provider(transport).forecast("   "))
        self.assertEqual(transport.calls, [], "空城市不该发请求")

    def test_missing_key_returns_none(self) -> None:
        transport = FakeTransport(dump(GEO_OK))
        client = QWeatherProvider("  ", transport=transport)
        self.assertIsNone(client.forecast("成都"))
        self.assertEqual(transport.calls, [])

    def test_host_with_path_is_rejected(self) -> None:
        transport = FakeTransport(dump(GEO_OK), dump(FORECAST_OK))
        client = provider(transport, host="evil.example.com/../x", geo_host="http://geo.example.com/")
        client.forecast("成都")
        self.assertTrue(transport.calls[1][0].startswith(f"https://{DEFAULT_HOST}/"), "非法主机退回默认")
        # 协议头与尾斜杠会被剥掉，但一律强制 https。
        self.assertTrue(transport.calls[0][0].startswith("https://geo.example.com/"))

    def test_timeout_is_clamped(self) -> None:
        transport = FakeTransport(dump(GEO_OK), dump(FORECAST_OK))
        client = provider(transport, timeout=999)
        client.forecast("成都")
        self.assertLessEqual(transport.calls[0][1], 10, "超时必须被夹到上限内")


class BreakerTests(unittest.TestCase):
    """熔断（§8.4）：连续 3 次传输失败停 5 分钟；「查无此城」不算失败。"""

    @staticmethod
    def client(transport: FakeTransport) -> tuple[QWeatherProvider, list[float]]:
        now = [0.0]
        return QWeatherProvider("k", transport=transport, clock=lambda: now[0]), now

    def test_three_transport_failures_open_the_breaker(self) -> None:
        transport = FakeTransport(urllib.error.URLError("x"), urllib.error.URLError("x"),
                                  urllib.error.URLError("x"), dump(GEO_OK), dump(FORECAST_OK))
        client, now = self.client(transport)
        for _ in range(3):
            self.assertIsNone(client.forecast("成都"))
        self.assertEqual(len(transport.calls), 3)
        self.assertIsNone(client.forecast("成都"), "熔断期间直接放弃，不发请求")
        self.assertEqual(len(transport.calls), 3)
        now[0] += BREAKER_SECONDS
        self.assertIsNotNone(client.forecast("成都"), "过 5 分钟后自动恢复")
        self.assertEqual(len(transport.calls), 5)

    def test_unknown_city_does_not_open_the_breaker(self) -> None:
        transport = FakeTransport(dump({"code": "404"}), dump({"code": "404"}),
                                  dump({"code": "404"}), dump(GEO_OK), dump(FORECAST_OK))
        client, _ = self.client(transport)
        for _ in range(3):
            self.assertIsNone(client.forecast("火星"))
        self.assertIsNotNone(client.forecast("成都"), "问错城市不能连累正常城市")
        self.assertEqual(len(transport.calls), 5)

    def test_success_resets_the_failure_count(self) -> None:
        transport = FakeTransport(urllib.error.URLError("x"), urllib.error.URLError("x"),
                                  dump(GEO_OK), dump(FORECAST_OK),
                                  urllib.error.URLError("x"), urllib.error.URLError("x"),
                                  dump(FORECAST_OK))
        client, _ = self.client(transport)
        self.assertIsNone(client.forecast("成都"))
        self.assertIsNone(client.forecast("成都"))
        self.assertIsNotNone(client.forecast("成都"), "成功一次就把计数清零")
        self.assertIsNone(client.forecast("成都"))
        self.assertIsNone(client.forecast("成都"))
        self.assertIsNotNone(client.forecast("成都"), "两次失败不该熔断")
        self.assertEqual(len(transport.calls), 7)


class TransportDecodingTests(unittest.TestCase):
    """和风的专属 API Host 会无视 `Accept-Encoding: identity` 直接回 gzip（实测）。"""

    def test_plain_body_passes_through(self) -> None:
        from plugins.playhub.adapters.outbound import _maybe_gunzip

        raw = dump(GEO_OK)
        self.assertEqual(_maybe_gunzip(raw), raw)

    def test_gzip_body_is_decompressed(self) -> None:
        from plugins.playhub.adapters.outbound import _maybe_gunzip

        raw = gzip.compress(dump(GEO_OK))
        self.assertEqual(_maybe_gunzip(raw), dump(GEO_OK))
        self.assertEqual(json.loads(_maybe_gunzip(raw).decode("utf-8"))["code"], "200")

    def test_broken_gzip_returns_none(self) -> None:
        from plugins.playhub.adapters.outbound import _maybe_gunzip

        self.assertIsNone(_maybe_gunzip(b"\x1f\x8b\x08\x00truncated"))

    def test_oversize_after_decompression_returns_none(self) -> None:
        from plugins.playhub.adapters.outbound import _maybe_gunzip

        bomb = gzip.compress(b" " * (MAX_RESPONSE_BYTES + 1024))
        self.assertIsNone(_maybe_gunzip(bomb), "解压后超限也要拒，防 gzip 炸弹")


class BuildProviderTests(unittest.TestCase):
    def test_defaults_to_offline(self) -> None:
        for config in (None, {}, {"weather_provider": "qweather"},
                       {"weather_provider": "qweather", "weather_api_key": "   "},
                       {"weather_provider": "openweather", "weather_api_key": "k"}):
            self.assertIsInstance(build_provider(config), OfflineProvider, f"配置应退回离线：{config}")

    def test_key_enables_qweather(self) -> None:
        client = build_provider({
            "weather_provider": "QWeather",
            "weather_api_key": " k ",
            "weather_api_host": "api.qweather.com",
            "weather_timeout_seconds": 2,
        })
        self.assertIsInstance(client, QWeatherProvider)
        assert isinstance(client, QWeatherProvider)
        self.assertEqual(client._host, "api.qweather.com")       # noqa: SLF001 - 装配断言
        self.assertEqual(client._key, "k")                        # noqa: SLF001
        self.assertEqual(client._timeout, 2.0)                    # noqa: SLF001

    def test_bad_timeout_falls_back(self) -> None:
        client = build_provider({"weather_provider": "qweather", "weather_api_key": "k",
                                 "weather_timeout_seconds": 0})
        assert isinstance(client, QWeatherProvider)
        self.assertEqual(client._timeout, QWeatherProvider("k")._timeout)   # noqa: SLF001

    def test_single_api_host_covers_both_hops(self) -> None:
        """新方案（2023+）一个专属 API Host 同时服务 geo 与 weather，只填一个就够。"""
        client = build_provider({
            "weather_provider": "qweather",
            "weather_api_key": "k",
            "weather_api_host": "abc123.re.qweatherapi.com",
        })
        assert isinstance(client, QWeatherProvider)
        self.assertEqual(client._host, "abc123.re.qweatherapi.com")        # noqa: SLF001
        self.assertEqual(client._geo_host, "abc123.re.qweatherapi.com")    # noqa: SLF001

    def test_geo_host_override_wins(self) -> None:
        client = build_provider({
            "weather_provider": "qweather",
            "weather_api_key": "k",
            "weather_api_host": "abc123.re.qweatherapi.com",
            "weather_geo_host": "geo.example.com",
        })
        assert isinstance(client, QWeatherProvider)
        self.assertEqual(client._geo_host, "geo.example.com")              # noqa: SLF001

    def test_build_engines_uses_config(self) -> None:
        engines = build_engines(config={"weather_provider": "qweather", "weather_api_key": "k"})
        self.assertIsInstance(engines["outbound"]._provider, QWeatherProvider)   # noqa: SLF001
        offline = build_engines()
        self.assertIsInstance(offline["outbound"]._provider, OfflineProvider)    # noqa: SLF001


class StubProvider:
    name = "stub"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def forecast(self, city: str) -> Weather | None:
        self.calls.append(city)
        return Weather(city=city, days=("今天 多云 12~20℃", "明天 晴 10~21℃"), source="stub")


class FakeMsg:
    def __init__(self, text: str, *, chat_id: str = ROOM, sender_id: str = UID_A, nickname: str = NAME_A):
        self.text = text
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.is_self = False
        self.data = {"sender_wechat_name": nickname}


class FakeCtx:
    class _Members:
        def resolve(self, name, room=None):
            raise KeyError(name)

    def __init__(self) -> None:
        self.members = self._Members()


class FakeClock:
    def __init__(self) -> None:
        self.ts = int(datetime.fromisoformat("2026-11-01T12:00:00").replace(tzinfo=CN).timestamp())

    def __call__(self) -> int:
        return self.ts

    def advance(self, seconds: int) -> None:
        self.ts += seconds


class WeatherEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self.store = Store(self.tmp / "playhub.sqlite3", account="acct")
        self.store.migrate()
        self.clock = FakeClock()
        self.ctx = FakeCtx()
        self.stub = StubProvider()
        self.app = App(
            store=self.store,
            now_fn=self.clock,
            repo_root=self.tmp,
            engines={"outbound": OutboundEngine(provider=self.stub, now_fn=self.clock)},
        )

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def say(self, text: str):
        return self.app.handle_message(self.ctx, FakeMsg(text))

    def test_live_provider_renders_card(self) -> None:
        reply = self.say("天气 成都")
        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertEqual(reply.action, "outbound.weather")
        self.assertIn("城市：成都", reply.text)
        self.assertIn("今天 多云 12~20℃", reply.text)
        self.assertIn("数据来源：stub", reply.text)

    def test_result_is_cached_for_ten_minutes(self) -> None:
        self.say("天气 成都")
        self.say("天气 成都")
        self.assertEqual(self.stub.calls, ["成都"], "10 分钟内第二次不该再问 provider")

    def test_cache_expires(self) -> None:
        self.say("天气 成都")
        self.clock.advance(WEATHER_TTL + 1)
        self.say("天气 成都")
        self.assertEqual(self.stub.calls, ["成都", "成都"])

    def test_each_city_has_its_own_cache_entry(self) -> None:
        self.say("天气 成都")
        self.say("天气 重庆")
        self.assertEqual(self.stub.calls, ["成都", "重庆"])

    def test_offline_provider_does_not_write_cache(self) -> None:
        app = App(
            store=self.store,
            now_fn=self.clock,
            repo_root=self.tmp,
            engines={"outbound": OutboundEngine(provider=OfflineProvider(), now_fn=self.clock)},
        )
        text = app.handle_message(self.ctx, FakeMsg("天气 成都")).text
        self.assertIn("天气服务还没配置", text)
        with self.store.tx(immediate=False) as conn:
            rows = conn.execute("SELECT COUNT(*) FROM kv WHERE key LIKE 'weather:%'").fetchone()[0]
        self.assertEqual(rows, 0, "接不上时不缓存，免得一次抖动把城市锁十分钟")

    def test_city_is_truncated(self) -> None:
        reply = self.say("天气 " + "成" * 30)
        self.assertIn("城市：" + "成" * 12, reply.text)


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
