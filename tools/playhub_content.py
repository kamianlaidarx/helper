"""题库离线校验入口。

用法：
    python tools/playhub_content.py check [--verbose] [--root content/playhub]
    python tools/playhub_content.py stats
    python tools/playhub_content.py dedupe

薄壳：全部逻辑在 `plugins/playhub/content/validate.py`，本文件只负责参数与输出。
只读，不会修改题库文件。
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VALIDATE_PATH = REPO_ROOT / "plugins" / "playhub" / "content" / "validate.py"
DEFAULT_CONTENT = REPO_ROOT / "content" / "playhub"


def _load_validate():
    spec = importlib.util.spec_from_file_location("playhub_content_validate", VALIDATE_PATH)
    if spec is None or spec.loader is None:
        raise SystemExit(f"无法加载校验模块：{VALIDATE_PATH}")
    module = importlib.util.module_from_spec(spec)
    # dataclasses 在解析注解时会回查 sys.modules，必须先登记再执行
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="playhub 题库校验（只读）")
    parser.add_argument("command", choices=("check", "stats", "dedupe"))
    parser.add_argument("--root", default=str(DEFAULT_CONTENT), help="题库目录")
    parser.add_argument("--verbose", "-v", action="store_true", help="列出全部条目而非仅问题")
    args = parser.parse_args(argv)

    validate = _load_validate()
    root = Path(args.root)
    if not root.is_dir():
        print(f"题库目录不存在：{root}", file=sys.stderr)
        return 2

    reports = validate.check_all(root)

    if args.command == "stats":
        total_items = sum(r.loaded for r in reports)
        total_bytes = sum(r.path.stat().st_size for r in reports if r.path.exists())
        print(f"类目 {len(reports)} 个，共 {total_items} 题，{total_bytes} B")
        for r in reports:
            size = r.path.stat().st_size if r.path.exists() else 0
            print(f"  {r.category:<8} {r.loaded:>4} 题  {size:>7} B  可用={r.usable}")
        return 0

    problems = [(r.category, p) for r in reports for p in r.problems]

    if args.command == "dedupe":
        dupes = [(c, p) for c, p in problems if "重复" in p.message]
        if not dupes:
            print("没有重复项。")
            return 0
        for category, p in dupes:
            print(f"{category}: {p}")
        return 1

    print(validate.format_report(reports, verbose=args.verbose))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
