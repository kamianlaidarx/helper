"""群角色读取适配：只包一层 `ctx.group_member_role()`（§7 第 1 批、`sdk.py:536`）。

薄到什么程度：不做缓存、不做重试、不做群号校验——只负责
「调框架 + 把任何异常/空值折成 `unknown`」，让 `core/permissions.py` 的纯函数
只面对四种角色字符串。异常一律**吞掉并失败关闭**：角色读不到时不能误判成管理员。
"""
from __future__ import annotations

import logging

from ..core.permissions import GroupRole, normalize_role


class MemberFeed:
    """一个 `ctx` 对应一个实例；无状态，可复用。"""

    def __init__(self, ctx, *, logger=None):
        self._ctx = ctx
        self._log = logger if logger is not None else logging.getLogger("playhub.adapters.memberfeed")

    def role(self, room: str, uid: str, *, expected_session=None) -> GroupRole:
        """取 `uid` 在 `room` 里的角色；参数不全、框架没有该方法、抛异常 → `unknown`。"""
        if not isinstance(room, str) or not room or not isinstance(uid, str) or not uid:
            return "unknown"
        getter = getattr(self._ctx, "group_member_role", None)
        if not callable(getter):
            return "unknown"
        try:
            raw = getter(room, uid, expected_session=expected_session)
        except Exception:
            self._log.warning("读群角色失败，按 unknown 处理：room=%s uid=%s", room, uid, exc_info=True)
            return "unknown"
        return normalize_role(raw)
