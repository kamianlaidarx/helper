"""`content/loader` 的缓存与拒载边界（`docs/design/playhub.md` §5.2 §5.4）。

钉住三件容易改坏的事：

1. **复核不重建**：60 s 内同一路径命中缓存；`(mtime, size)` 任一变化都必须重读
   （只比时间戳会在「同一秒内改文件」时漏更新，只比大小会在「等长改写」时漏更新）。
2. **拒载口径与 `validate` 一致**：坏行 >10% 或有效题 <30 → 整类不可用；语义问题
   只剔行不拒类（`_failed_ids`），题全被剔光才报「没有可用题目」。
3. **覆盖层坏了要回落**：账号覆盖层存在但不可用时继续看仓库层，两层都不行才抛，
   且 `BankError.reason` 必须把两层的原因都串上（否则运维只看到「题库文件不存在」）。

不联网、不写仓库外路径：题库与数据库全部落在 `TemporaryDirectory` 里。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from plugins.playhub.content import loader
from plugins.playhub.core import Store

CATEGORY = "song"


def _rows(count: int, *, category: str = CATEGORY, prefix: str = "题面") -> list[dict]:
    return [
        {
            "id": f"{category}-{i:04d}",
            "v": 1,
            "q": f"{prefix}{i}：一个用来测试的题目描述",
            "a": [f"答案{i}", f"别名{i}"],
            "hint": [f"提示{i}之一", f"提示{i}之二"],
            "d": 1,
            "tag": ["测试"],
            "src": "test",
        }
        for i in range(1, count + 1)
    ]


def _write(path: Path, rows: list[dict], *, extra: tuple[str, ...] = ()) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r, ensure_ascii=False) for r in rows] + list(extra)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class LoaderBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.store = Store(self.tmp / "acct" / "playhub.sqlite3", account="acct")
        self.store.migrate()
        self.addCleanup(self.store.close)
        self.local = self.tmp / "acct" / "content" / f"{CATEGORY}.jsonl"
        self.repo = self.tmp / "repo"
        self.repo_file = self.repo / loader.CONTENT_DIR / f"{CATEGORY}.jsonl"
        loader.clear_cache()
        self.addCleanup(loader.clear_cache)

    def load(self, **kwargs):
        return loader.load_bank(self.store, CATEGORY, repo_root=self.repo, **kwargs)


class PathTests(LoaderBase):
    def test_local_override_comes_first(self) -> None:
        paths = loader.bank_paths(self.store, CATEGORY, repo_root=self.repo)
        self.assertEqual(paths[0], self.local)
        self.assertEqual(paths[1], self.repo_file)

    def test_default_repo_root_is_the_repository(self) -> None:
        paths = loader.bank_paths(self.store, CATEGORY)
        self.assertEqual(
            paths[1], loader._REPO_ROOT / loader.CONTENT_DIR / f"{CATEGORY}.jsonl"
        )
        self.assertTrue(paths[1].exists(), "随包发布的题库应当就在仓库里")


class CacheTests(LoaderBase):
    def test_same_file_is_reused_within_the_recheck_window(self) -> None:
        _write(self.local, _rows(30))
        first = self.load()
        second = self.load()
        self.assertIs(first, second)

    def test_touch_with_new_mtime_rebuilds_even_inside_the_window(self) -> None:
        path = _write(self.local, _rows(30))
        first = self.load()

        _write(path, _rows(31))
        os.utime(path, (first.path.stat().st_atime, 1_000_000_000.0))
        second = self.load()

        self.assertIsNot(second, first, "(mtime, size) 变了就必须重读")
        self.assertEqual(len(second.questions), 31)

    def test_same_size_edit_with_new_mtime_rebuilds(self) -> None:
        path = _write(self.local, _rows(30))
        first = self.load()

        rows = _rows(30, prefix="改后")
        _write(path, rows)
        # Windows 上两次连续的等长写入可能落在同一个时钟刻度里，
        # mtime 根本没变；这里显式往后推一秒，才是在考「靠 mtime 发现改动」。
        stat = path.stat()
        os.utime(path, (stat.st_atime, stat.st_mtime + 1))
        second = self.load()

        self.assertIsNot(second, first, "等长改写靠 mtime 发现，不能只看 size")
        self.assertIn("改后", second.questions[0].q)


class RejectTests(LoaderBase):
    def test_missing_files_report_missing(self) -> None:
        with self.assertRaises(loader.BankError) as caught:
            self.load()
        self.assertEqual(caught.exception.reason, "题库文件不存在")

    def test_too_few_questions_is_unusable(self) -> None:
        _write(self.local, _rows(29))
        with self.assertRaises(loader.BankError) as caught:
            self.load()
        self.assertIn("不可用", caught.exception.reason)

    def test_bad_line_ratio_over_ten_percent_rejects_the_category(self) -> None:
        # 30 有效 + 4 坏行 = 11.4% > 10%：宁可整类不上线，也不让残缺题库混进来
        _write(self.local, _rows(30), extra=("{坏行", "{\"a\": ", "[]", "null"))
        with self.assertRaises(loader.BankError) as caught:
            self.load()
        self.assertIn("不可用", caught.exception.reason)

    def test_bad_line_ratio_within_limit_keeps_the_rest(self) -> None:
        # 30 + 3 = 9.1% ≤ 10%：坏行剔掉，其余照常
        _write(self.local, _rows(30), extra=("{坏行", "not json", "[]"))
        bank = self.load()
        self.assertEqual(len(bank.questions), 30)
        self.assertEqual(bank.bad_lines, 3)

    def test_semantic_problem_drops_the_row_but_keeps_the_category(self) -> None:
        rows = _rows(30)
        rows[0]["q"] = "答案是答案1"           # 题面含答案明文
        _write(self.local, rows)
        bank = self.load()
        self.assertEqual(len(bank.questions), 29)
        self.assertEqual(bank.bad_lines, 0)
        self.assertIsNone(bank.by_id("song-0001"))

    def test_duplicate_answers_keep_only_the_first(self) -> None:
        rows = _rows(30)
        rows.append({**rows[0], "id": "song-0099"})
        _write(self.local, rows)
        bank = self.load()
        self.assertEqual(len(bank.questions), 30)
        self.assertEqual(
            sum(1 for q in bank.questions if list(q.answers) == rows[0]["a"]), 1
        )

    def test_all_rows_rejected_reports_no_usable_question(self) -> None:
        rows = _rows(30)
        for row in rows:
            row["q"] = f"答案是{row['a'][0]}"
        _write(self.local, rows)
        with self.assertRaises(loader.BankError) as caught:
            self.load()
        self.assertIn("没有可用题目", caught.exception.reason)

    def test_broken_override_falls_back_to_the_repository_layer(self) -> None:
        _write(self.local, _rows(10))              # 覆盖层不足 30 题
        _write(self.repo_file, _rows(30, prefix="仓库"))
        bank = self.load()
        self.assertEqual(bank.path, self.repo_file)
        self.assertIn("仓库", bank.questions[0].q)

    def test_both_layers_broken_reports_every_reason(self) -> None:
        _write(self.local, _rows(10))
        _write(self.repo_file, _rows(5))
        with self.assertRaises(loader.BankError) as caught:
            self.load()
        reason = caught.exception.reason
        self.assertIn(f"{CATEGORY}.jsonl", reason)
        self.assertEqual(reason.count("不可用"), 2, reason)


class BankShapeTests(LoaderBase):
    def test_by_id_finds_and_misses(self) -> None:
        _write(self.local, _rows(30))
        bank = self.load()
        self.assertIsNotNone(bank.by_id("song-0007"))
        self.assertIsNone(bank.by_id("song-9999"))

    def test_diff_bank_accepts_both_sides_of_the_edit(self) -> None:
        rows = [
            {"id": f"diff-{i:04d}", "v": 1, "a": f"今天天气很好啊{i}", "b": f"今天天气很坏啊{i}", "d": 1}
            for i in range(1, 31)
        ]
        _write(self.tmp / "acct" / "content" / "diff.jsonl", rows)
        loader.clear_cache()
        bank = loader.load_bank(self.store, "diff", repo_root=self.repo)
        self.assertEqual(len(bank.questions), 30)
        self.assertIn("好", bank.questions[0].accepted)
        self.assertIn("坏", bank.questions[0].accepted)
        self.assertIn("原句：", bank.questions[0].q)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
