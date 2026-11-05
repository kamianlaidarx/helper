"""出站能力适配层（第 3 批）：对外部服务的**唯一**接触面。

为什么单独一层：`engines/` 只该处理「群里的语义」，一旦直接 `requests.get`，
换供应商就是改业务代码。这里用 Protocol 定协议，引擎只认协议。

本批只有天气，且 §9 明确「无 key / 未选型」，所以默认实现是
`OfflineProvider`——**它不编天气**，只返回 None，由引擎回一张说明卡。
真要接线上服务时：

1. 写一个类实现 `WeatherProvider.forecast(city) -> Weather | None`；
2. 在 `plugins/playhub/plugin.json` 的 requires 里加上对应能力声明；
3. 装配时替换 `DEFAULT_PROVIDER`（或注入 `OutboundEngine(provider=...)`）。

协议刻意做成「同步、纯数据、不抛异常」：网络失败一律返回 None，让引擎统一回
「服务不可用」，避免每种异常各写一段话术。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


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


DEFAULT_PROVIDER: WeatherProvider = OfflineProvider()
