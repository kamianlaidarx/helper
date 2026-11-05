"""playhub 关键词路由：归一化 → 整词精确匹配 → 只拆首部第一个分隔符
（`docs/design/playhub.md:517-594`）。

三条不可松动的规则：
1. `head` 是"整段无空白文本"，所以只可能是精确命中，**不存在包含匹配**。
2. 分隔符只有三种：空白、`#`、结构化艾特；且**只拆命令首部第一个**。
3. 未命中返回 `None`，调用方零回复、零日志噪音。

本模块**不查动作绑定、不执行裸触发策略**：前者是 `registry.dispatch` 的分支，
后者要按 `Trigger.bare_behavior` 由 `app` 层决定（见 `registry.py` 顶部说明）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .keywords import TABLE, KeywordTable
from .textkit import Mention, normalize_with_mentions

MAX_COMMAND_LEN = 64
Separator = Literal["space", "#", "@"]


@dataclass(frozen=True)
class Command:
    """一次命中的命令（字段与顺序照 §2.3 / §2.6.2）。"""

    name: str        # 命中的触发词（归一化后原词，如 "签到" / "宝宝"）
    action: str      # 引擎内动作 id，如 "daily.signin" / "baby.view"
    raw: str         # 原始整行（发送前的原文，仅用于日志/审计）
    args: str        # 首部之后的剩余文本（已 strip），参数内的 `#` 原样保留
    tokens: tuple[str, ...]   # args 的空白切分，不剥 `@` 前缀
    had_arg: bool    # 是否带参数；区分「宝宝」（裸发）与「宝宝 看」
    separator: Separator | None   # 首部分隔符；裸命令为 None
    mentions: tuple[Mention, ...]  # 本条消息已识别的艾特范围（升序、不重叠）


def mention_starts_at(mentions: tuple[Mention, ...], index: int) -> bool:
    """某个规范化下标是否是某条艾特范围的起点。"""
    return any(m.start == index for m in mentions)


def _split_head(s: str, mentions: tuple[Mention, ...]) -> tuple[int | None, Separator | None]:
    """单次扫描命令首部，返回 (切点, 分隔符)；整段无分隔符时返回 (None, None)。"""
    i = 0
    while i < len(s):
        if mention_starts_at(mentions, i):
            return i, "@"           # 艾特整体留在参数里
        ch = s[i]
        if ch == " ":
            return i, "space"
        if ch in "#＃":
            return i, "#"
        if ch in "@＠":             # 兼容无空格手打 @
            return i, "@"
        i += 1
    return None, None


def route(
    text: str,
    mentions: tuple[Mention, ...] | list[Mention] = (),
    *,
    table: KeywordTable | None = None,
) -> Command | None:
    """解析一条群消息；不是命令就返回 `None`。

    `mentions` 的下标指向**原始 text**，由 `normalize_with_mentions` 统一重映射。
    """
    table = TABLE if table is None else table
    s, spans = normalize_with_mentions(text, tuple(mentions))
    if not s or len(s) > MAX_COMMAND_LEN:      # 超过 64 字不可能是「命令 + 少量参数」
        return None
    if mention_starts_at(spans, 0) or s.startswith("@"):   # 句首艾特 = 对人说话
        return None

    cut, sep = _split_head(s, spans)
    head = s if cut is None else s[:cut]
    trig = table.head_index.get(head)
    if trig is None:
        return None

    if cut is None:
        args = ""
    elif sep == "@":
        args = s[cut:].strip()                 # 艾特连同 @ 一起给引擎
    else:
        args = s[cut + 1:].strip()             # 只消耗这一个首部分隔符

    return Command(
        name=head,
        action=trig.action,
        raw=text,
        args=args,
        tokens=tuple(args.split()),            # 不再 split("#")、不再 lstrip("@")
        had_arg=bool(args),
        separator=sep,
        mentions=spans,
    )
