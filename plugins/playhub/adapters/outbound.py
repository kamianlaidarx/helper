"""出站能力适配层：对外部服务的**唯一**接触面。

为什么单独一层：`engines/` 只该处理「群里的语义」，一旦直接 `requests.get`，
换供应商就是改业务代码。这里用 Protocol 定协议，引擎只认协议。

**怎么接上天气（2026-11-06 定：和风天气 QWeather）**：

1. 在 `bot_config.json` 的 `plugins.playhub` 里写 `"weather_provider": "qweather"`
   与 `"weather_api_key": "<key>"`；
2. 可选覆盖 `weather_api_host`（默认 `devapi.qweather.com`——个人开发者免费订阅用这个，
   商业订阅改成 `api.qweather.com`）、`weather_geo_host`（默认 `geoapi.qweather.com`）、
   `weather_timeout_seconds`（默认 3，允许 0 < t ≤ 10）；
3. `build_provider()` 据此装配；**没配 key、名字不认识、值非法** 一律退回
   `OfflineProvider`——它不编天气，只返回 None，由引擎回一张说明卡。

已知限制（`docs/design/playhub.md` §8.6）：`QWeatherProvider` 是**同步** HTTP，
冷启动一次要两跳（城市名 → LocationID、LocationID → 预报），最坏阻塞约
`2 × weather_timeout_seconds`。同一进程内城市名解析结果会缓存，所以只有
「第一次问某个城市」有这一跳。彻底异步要先落 `adapters/executor.py`（设计 §2.5
要求慢活离开回调，本批尚未实现）——在那之前，天气是唯一会在回调里发 HTTP 的玩法，
介意就干脆别配 key（保持离线）。

协议刻意做成「同步、纯数据、不抛异常」：网络失败一律返回 None，让引擎统一回
「服务不可用」，避免每种异常各写一段话术。
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

QWEATHER = "qweather"
DEFAULT_HOST = "devapi.qweather.com"        # 个人开发者（免费订阅）
COMMERCIAL_HOST = "api.qweather.com"        # 商业订阅（仅作注释，值来自配置）
GEO_HOST = "geoapi.qweather.com"
CITY_LOOKUP_PATH = "/geo/v2/city/lookup"
FORECAST_PATH = "/v7/weather/3d"
DEFAULT_TIMEOUT = 3.0
TIMEOUT_MAX = 10.0
MAX_RESPONSE_BYTES = 64 * 1024
DAY_LABELS = ("今天", "明天", "后天")
USER_AGENT = "helper-playhub/1.0"
#: 熔断（设计 §8.4）：连续 3 次失败停 5 分钟，免得对端挂了还每句话都去撞一次。
FAIL_THRESHOLD = 3
BREAKER_SECONDS = 300.0
#: 业务码「查无此城」不算失败——否则用户连问三次火星就把真城市一起停了。
NOT_FOUND_CODE = "404"

#: 传输层签名：`(url, timeout) -> 响应字节 | None`。测试注入假实现，线上走 `_urlopen`。
Transport = Callable[[str, float], "bytes | None"]


@dataclass(frozen=True)
class Weather:
    """三天预报的一行一天；`text` 是给人看的一整句。"""

    city: str
    days: tuple[str, ...]          # 每项形如 "今天 多云 12~20℃"
    source: str = "offline"


@runtime_checkable
class WeatherProvider(Protocol):
    """天气服务协议。失败返回 None，**不抛异常**。"""

    name: str

    def forecast(self, city: str) -> Weather | None:  # pragma: no cover - 协议
        ...


class OfflineProvider:
    """默认实现：不联网、不编数据，永远返回 None。"""

    name = "offline"

    def forecast(self, city: str) -> Weather | None:  # noqa: ARG002 - 协议签名
        return None


class QWeatherProvider:
    """和风天气（QWeather）v7：城市名 → LocationID → 未来三天预报。

    设计要点：
    - **绝不抛异常**，任何失败（网络、超时、码值非 200、JSON 坏、超长）都返回 None；
    - 城市名解析结果（LocationID）在进程内缓存，热路径只有一次请求；
    - 响应体上限 `MAX_RESPONSE_BYTES`，防止对端返回巨大内容拖死回调；
    - 连续 `FAIL_THRESHOLD` 次失败后熔断 `BREAKER_SECONDS` 秒，期间直接返回 None
      （「查无此城」不计入失败，否则问错城市会连累正常城市）。
    """

    name = QWEATHER

    def __init__(
        self,
        api_key: str,
        *,
        host: str = DEFAULT_HOST,
        geo_host: str = GEO_HOST,
        timeout: float = DEFAULT_TIMEOUT,
        transport: Transport | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._key = (api_key or "").strip()
        self._host = self._clean_host(host) or DEFAULT_HOST
        self._geo_host = self._clean_host(geo_host) or GEO_HOST
        self._timeout = self._clean_timeout(timeout)
        self._fetch = transport if transport is not None else _urlopen
        self._locations: dict[str, str] = {}
        self._clock = clock if clock is not None else time.monotonic
        self._failures = 0
        self._open_until = 0.0

    # -- 协议 ---------------------------------------------------------------
    def forecast(self, city: str) -> Weather | None:
        name = (city or "").strip()
        if not name or not self._key:
            return None
        if self._clock() < self._open_until:
            return None                 # 熔断中：已知对端不可用，不再发请求
        try:
            location = self._location_id(name)
            if not location:
                return None
            payload = self._get_json(
                self._url(self._host, FORECAST_PATH, {"location": location, "lang": "zh"})
            )
            days = self._days(payload)
            return Weather(city=name, days=days, source=self.name) if days else None
        except Exception:          # 协议：失败一律 None，绝不把异常抛回回调
            self._failed()
            return None

    # -- 熔断 ---------------------------------------------------------------
    def _failed(self) -> None:
        self._failures += 1
        if self._failures >= FAIL_THRESHOLD:
            self._failures = 0
            self._open_until = self._clock() + BREAKER_SECONDS

    def _succeeded(self) -> None:
        self._failures = 0

    # -- 内部 ---------------------------------------------------------------
    @staticmethod
    def _clean_host(value: object) -> str:
        """只接受域名：去掉协议头与尾斜杠，含 `/`、空格、`:` 的一律作废（防注入）。"""
        text = str(value or "").strip().lower()
        for prefix in ("https://", "http://"):
            if text.startswith(prefix):
                text = text[len(prefix):]
        text = text.rstrip("/")
        if not text or any(ch in text for ch in "/ :"):
            return ""
        return text

    @staticmethod
    def _clean_timeout(value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return DEFAULT_TIMEOUT
        number = float(value)
        return number if 0 < number <= TIMEOUT_MAX else DEFAULT_TIMEOUT

    def _location_id(self, name: str) -> str:
        cached = self._locations.get(name)
        if cached:
            return cached
        payload = self._get_json(
            self._url(self._geo_host, CITY_LOOKUP_PATH, {"location": name, "number": 1, "lang": "zh"})
        )
        items = payload.get("location") if isinstance(payload, Mapping) else None
        if not isinstance(items, list) or not items or not isinstance(items[0], Mapping):
            return ""
        value = str(items[0].get("id") or "").strip()
        if value:
            self._locations[name] = value      # 只缓存成功结果，失败下次还能重试
        return value

    @staticmethod
    def _days(payload: object) -> tuple[str, ...]:
        """只取**从今天开始连续有效**的条目：缺一天就截断，避免把明天标成今天。"""
        if not isinstance(payload, Mapping):
            return ()
        daily = payload.get("daily")
        if not isinstance(daily, list):
            return ()
        rows: list[str] = []
        for label, item in zip(DAY_LABELS, daily):
            if not isinstance(item, Mapping):
                break
            text = str(item.get("textDay") or "").strip()
            low, high = item.get("tempMin"), item.get("tempMax")
            if not text or low is None or high is None:
                break
            rows.append(f"{label} {text} {low}~{high}℃")
        return tuple(rows)

    def _get_json(self, url: str) -> Mapping[str, object] | None:
        """一次请求 → 解析。失败返回 None，并按传输级/业务级分别记账（见熔断）。"""
        try:
            raw = self._fetch(url, self._timeout)
        except Exception:          # 网络异常、超时：都算传输失败
            self._failed()
            return None
        if not isinstance(raw, (bytes, bytearray)) or not raw:
            self._failed()
            return None
        if len(raw) > MAX_RESPONSE_BYTES:
            self._failed()
            return None
        try:
            data = json.loads(bytes(raw).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._failed()
            return None
        if not isinstance(data, Mapping):
            self._failed()
            return None
        code = str(data.get("code") or "")
        if code == "200":
            self._succeeded()
            return data
        if code != NOT_FOUND_CODE:
            self._failed()         # 鉴权失败、额度用尽等：值得熔断
        return None

    def _url(self, host: str, path: str, params: Mapping[str, object]) -> str:
        query = urllib.parse.urlencode({**params, "key": self._key})
        return f"https://{host}{path}?{query}"


def _urlopen(url: str, timeout: float) -> bytes | None:
    """默认传输：一次 GET，只收 200，读满上限就放弃。异常交给调用方吞。"""
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:   # noqa: S310 - 固定 https
        if getattr(response, "status", 200) != 200:
            return None
        data = response.read(MAX_RESPONSE_BYTES + 1)
    return data if len(data) <= MAX_RESPONSE_BYTES else None


DEFAULT_PROVIDER: WeatherProvider = OfflineProvider()


def build_provider(config: Mapping[str, object] | None = None) -> WeatherProvider:
    """按 `bot_config.json → plugins.playhub` 的配置装配 provider。

    只有「名字是 qweather **且** 有非空 key」才联网，其余一律离线——失败关闭，
    免得配置写错就悄悄去调外部接口。
    """
    options = config if isinstance(config, Mapping) else {}
    if str(options.get("weather_provider") or "").strip().lower() != QWEATHER:
        return DEFAULT_PROVIDER
    key = options.get("weather_api_key")
    if not isinstance(key, str) or not key.strip():
        return DEFAULT_PROVIDER
    host = str(options.get("weather_api_host") or DEFAULT_HOST)
    geo_host = str(options.get("weather_geo_host") or GEO_HOST)
    timeout = QWeatherProvider._clean_timeout(options.get("weather_timeout_seconds", DEFAULT_TIMEOUT))
    return QWeatherProvider(key, host=host, geo_host=geo_host, timeout=timeout)
