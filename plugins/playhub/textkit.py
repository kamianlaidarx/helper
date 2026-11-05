"""playhub 文本归一化（`docs/design/playhub.md:495-515`）。

纯字符串操作：不依赖框架、GUI、网络与数据库。

四条取舍照文档执行：
1. **先 NFKC 再删零宽**：NFKC 可能展开兼容字符并带出新的零宽字符。
2. **不做繁简转换**、3. **不做 ASCII 大小写折叠**、4. **不剥标点**。

艾特范围重映射的实现方式：归一化与位置映射在**同一遍**里完成，
每个原字符产出多少字符当即记账，因此 span 下标不需要事后反推。
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass

ZERO_WIDTH = "\u200b\u200c\u200d\u2060\ufeff"                    # 零宽空格/连字/词连接符/BOM
SPACE_ALL = " \t\u00a0\u3000\u202f\u2005\u2009\u200a\u205f"      # 半角/制表/NBSP/全角/窄 NBSP/微信艾特后空格/薄空格/发空格/中数学空格

_ZERO = frozenset(ZERO_WIDTH)
_SPACE = frozenset(SPACE_ALL)


@dataclass(frozen=True)
class Mention:
    """一条已识别（或部分识别）的艾特范围，下标指向**归一化后**的文本。

    `uid is None` 表示结构不完整：适配层看到了 `@昵称`，但没能确认 wxid。
    """

    start: int
    end: int
    uid: str | None = None


# 文档 §2.3 的名字；保留别名，避免调用方两套名字。
MentionSpan = Mention


def strip_invisible(text: str) -> str:
    """只删零宽字符，不做 NFKC、不折空白、不 strip。"""
    return "".join(ch for ch in text if ch not in _ZERO)


def _normalize_with_map(text: str) -> tuple[str, list[int]]:
    """返回 (归一化文本, 位置映射)。

    映射长度 = len(text) + 1；`map[i]` 是原文本第 i 个字符在归一化文本中的
    起始下标，`map[len(text)]` 是结尾。被删除字符的映射指向它的产出位置
    （即下一个可见字符之前），span 端点因此不会越界。
    """
    out: list[str] = []
    pos = [0] * (len(text) + 1)
    pending_space = False

    for i, ch in enumerate(text):
        piece = "" if ch in _ZERO else unicodedata.normalize("NFKC", ch)
        visible = [c for c in piece if c not in _ZERO and c not in _SPACE]
        # 折叠后的空格只在前方已有内容、且本字符确有可见产出时落地，
        # 这样既吞掉前导空白，也不会把空格挂在 span 起点之前。
        if visible and pending_space and out:
            out.append(" ")
            pending_space = False
        pos[i] = len(out)
        for c in piece:
            if c in _ZERO:
                continue
            if c in _SPACE:
                pending_space = True
                continue
            out.append(c)

    pos[len(text)] = len(out)
    return "".join(out), pos


def normalize(text: str) -> str:
    """NFKC → 删零宽 → 空白折叠 → strip（文档 §2.6.1）。"""
    return _normalize_with_map(text)[0]


def normalize_with_mentions(
    text: str, mentions: tuple[Mention, ...] | list[Mention] = ()
) -> tuple[str, tuple[Mention, ...]]:
    """归一化文本，并把艾特范围同步重映射到归一化后的下标。

    艾特结构由适配层从**原始 text** 识别，下标对应原文；NFKC、删零宽、
    空白折叠都会改变长度，所以范围必须跟着同一步重算，不能用原 offset。
    """
    s, pos = _normalize_with_map(text)
    n = len(text)
    spans: list[Mention] = []
    for m in mentions:
        start = pos[min(max(m.start, 0), n)]
        end = pos[min(max(m.end, 0), n)]
        if end < start:
            start, end = end, start
        spans.append(Mention(start=start, end=end, uid=m.uid))
    return s, tuple(spans)
