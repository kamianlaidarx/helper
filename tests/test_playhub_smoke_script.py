"""`tools/playhub_smoke.py` 自身必须能跑通：退出码 0，且结构化结果自洽。

两种调用方式都覆盖：

- `import`：拿到 `Report`，直接检查 57 个触发词、50 条闲聊、零误回复；
- `subprocess`：模拟同事在命令行敲 `.venv/bin/python tools/playhub_smoke.py`，
  断言退出码 0 且 stdout 里有摘要。

两种方式都用临时库，不碰仓库里的 `playhub.sqlite3`。
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import subprocess
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "tools" / "playhub_smoke.py"


def load_smoke_module():
    """按文件路径加载脚本（`tools/` 不是包，不能直接 import）。"""
    spec = importlib.util.spec_from_file_location("playhub_smoke", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclass 需要能从 sys.modules 里查到自己的模块，先登记再执行。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SmokeScriptImportTests(unittest.TestCase):
    """import 方式：直接读结构化结果。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.smoke = load_smoke_module()
        cls.report = cls.smoke.run_smoke()

    def test_script_exists(self) -> None:
        self.assertTrue(SCRIPT.is_file(), f"缺少冒烟脚本：{SCRIPT}")

    def test_keyword_count_is_57(self) -> None:
        self.assertEqual(self.report.total_keywords, 57)
        self.assertEqual(self.report.total_keywords, self.smoke.kw_mod.KEYWORD_COUNT)

    def test_every_keyword_was_probed(self) -> None:
        probed = [row.keyword for row in self.report.rows]
        self.assertEqual(probed, list(self.smoke.kw_mod.TABLE.keywords))
        self.assertEqual(len(set(probed)), 57, "触发词不该重复")

    def test_no_keyword_raised_or_fell_into_error_text(self) -> None:
        broken = {row.keyword: row.error for row in self.report.rows if row.error}
        self.assertEqual(broken, {}, f"这些触发词没跑通：{broken}")

    def test_reply_presence_matches_design(self) -> None:
        mismatched = [
            (row.keyword, row.replied, row.expected_silent)
            for row in self.report.rows
            if not row.ok
        ]
        self.assertEqual(mismatched, [], "有触发词的静默/回包与设计不符")
        self.assertEqual(self.report.replied + self.report.silent, 57)

    def test_chit_chat_gets_no_reply(self) -> None:
        self.assertEqual(self.report.chit_chat_sent, 50, "应当发满 50 条闲聊")
        self.assertEqual(self.report.chit_chat_replies, [], "闲聊被插件接话了")
        self.assertEqual(len(self.smoke.CHIT_CHAT), 50)
        self.assertEqual(len(set(self.smoke.CHIT_CHAT)), 50, "闲聊样本不该重复")

    def test_report_is_green(self) -> None:
        self.assertEqual(self.report.problems, [])
        self.assertTrue(self.report.ok)

    def test_smoke_leaves_no_files_behind(self) -> None:
        """临时库在 TemporaryDirectory 里，跑完不该在仓库留下 sqlite 文件。"""
        leftovers = sorted(p.name for p in REPO_ROOT.glob("smoke*.sqlite3"))
        self.assertEqual(leftovers, [])


class SmokeScriptSubprocessTests(unittest.TestCase):
    """命令行方式：与同事手动执行完全一致。"""

    def test_exit_code_is_zero(self) -> None:
        # Windows 上 `text=True` 会按本地代码页（cp936）解码子进程序列，而脚本输出是 UTF-8：
        # 一方面显式指定 encoding，另一方面用 PYTHONIOENCODING 让子进程稳定按 UTF-8 写。
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0, f"冒烟脚本退出码非 0：\n{proc.stdout}\n{proc.stderr}")
        self.assertIn("冒烟通过", proc.stdout)
        self.assertIn("触发词：57", proc.stdout)
        self.assertIn("误回复 0 条", proc.stdout)
        self.assertIn("有回复：", proc.stdout)


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
