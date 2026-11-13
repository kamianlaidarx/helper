"""xiaoapi.cn 出站能力：点歌（详情页 <https://xiaoapi.cn/detail/19>）。

接口 `GET/POST https://xiaoapi.cn/v1/yy_sq.php`：
- 参数 `key`（必填）、`msg`（歌名/关键词，必填）、`n`（第几个结果，默认 1）；
- 成功响应 `{"code":200,"cover":…,"name":…,"singer":…,"quality":…,"url":…,"tips":…}`；
- 失败响应 `{"code":0,"msg":…,"errcode":110xx}`（`11002` 密钥错误、`11005` 频率超限…）。

三条硬约束（写死在代码里，不要"优化"掉）：
1. **`tips` 一律丢弃。** 它是接口自带的推广文案（`慕名API：http://xiaoapi.cn`），
   2026-11-13 用户明确要求"返回把 tips 处理掉"，卡面不展示、也不进日志。
2. **没有 key 就离线。** `build_music_provider()` 只认 `bot_config.json →
   plugins.playhub.xiaoapi_key`（或环境变量 `XIAOAPI_KEY`），拿不到就返回
   `OfflineMusicProvider`，引擎退回原来的"只登记、报位"，绝不悄悄打外部接口。
3. **`search()` 永不抛异常。** 任何失败（网络、超时、非 JSON、码不对、url 不是 http(s)）
   一律返回 `None`，由引擎回一句"没找到"；响应体上限 64 KiB，超时默认 3 秒。
"""
from __future__ import annotations

import json
import logging
import os
import urllib.parse
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol

from .outbound import MAX_RESPONSE_BYTES, _maybe_gunzip, _urlopen

ENDPOINT = "https://xiaoapi.cn/v1/yy_sq.php"
KEY_ENV = "XIAOAPI_KEY"
DEFAULT_TIMEOUT = 3.0
MAX_TIMEOUT = 10.0
TITLE_MAX = 60
SINGER_MAX = 40
QUALITY_MAX = 16

Transport = Callable[[str, float], "bytes | None"]

_log = logging.getLogger("playhub.xiaoapi")


@dataclass(frozen=True)
class Song:
    """点歌结果。`tips` 故意没有字段：接口那个推广文案不进入本插件的任何输出。"""

    name: str
    singer: str = ""
    quality: str = ""
    url: str = ""
    cover: str = ""


class MusicProvider(Protocol):
    enabled: bool

    def search(self, song: str, *, n: int = 1) -> "Song | None": ...


class OfflineMusicProvider:
    """没有 key / 配置非法时的兜底：一问三不知，引擎走"只登记"路径。"""

    enabled = False

    def search(self, song: str, *, n: int = 1) -> "Song | None":
        return None


class XiaoApiMusicProvider:
    enabled = True

    def __init__(
        self,
        key: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        transport: Transport | None = None,
    ):
        self._key = key
        self._timeout = _clean_timeout(timeout)
        self._transport = transport if transport is not None else _urlopen

    def search(self, song: str, *, n: int = 1) -> "Song | None":
        title = (song or "").strip()
        if not title:
            return None
        query = urllib.parse.urlencode({"key": self._key, "msg": title, "n": int(n)})
        try:
            raw = self._transport(f"{ENDPOINT}?{query}", self._timeout)
        except Exception:
            # 传输层异常不外传：一条歌名查不到不该把整条消息处理链带走。
            _log.warning("点歌请求失败（按没找到处理）", exc_info=True)
            return None
        if not raw:
            return None
        return _to_song(raw)


def _to_song(raw: bytes) -> "Song | None":
    if len(raw) > MAX_RESPONSE_BYTES:
        return None
    if raw.startswith(b"\x1f\x8b"):
        raw = _maybe_gunzip(raw) or b""
        if not raw:
            return None
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("code") != 200:
        # 失败响应只有 code/msg/errcode；`msg` 是给人看的（密钥错误、频率超限…），
        # 引擎不展示它（会泄露账号状态），只记一行 debug。
        _log.debug("点歌接口返回失败：errcode=%s", payload.get("errcode"))
        return None
    name = str(payload.get("name") or "").strip()
    url = str(payload.get("url") or "").strip()
    if not name or not url.startswith(("http://", "https://")):
        return None
    cover = str(payload.get("cover") or "").strip()
    return Song(
        name=name[:TITLE_MAX],
        singer=str(payload.get("singer") or "").strip()[:SINGER_MAX],
        quality=str(payload.get("quality") or "").strip()[:QUALITY_MAX],
        url=url,
        cover=cover if cover.startswith(("http://", "https://")) else "",
    )


def _clean_timeout(value: object) -> float:
    try:
        timeout = float(value)          # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    if not 0 < timeout <= MAX_TIMEOUT:
        return DEFAULT_TIMEOUT
    return timeout


DEFAULT_MUSIC_PROVIDER: MusicProvider = OfflineMusicProvider()


def build_music_provider(config: Mapping[str, object] | None = None) -> MusicProvider:
    """按配置装配点歌 provider；拿不到 key 就离线（失败关闭）。"""
    options = config if isinstance(config, Mapping) else {}
    key = options.get("xiaoapi_key")
    if not isinstance(key, str) or not key.strip():
        key = os.environ.get(KEY_ENV, "")
    if not isinstance(key, str) or not key.strip():
        return DEFAULT_MUSIC_PROVIDER
    timeout = _clean_timeout(options.get("xiaoapi_timeout_seconds", DEFAULT_TIMEOUT))
    return XiaoApiMusicProvider(key.strip(), timeout=timeout)
