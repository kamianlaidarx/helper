"""群角色门禁（`docs/design/playhub.md:170` 第 14 行、§7 第 1 批）。

**纯函数**：只吃一个角色字符串，不碰 `ctx`、不查库、不联网、不打日志。
本批只做「定义 + 单测」，**不接任何玩法调用**——签到/抽签/榜单对所有人开放，
权限门禁等服务端侧玩法（小黑屋、黑名单）落地时再接线。

失败关闭：拿不准的角色（`unknown`、空串、`None`、非字符串、以及任何不在四种
取值内的拼写）一律判定为**不放行**。大小写与首尾空白不敏感（框架返回的是小写字面量，
但 `OWNER`、` owner ` 这类输入按同一角色处理）。宁可让运维多给一次权限，
也不让陌生值默默通过。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# §3 / 框架口径：群角色只有这四种。`unknown` 是「没取到」，不是一种真角色。
GroupRole = Literal["owner", "admin", "member", "unknown"]

ROLES: tuple[GroupRole, ...] = ("owner", "admin", "member", "unknown")

# 只有群主与管理员放行；`member` 不放行（本批门禁的默认档位）。
ALLOWED_ROLES: frozenset[str] = frozenset({"owner", "admin"})


@dataclass(frozen=True)
class GateDecision:
    """一次判定结果。`reason` 只用于日志/审计，不发给用户。"""

    allowed: bool
    role: GroupRole
    reason: str = ""


def normalize_role(value: object) -> GroupRole:
    """把任意输入折成四种角色之一；折不出来的都是 `unknown`。"""
    if not isinstance(value, str):
        return "unknown"
    role = value.strip().lower()
    return role if role in ROLES else "unknown"      # type: ignore[return-value]


def is_allowed(role: object) -> bool:
    """是否放行：`owner` / `admin` 放行，其余（含 `unknown`）拒绝。"""
    return normalize_role(role) in ALLOWED_ROLES


def gate(role: object) -> GateDecision:
    """带原因的判定；本批不接任何玩法，只供后续侧玩法与单测调用。"""
    normalized = normalize_role(role)
    if normalized in ALLOWED_ROLES:
        return GateDecision(allowed=True, role=normalized, reason="管理角色放行")
    if normalized == "unknown":
        return GateDecision(allowed=False, role=normalized, reason="角色未知，失败关闭")
    return GateDecision(allowed=False, role=normalized, reason="普通成员不放行")
