"""消息级幂等（`docs/design/playhub.md` §4.2.10、表 `core/schema.sql:318`）。

微信侧重投是常态（断线重连、`session` 重放、宿主重试）。业务状态位
（`daily_counter` / `pair` / `inventory`）只兜得住「第二次会撞状态」的命令，
送礼、盲盒加购这类「每次都合法」的扣费命令必须靠消息身份去重。

键的两个来源（§4.2.10 已定顺序）：

1. `msg:<room>:<native_message_id>:<action>`——微信侧的原消息 id，最可靠；
   `native_message_id` 为空串或 `"0"` 视为没有。
2. `<session_id>:<sequence>:<action>`——宿主给的会话 + 序号（§6.3 的老口径），
   要求 `sequence > 0` 且 `session_id` 非空。

两个都没有就不生成键、**不做幂等**：编不出身份就别假装有身份，否则会把
不同消息当成同一条吞掉。

`claim()` 自己开事务、先 claim 后执行；业务失败**不释放**（至多一次，不是至少
一次）。过期清理由后台 `sched:purge` 按 30 天删。
"""
from __future__ import annotations

import sqlite3

#: 单条幂等键的最长长度：再长就是上游把整段消息塞进来了，不写进主键。
MAX_KEY_LENGTH = 512

#: `native_message_id` 里表示「没有 id」的写法。
_EMPTY_NATIVE_IDS = frozenset({"", "0"})


def native_id(msg) -> str:
    """取微信侧原消息 id；取不到返回空串。"""
    data = getattr(msg, "data", None)
    if not isinstance(data, dict):
        return ""
    raw = data.get("native_message_id")
    if raw is None:
        return ""
    value = str(raw).strip()
    return "" if value in _EMPTY_NATIVE_IDS else value


def message_key(msg, action: str) -> str | None:
    """按 §4.2.10 的顺序生成幂等键；两个来源都没有时返回 None（不做幂等）。"""
    if not isinstance(action, str) or not action:
        return None

    native = native_id(msg)
    if native:
        room = getattr(msg, "chat_id", "") or ""
        if isinstance(room, str) and room:
            return _clip(f"msg:{room}:{native}:{action}")

    session = getattr(msg, "session_id", "") or ""
    try:
        sequence = int(getattr(msg, "sequence", 0) or 0)
    except (TypeError, ValueError):
        sequence = 0
    if isinstance(session, str) and session and sequence > 0:
        return _clip(f"{session}:{sequence}:{action}")
    return None


def claim(store, key: str, *, scope: str, now: int) -> bool:
    """在独立事务里占下这条消息的幂等键：返回 True=首次，False=重复。

    刻意不用 `INSERT OR IGNORE`：那张表之后要回答「这条消息到底处理过没有」，
    静默忽略会让重复请求看起来像成功。`sqlite3.IntegrityError` 之外的异常
    （如写锁超时）原样上抛，由调用方决定「不执行」——这里不猜。
    """
    if not isinstance(key, str) or not key:
        raise ValueError("幂等键必须是非空字符串")
    try:
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO idem (key, scope, at) VALUES (?, ?, ?)", (key, scope, int(now))
            )
    except sqlite3.IntegrityError:
        return False
    return True


def _clip(key: str) -> str:
    return key[:MAX_KEY_LENGTH]
