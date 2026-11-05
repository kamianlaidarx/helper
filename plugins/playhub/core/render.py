"""小V 风格排版原语（对照 `.runtime/research/xiaov/img/` 的 66 张功能卡逐条复刻）。

设计来源是小V 的实际卡片，归纳成 6 条硬规矩，47 个功能全部按这套排：

1. **菜单卡**用 `=====图标 名称 图标=====` 起头，一行一条 `图标【信号】说明`，
   末尾可挂 `⚠注：`。小V 的 `结婚`/`股东`/`盲盒` 卡都是这个形状。
2. **结果卡**用 `@昵称` 起头（小V 每条结果都先点人），随后是分组行
   `🎁 签到内容` + 若干 `├ 项目：值`，最后一句彩蛋收尾。
3. **数值回显**永远带「当前」二字：小V 是 `当前 SVIP 值：1000`、`当前金币：1000`。
   只报增量不报余额的回复在群里无法判断是否到账。
4. **图标是语法不是装饰**：图标区分「分组 / 项目 / 提示 / 彩蛋」四类，同一类
   在同一张卡里不混用（否则 emoji 只增加噪音）。
5. **行数与字数双上限**：`limit()` 默认 16 行 / 1400 字。小V 的钓鱼卡有 35 行，
   那种长度在群里会刷屏；超限时保留头尾、中间折成一行「…（略 N 行）…」。
6. **不产生触发词**：卡片正文任何一行都不等于触发词本身（`§2.6.5` 自触发不变量），
   起头一定是装饰行或 `@昵称`，不会是裸关键词。

纯字符串操作：不依赖框架、数据库、网络，可在任何线程调用。
"""
from __future__ import annotations

from dataclasses import dataclass

RULE = "═" * 20                     # 分节线
DEFAULT_MAX_LINES = 16
DEFAULT_MAX_CHARS = 1400
ELLIPSIS = "…"


@dataclass(frozen=True)
class Card:
    """一张菜单卡：标题 + 若干行 + 可选注释/彩蛋。

    行是已经排好的字符串（含图标），本类不解释语义——排版留在这里，
    业务文案留在 `menus.py`，两边互不牵连。
    """

    icon: str
    title: str
    rows: tuple[str, ...] = ()
    note: str = ""
    tail: str = ""

    def render(self) -> str:
        lines = [banner(self.icon, self.title)]
        lines.extend(self.rows)
        if self.note:
            lines.append(rule())
            lines.append(note(self.note))
        if self.tail:
            lines.append(rule())
            lines.append(self.tail)
        return limit("\n".join(lines))


# ---------------------------------------------------------------------------
# 原子：一行一个语义
# ---------------------------------------------------------------------------
def banner(icon: str, title: str, *, pad: int = 5) -> str:
    """`=====💘结婚系统💘=====`。"""
    bar = "=" * pad
    return f"{bar}{icon}{title}{icon}{bar}"


def rule(width: int = 20) -> str:
    return RULE if width == 20 else "═" * width


def at(nickname: str) -> str:
    """结果卡第一行：小V 每条结果都以 `@昵称` 开头。"""
    name = (nickname or "").strip()
    return f"@{name}" if name else ""


def head(icon: str, text: str) -> str:
    """分组标题，如 `🎁 签到内容`。"""
    return f"{icon} {text}"


def row(icon: str, text: str) -> str:
    """项目行，如 `├ 金币：+1000`。"""
    return f"{icon}{text}"


def field(icon: str, label: str, value: object) -> str:
    """带标签的项目行：`💰 金币：+30`。"""
    return f"{icon}{label}：{value}"


def current(icon: str, label: str, value: object) -> str:
    """余额回显，固定带「当前」：`💰 当前金币：130`。"""
    return f"{icon}当前{label}：{value}"


def cmd(icon: str, signature: str, desc: str = "") -> str:
    """菜单条目：`❤【@xX 求婚】向对方求婚`。"""
    body = f"【{signature}】"
    return f"{icon}{body}{desc}" if desc else f"{icon}{body}"


def note(text: str) -> str:
    return f"⚠注：{text}"


def tip(icon: str, text: str) -> str:
    return f"{icon} 提示：{text}"


def quote(icon: str, text: str) -> str:
    """成段文字（签文、文案、攻略），按原文换行保留。"""
    body = str(text).strip()
    return "\n".join(f"{icon} {line}" if i == 0 else line for i, line in enumerate(body.splitlines()))


# ---------------------------------------------------------------------------
# 组装：菜单卡 / 结果卡
# ---------------------------------------------------------------------------
def menu(icon: str, title: str, rows=(), *, note_text: str = "", tail: str = "") -> str:
    return Card(icon=icon, title=title, rows=tuple(rows), note=note_text, tail=tail).render()


def result(
    nickname: str,
    headline: str,
    rows=(),
    *,
    tail: str = "",
    at_sender: bool = True,
) -> str:
    """结果卡：`@昵称` → 主行 → 明细行 → 彩蛋。"""
    lines: list[str] = []
    if at_sender:
        who = at(nickname)
        if who:
            lines.append(who)
    if headline:
        lines.append(headline)
    lines.extend(rows)
    if tail:
        lines.append(tail)
    return limit("\n".join(lines))


def limit(
    text: str,
    *,
    max_lines: int = DEFAULT_MAX_LINES,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """行数/字数双上限；超限保留头尾，中间折成一行。"""
    if not text:
        return ""
    lines = text.split("\n")
    if len(lines) > max_lines:
        keep = max_lines - 1
        head_n = (keep + 1) // 2
        tail_n = keep - head_n
        dropped = len(lines) - head_n - tail_n
        lines = lines[:head_n] + [f"{ELLIPSIS}（略 {dropped} 行）{ELLIPSIS}"] + lines[-tail_n:]
    out = "\n".join(line.rstrip() for line in lines)
    if len(out) > max_chars:
        out = out[: max_chars - 1] + ELLIPSIS
    return out
