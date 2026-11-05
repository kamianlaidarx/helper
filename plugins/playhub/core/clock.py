"""时间工具：全插件统一走北京时区（Asia/Shanghai）。

为什么要独立一个模块：SQLite 的时间函数默认 UTC，日切会错 8 小时；文档
（`docs/design/playhub.md:723-724`、`1132`）要求 `day` 一律由 Python 生成后传参，
禁止用 SQL 的 `date('now')`。所有"今天""本周""还有多久"都必须经过这里。
"""
from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

CN = ZoneInfo("Asia/Shanghai")


def now_ts() -> int:
    """当前 unix 秒（整数）。所有 `now` 参数都用它生成。"""
    return int(time.time())


def _local(ts: float | None) -> datetime:
    """把 unix 秒换算成北京时间；`None` 表示取当前时刻。"""
    return datetime.fromtimestamp(now_ts() if ts is None else ts, CN)


def day_of(ts: float | None = None) -> str:
    """北京时间的日期键 `'YYYY-MM-DD'`，用于 daily_counter / activity_daily 的 day 列。"""
    return _local(ts).strftime("%Y-%m-%d")


def week_of(ts: float | None = None) -> str:
    """北京时间的 ISO 周键 `'YYYY-Www'`（周一为周起点，与 §3.4 的周榜口径一致）。"""
    year, week, _ = _local(ts).isocalendar()
    return f"{year}-W{week:02d}"


def fmt_left(seconds: float) -> str:
    """把剩余秒数格式化成中文可读串，如 `"3小时12分"`。

    只保留最大的两个非零单位（分/小时/天），不足一分钟回落到秒，
    避免出现"0天0小时1分"这种噪声。
    """
    total = int(seconds) if seconds > 0 else 0
    if total <= 0:
        return "0秒"
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小时")
    if minutes:
        parts.append(f"{minutes}分")
    if not parts:
        parts.append(f"{secs}秒")
    return "".join(parts[:2])
