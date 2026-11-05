"""进程内的「当前游戏」槽位：一个群同一时刻只允许一局游戏。

为什么需要它：成语接龙、看图猜成语、playhub 竞猜各自把对局放在自己的内存状态里
（`plugins/idiom_chain/__init__.py:38`、`plugins/picture_idiom/__init__.py:37`、
`plugins/playhub` 的 `quiz_round` 表），彼此读不到对方「有没有在玩」；
`PluginContext` 也只暴露 `plugin_is_active(name)`——那是「插件启用」，不是「正在玩」。
所以放一张框架级的 房间 → 占用者 表：谁开局谁 `claim`，谁结束谁 `release`，
新的一局开场前先 `claim`，被别的玩法占着就拒绝开场并提示发「结束」。

局限（写在这里，免得以后把它当成分布式锁）：只是本进程内存里的状态，机器人重启
即清空；也不跨进程。当前框架的插件回调是单线程串行的，这已经够用。

`ttl` 用于「会自己超时的玩法」（竞猜一题 5 分钟）：到点自动失效，不必依赖对方
一定走到 release；不给 `ttl` 的（接龙）必须显式 release，`退出接龙`／`结束` 都算。
"""
from __future__ import annotations

import threading
import time

_LOCK = threading.Lock()
# room -> (owner, label, expires_at or None)
_SLOTS: dict[str, tuple[str, str, float | None]] = {}


def _alive(entry: tuple[str, str, float | None], now: float) -> bool:
    expires_at = entry[2]
    return expires_at is None or expires_at > now


def claim(room: str, owner: str, *, label: str = "", ttl: float | None = None,
          now: float | None = None) -> str | None:
    """占用本群的游戏槽。

    成功（包括本来就由自己占着时的续期）返回 `None`；被别人占着返回对方的显示名
    （`label`，没给就用 `owner`），调用方应当据此拒绝开场。
    """
    if not room or not owner:
        raise ValueError("room 与 owner 都不能为空")
    moment = time.monotonic() if now is None else now
    expires_at = None if ttl is None else moment + float(ttl)
    with _LOCK:
        entry = _SLOTS.get(room)
        if entry is not None and _alive(entry, moment) and entry[0] != owner:
            return entry[1]
        _SLOTS[room] = (owner, label or owner, expires_at)
        return None


def release(room: str, owner: str | None = None) -> bool:
    """释放本群游戏槽；给了 `owner` 就只释放自己的（别人的槽不能被顺手清掉）。"""
    with _LOCK:
        entry = _SLOTS.get(room)
        if entry is None:
            return False
        if owner is not None and entry[0] != owner:
            return False
        _SLOTS.pop(room, None)
        return True


def holder(room: str, *, now: float | None = None) -> str | None:
    """当前占用者的显示名；空槽或已过期返回 `None`。"""
    moment = time.monotonic() if now is None else now
    with _LOCK:
        entry = _SLOTS.get(room)
        if entry is None or not _alive(entry, moment):
            return None
        return entry[1]


def holder_owner(room: str, *, now: float | None = None) -> str | None:
    """当前占用者的 owner 标识；空槽或已过期返回 `None`。"""
    moment = time.monotonic() if now is None else now
    with _LOCK:
        entry = _SLOTS.get(room)
        if entry is None or not _alive(entry, moment):
            return None
        return entry[0]


def clear() -> None:
    """清空全部槽位（只给测试用）。"""
    with _LOCK:
        _SLOTS.clear()
