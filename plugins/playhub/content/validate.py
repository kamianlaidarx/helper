"""题库校验：字段、id 唯一性、答案去重、题面泄漏答案。

被 `tools/playhub_content.py` 以文件路径方式加载，**只读**：本模块不写任何文件，
也不自动修正题库，只报告问题。
"""

from __future__ import annotations

import difflib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

ID_RE = re.compile(r"^[a-z]+-(\d{4})$")
MAX_Q = 200
MAX_A = 8
MAX_HINT = 3
MIN_ANSWERS = 1
MIN_QUESTIONS = 30          # 类目可用门槛
BAD_LINE_RATIO = 0.10       # 坏行比例上限

QUIZ_CATEGORIES = (
    "chengyu", "riddle", "trivia", "song", "movie", "star",
    "animal", "plant", "food", "honor", "anime", "guoman",
)
DIFF_CATEGORIES = ("diff",)
ALL_CATEGORIES = QUIZ_CATEGORIES + DIFF_CATEGORIES

_PUNCT = set("，。、；：？！“”‘’（）《》〈〉【】…—～·,.;:?!\"'()<>[]{}`~-_/\\|")


def normalize(text: str) -> str:
    """NFKC + 去空白 + 去标点 + ASCII 小写。不引入 opencc，不做繁简转换。"""
    out = unicodedata.normalize("NFKC", text)
    out = "".join(ch for ch in out if not ch.isspace() and ch not in _PUNCT)
    return out.lower()


def leaks(text: str, answer_norm: str) -> bool:
    """题面/提示是否泄漏答案。

    短英文答案（如 `Au`）用词边界匹配，否则 `aurum` 这类正常词汇会被误判。
    """
    if not answer_norm:
        return False
    haystack = normalize(text)
    if not haystack:
        return False
    if answer_norm.isascii() and len(answer_norm) <= 3:
        return re.search(rf"(?<![a-z0-9]){re.escape(answer_norm)}(?![a-z0-9])", haystack) is not None
    return answer_norm in haystack


@dataclass
class Problem:
    category: str
    line_no: int
    item_id: str
    field: str
    message: str

    def __str__(self) -> str:
        where = f"{self.category}.jsonl:{self.line_no}"
        if self.item_id:
            where += f" [{self.item_id}]"
        return f"{where} {self.field}: {self.message}"


@dataclass
class CategoryReport:
    category: str
    path: Path
    total: int = 0
    loaded: int = 0
    bad_lines: int = 0
    problems: list[Problem] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return (
            self.loaded >= MIN_QUESTIONS
            and self.total > 0
            and self.bad_lines / max(self.total, 1) <= BAD_LINE_RATIO
        )


def _read_lines(path: Path) -> tuple[list[tuple[int, dict]], int, int]:
    """返回 (有效条目, 总行数, 坏行数)。空行不计入。"""
    items: list[tuple[int, dict]] = []
    total = 0
    bad = 0
    with path.open("r", encoding="utf-8") as fp:
        for line_no, raw in enumerate(fp, 1):
            raw = raw.strip()
            if not raw:
                continue
            total += 1
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError as exc:
                bad += 1
                items.append((line_no, {"__error__": f"JSON 解析失败：{exc.msg}"}))
                continue
            if not isinstance(obj, dict):
                bad += 1
                items.append((line_no, {"__error__": "不是 JSON 对象"}))
                continue
            items.append((line_no, obj))
    return items, total, bad


def _check_quiz(category: str, line_no: int, obj: dict, report: CategoryReport) -> None:
    problem = lambda field, msg: report.problems.append(  # noqa: E731
        Problem(category, line_no, str(obj.get("id", "")), field, msg)
    )

    if "__error__" in obj:
        problem("-", obj["__error__"])
        return

    item_id = obj.get("id")
    if not isinstance(item_id, str):
        problem("id", "缺失或不是字符串")
    else:
        m = ID_RE.match(item_id)
        if not m:
            problem("id", f"格式应为 <类目>-0001，实际 {item_id!r}")
        elif item_id.split("-")[0] != category:
            problem("id", f"前缀与类目不符（{item_id}）")

    q = obj.get("q")
    if not isinstance(q, str) or not q.strip():
        problem("q", "缺失或为空")
        q = ""
    elif len(q) > MAX_Q:
        problem("q", f"超过 {MAX_Q} 字（{len(q)}）")

    answers = obj.get("a")
    norm_answers: list[str] = []
    if not isinstance(answers, list):
        problem("a", "缺失或不是数组")
        answers = []
    elif not (MIN_ANSWERS <= len(answers) <= MAX_A):
        problem("a", f"答案个数应在 {MIN_ANSWERS}–{MAX_A}（{len(answers)}）")
    for i, ans in enumerate(answers):
        if not isinstance(ans, str) or not ans.strip():
            problem("a", f"第 {i + 1} 项为空")
            continue
        n = normalize(ans)
        if not n:
            problem("a", f"第 {i + 1} 项归一化后为空")
            continue
        if n in norm_answers:
            problem("a", f"第 {i + 1} 项与前面的答案等价（{ans}）")
        norm_answers.append(n)

    hint = obj.get("hint")
    if hint is not None:
        if not isinstance(hint, list):
            problem("hint", "不是数组")
        elif len(hint) > MAX_HINT:
            problem("hint", f"超过 {MAX_HINT} 项（{len(hint)}）")
        else:
            for i, h in enumerate(hint):
                if not isinstance(h, str) or not h.strip():
                    problem("hint", f"第 {i + 1} 项为空")
                elif any(leaks(h, n) for n in norm_answers):
                    problem("hint", f"第 {i + 1} 项含答案明文")

    d = obj.get("d", 1)
    if not isinstance(d, int) or isinstance(d, bool) or not (1 <= d <= 3):
        problem("d", f"应为 1–3 的整数（{d!r}）")

    tag = obj.get("tag")
    if tag is not None and (not isinstance(tag, list) or any(not isinstance(t, str) for t in tag)):
        problem("tag", "应为字符串数组")

    src = obj.get("src")
    if src is not None and not isinstance(src, str):
        problem("src", "应为字符串")

    # 题面不得包含答案明文
    norm_q = normalize(q)
    for n in norm_answers:
        if leaks(q, n):
            problem("q", "题面含答案明文")
            break

    if norm_q and norm_q in norm_answers:
        problem("q", "题面本身就是答案")


def _check_diff(category: str, line_no: int, obj: dict, report: CategoryReport) -> None:
    problem = lambda field, msg: report.problems.append(  # noqa: E731
        Problem(category, line_no, str(obj.get("id", "")), field, msg)
    )
    if "__error__" in obj:
        problem("-", obj["__error__"])
        return

    item_id = obj.get("id")
    if not isinstance(item_id, str) or not ID_RE.match(item_id):
        problem("id", f"格式应为 diff-0001，实际 {item_id!r}")
    elif item_id.split("-")[0] != category:
        problem("id", f"前缀与类目不符（{item_id}）")

    a, b = obj.get("a"), obj.get("b")
    if not isinstance(a, str) or not a.strip():
        problem("a", "缺失或为空")
        a = ""
    if not isinstance(b, str) or not b.strip():
        problem("b", "缺失或为空")
        b = ""
    if a and b:
        if a == b:
            problem("b", "与 a 完全相同，没有差异")
        elif abs(len(a) - len(b)) > 6:
            problem("b", f"长度差过大（{len(a)} vs {len(b)}）")
        else:
            # 按编辑块比对，避免「删一个字导致后面整体错位」被算成多处差异
            blocks = [
                (i1, i2, j1, j2)
                for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes()
                if tag != "equal"
            ]
            changed = sum(max(i2 - i1, j2 - j1) for i1, i2, j1, j2 in blocks)
            if len(blocks) > 3:
                problem("b", f"差异块过多（{len(blocks)} 处）")
            elif changed > 6:
                problem("b", f"改动字符过多（{changed} 个）")

    d = obj.get("d", 1)
    if not isinstance(d, int) or isinstance(d, bool) or not (1 <= d <= 3):
        problem("d", f"应为 1–3 的整数（{d!r}）")


def check_category(root: Path, category: str) -> CategoryReport:
    path = root / f"{category}.jsonl"
    report = CategoryReport(category=category, path=path)
    if not path.exists():
        report.problems.append(Problem(category, 0, "", "-", "文件不存在"))
        return report

    items, total, bad = _read_lines(path)
    report.total = total
    report.bad_lines = bad
    checker = _check_diff if category in DIFF_CATEGORIES else _check_quiz

    seen_ids: dict[str, int] = {}
    seen_answers: dict[str, tuple[int, str]] = {}
    for line_no, obj in items:
        checker(category, line_no, obj, report)
        if "__error__" in obj:
            continue
        report.loaded += 1

        item_id = obj.get("id")
        if isinstance(item_id, str):
            if item_id in seen_ids:
                report.problems.append(
                    Problem(category, line_no, item_id, "id",
                            f"与第 {seen_ids[item_id]} 行重复")
                )
            else:
                seen_ids[item_id] = line_no

        if category not in DIFF_CATEGORIES:
            answers = obj.get("a")
            if isinstance(answers, list):
                key = "|".join(sorted(normalize(a) for a in answers if isinstance(a, str)))
                if key:
                    if key in seen_answers:
                        prev_line, prev_id = seen_answers[key]
                        report.problems.append(
                            Problem(category, line_no, str(item_id), "a",
                                    f"答案与第 {prev_line} 行（{prev_id}）重复")
                        )
                    else:
                        seen_answers[key] = (line_no, str(item_id))
    return report


def check_all(root: Path) -> list[CategoryReport]:
    return [check_category(root, cat) for cat in ALL_CATEGORIES]


def format_report(reports: list[CategoryReport], *, verbose: bool = False) -> str:
    lines: list[str] = []
    total_problems = sum(len(r.problems) for r in reports)
    total_items = sum(r.loaded for r in reports)
    total_bytes = sum(r.path.stat().st_size for r in reports if r.path.exists())

    for r in reports:
        flag = "OK  " if r.usable and not r.problems else ("可用" if r.usable else "不足")
        size = r.path.stat().st_size if r.path.exists() else 0
        lines.append(
            f"{r.category:<8} {r.loaded:>4} 题  {size:>7} B  "
            f"坏行 {r.bad_lines:>2}  问题 {len(r.problems):>3}  {flag}"
        )
        if verbose or r.problems:
            for p in r.problems:
                lines.append(f"    - {p}")

    lines.append("")
    lines.append(f"合计 {total_items} 题，{total_bytes} B，问题 {total_problems} 条")
    missing = [r.category for r in reports if not r.path.exists()]
    if missing:
        lines.append("缺失文件：" + "、".join(missing))
    thin = [f"{r.category}({r.loaded})" for r in reports if r.path.exists() and r.loaded < MIN_QUESTIONS]
    if thin:
        lines.append(f"未达 {MIN_QUESTIONS} 题门槛：" + "、".join(thin))
    return "\n".join(lines)
