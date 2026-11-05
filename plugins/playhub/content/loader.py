"""题库加载器：懒加载 + 60 s 复核 + 与 `validate.py` 同一套结构校验
（`docs/design/playhub.md:1409-1428` §5.4、`:1376-1386` §5.2）。

三条硬规矩：

1. **路径优先级不做合并**：① `<账号工作区>/.runtime/playhub/<hash>/content/<cat>.jsonl`
   （运维手工覆盖）→ ② `<仓库根>/content/playhub/<cat>.jsonl`（随包发布）。
   命中即**完全覆盖**，与 `engines/daily.py:178-189` 的签文库口径一致。
2. **校验复用**：能不能用、坏行比例、类目门槛全部交给
   `content/validate.check_category()`，本模块只把「有问题的行」剔掉。
   两处各写一套校验，早晚会出现「CLI 说 OK、运行时说坏」的鬼故事。
3. **复核不重建**：同一路径 60 s 内命中缓存；`(mtime, size)` 变了才重读。
   群里每条消息都读盘会把回调线程拖死，而题库是低频变更数据。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

from .validate import (
    DIFF_CATEGORIES,
    CategoryReport,
    check_category,
    normalize,
)

# 仓库根：plugins/playhub/content/loader.py → parents[3]
_REPO_ROOT = Path(__file__).resolve().parents[3]

CONTENT_DIR = "content/playhub"
RECHECK_SECONDS = 60.0
_BAD_LINE_REASON = "坏行比例超限或题数不足"

_log = logging.getLogger("playhub.content.loader")


class BankError(Exception):
    """题库不可用。`reason` 直接进用户回包，必须是人话。"""

    def __init__(self, category: str, reason: str) -> None:
        super().__init__(f"{category}: {reason}")
        self.category = category
        self.reason = reason


@dataclass(frozen=True)
class Quiz:
    """选择题/问答题（`a` 是等价答案数组，`hint` 逐级提示）。"""

    id: str
    category: str
    q: str
    answers: tuple[str, ...]
    hints: tuple[str, ...]
    d: int
    tag: tuple[str, ...]

    kind = "quiz"

    @property
    def accepted(self) -> tuple[str, ...]:
        """可接受的归一化答案集合。"""
        return tuple(n for n in (normalize(a) for a in self.answers) if n)

    @property
    def display(self) -> str:
        """公布答案时的展示文本。"""
        return self.answers[0] if self.answers else ""


@dataclass(frozen=True)
class Diff:
    """文字找茬：`a` 原句、`b` 改句，答案由编辑块算出（无需人工标注）。"""

    id: str
    category: str
    a: str
    b: str
    d: int

    kind = "diff"

    @property
    def q(self) -> str:
        """统一题面字段：找茬把两句都摆出来。"""
        return f"原句：{self.a}\n改句：{self.b}"

    @property
    def hints(self) -> tuple[str, ...]:
        return ()

    @property
    def accepted(self) -> tuple[str, ...]:
        """改动的那些片段，原句侧与改句侧都算对。"""
        segments: list[str] = []
        for tag, i1, i2, j1, j2 in SequenceMatcher(None, self.a, self.b).get_opcodes():
            if tag == "equal":
                continue
            for segment in (self.a[i1:i2], self.b[j1:j2]):
                segment = segment.strip()
                if segment:
                    segments.append(normalize(segment))
        # 去重保序：同一处改动可能两侧都给出同一片段
        seen: set[str] = set()
        return tuple(s for s in segments if s and not (s in seen or seen.add(s)))

    @property
    def display(self) -> str:
        return "、".join(self.accepted) or "（这道题没有可用于判定的差异）"


@dataclass(frozen=True)
class Bank:
    """一个类目的可用题目集合。"""

    category: str
    path: Path
    questions: tuple[Any, ...]
    total: int
    bad_lines: int

    def by_id(self, question_id: str):
        for question in self.questions:
            if question.id == question_id:
                return question
        return None


@dataclass(frozen=True)
class _CacheEntry:
    mtime: float
    size: int
    bank: Bank
    checked_at: float


_CACHE: dict[Path, _CacheEntry] = {}


def bank_paths(store, category: str, *, repo_root: Path | None = None) -> tuple[Path, ...]:
    """两条候选路径，按优先级排列（§5.2：账号覆盖 → 仓库默认）。"""
    local = Path(store.db_path).parent / "content" / f"{category}.jsonl"
    root = Path(repo_root) if repo_root is not None else _REPO_ROOT
    return (local, root / CONTENT_DIR / f"{category}.jsonl")


def _failed_ids(report: CategoryReport) -> set[str]:
    """校验报出问题的条目 id；这些行不进运行时题库。"""
    return {p.item_id for p in report.problems if p.item_id}


def _build(report: CategoryReport, path: Path) -> Bank:
    """按校验报告剔除坏行、去重后构造 `Bank`。"""
    bad_ids = _failed_ids(report)
    is_diff = report.category in DIFF_CATEGORIES
    questions: list[Any] = []
    seen_answers: set[str] = set()
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise BankError(report.category, f"读不了题库文件（{exc.strerror or exc}）") from exc

    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue                      # 坏行已计入 report，跳过
        if not isinstance(row, dict) or "__error__" in row:
            continue
        item_id = row.get("id")
        if not isinstance(item_id, str) or item_id in bad_ids:
            continue
        if is_diff:
            questions.append(
                Diff(
                    id=item_id,
                    category=report.category,
                    a=row.get("a", ""),
                    b=row.get("b", ""),
                    d=row.get("d", 1),
                )
            )
            continue
        answers = tuple(a.strip() for a in row.get("a") or () if isinstance(a, str) and a.strip())
        key = "|".join(sorted(normalize(a) for a in answers))
        if not answers or key in seen_answers:   # 去重键同 validate（category, sorted(a)）
            continue
        seen_answers.add(key)
        hints = tuple(h.strip() for h in row.get("hint") or () if isinstance(h, str) and h.strip())
        tags = tuple(t for t in row.get("tag") or () if isinstance(t, str))
        questions.append(
            Quiz(
                id=item_id,
                category=report.category,
                q=row.get("q", ""),
                answers=answers,
                hints=hints,
                d=row.get("d", 1),
                tag=tags,
            )
        )

    return Bank(
        category=report.category,
        path=path,
        questions=tuple(questions),
        total=report.total,
        bad_lines=report.bad_lines,
    )


def load_bank(
    store,
    category: str,
    *,
    repo_root: Path | None = None,
    now_fn: Callable[[], float] | None = None,
) -> Bank:
    """取一个类目的题库；不可用抛 `BankError`。

    `store` 只用来定位账号工作区（`store.db_path`），不碰数据库。
    """
    clock_fn = now_fn if now_fn is not None else time.monotonic
    reasons: list[str] = []

    for path in bank_paths(store, category, repo_root=repo_root):
        if not path.exists():
            continue
        try:
            stat = path.stat()
        except OSError as exc:
            reasons.append(f"{path.name} 读取失败（{exc.strerror or exc}）")
            continue

        entry = _CACHE.get(path)
        if (
            entry is not None
            and entry.mtime == stat.st_mtime
            and entry.size == stat.st_size
            and clock_fn() - entry.checked_at < RECHECK_SECONDS
        ):
            return entry.bank

        report = check_category(path.parent, category)
        if not report.usable:
            reasons.append(f"{path.name} 不可用（{_BAD_LINE_REASON}）")
            _log.warning(
                "题库不可用：category=%s path=%s 已载入=%s 门槛=%s 坏行=%s/%s 问题=%s",
                category, path, report.loaded, "见 validate.MIN_QUESTIONS",
                report.bad_lines, report.total, len(report.problems),
            )
            continue

        bank = _build(report, path)
        if not bank.questions:
            reasons.append(f"{path.name} 没有可用题目")
            continue
        _CACHE[path] = _CacheEntry(
            mtime=stat.st_mtime, size=stat.st_size, bank=bank, checked_at=clock_fn()
        )
        return bank

    raise BankError(category, "；".join(reasons) or "题库文件不存在")


def clear_cache() -> None:
    """清空缓存（测试与运维换题库时用）。"""
    _CACHE.clear()
