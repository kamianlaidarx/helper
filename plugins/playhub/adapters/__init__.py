"""playhub 适配层：把框架对象包成引擎能用、且**拿不到框架内部**的最小接口。

本批只有一个适配器 `memberfeed`（群角色读取）。慢活执行器 `executor.py`
属第 3 批，不在这里出现。
"""
from __future__ import annotations

from .memberfeed import MemberFeed

__all__ = ["MemberFeed"]
