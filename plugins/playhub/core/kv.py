"""`kv` 表的读写小工具：房间级键值（游标、计数器、临时状态都用它）。

只做三件事——读字符串、写字符串、按 JSON 读写对象；不做缓存也不做校验。
`value` 列是 TEXT，调用方负责序列化（本模块的 `get_json`/`set_json` 已经做了）。

为什么单独一个模块：`engines/` 里已经有 3 处 SQL 豁免（`ranking.py`/`daily.py`/
`app.py`，见 `docs/design/playhub.md` §3.2），新增引擎不该再让 SQL 长满业务文件。
"""
from __future__ import annotations

import json


def get(conn, room: str, key: str) -> str | None:
    """读一个房间级键；不存在返回 None。"""
    row = conn.execute(
        "SELECT value FROM kv WHERE room = ? AND key = ?", (room, key)
    ).fetchone()
    return None if row is None else str(row[0])


def put(conn, room: str, key: str, value: str, now: int) -> None:
    """写/覆盖一个房间级键。"""
    conn.execute(
        "INSERT INTO kv (room, key, value, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(room, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (room, key, value, now),
    )


def get_json(conn, room: str, key: str) -> dict | None:
    """读一个 JSON 对象；空值/坏值都返回 None（坏值不该让玩法崩）。"""
    raw = get(conn, room, key)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def set_json(conn, room: str, key: str, value: dict, now: int) -> None:
    put(conn, room, key, json.dumps(value, ensure_ascii=False, separators=(",", ":")), now)


def bump(conn, room: str, key: str, now: int, *, delta: int = 1) -> int:
    """把键当计数器自增（不存在从 0 起），返回新值；坏值按 0 处理。"""
    raw = get(conn, room, key)
    try:
        current = int(raw) if raw is not None else 0
    except ValueError:
        current = 0
    current += delta
    put(conn, room, key, str(current), now)
    return current


def clear(conn, room: str, key: str) -> None:
    """删一个键（用于「解除/收局」这类状态复位）。"""
    conn.execute("DELETE FROM kv WHERE room = ? AND key = ?", (room, key))
