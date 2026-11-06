#!/usr/bin/env python3
"""playhub 离线冒烟：不连微信、不碰线上库，把 62 个触发词和 50 条闲聊各跑一遍。

做什么
------
1. 在临时目录建一个全新的 SQLite（`Store(..., account="smoke")` + `migrate()`）；
2. 从代码的触发词注册表 `plugins/playhub/keywords.py` 的 `TABLE` **程序化**取出全部
   触发词（不手抄），断言数量就是 `KEYWORD_COUNT`（62）；
3. 每个触发词裸发一次，记录：有没有回包、有没有抛异常、有没有落到 `ERROR_TEXT`；
4. 换一个全新房间发 50 条内置闲聊（脚本自带样本，且脚本会自检它们不含任何触发词），
   要求零回包——群里的日常聊天不该被插件接话；
5. 打印摘要表，按结果给退出码（全部通过 0，否则 1）。

安全边界
--------
- 数据库在 `tempfile.TemporaryDirectory()` 里，退出即删；
- 断言 `main.py` 的 `--check` 之外不启动任何东西：本脚本只 import playhub；
- 时钟固定（2026-11-01 12:00 Asia/Shanghai）、随机数固定种子，结果可复现；
- 不 sleep、不发网络请求（playhub 的 outbound 默认 OfflineProvider）。

用法
----
    .venv/bin/python tools/playhub_smoke.py
"""
from __future__ import annotations

import itertools
import random
import sys
import tempfile
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:      # 直接 `python tools/playhub_smoke.py` 时要能 import
    sys.path.insert(0, str(REPO_ROOT))

from plugins.playhub import keywords as kw_mod                      # noqa: E402
from plugins.playhub.app import ACTION_SWITCH, ERROR_TEXT, App      # noqa: E402
from plugins.playhub.core import Store, menus                       # noqa: E402
from plugins.playhub.core.clock import CN                           # noqa: E402

SMOKE_ROOM = "99999999@chatroom"
CHAT_ROOM = "88888888@chatroom"
SMOKE_UID = "wxid_smoke"
SMOKE_NAME = "冒烟员"
SEED = 20261101
CLOCK_DAY = "2026-11-01"

#: 50 条日常闲聊：不带任何触发词，也不该被插件接话。
CHIT_CHAT: tuple[str, ...] = (
    "吃了吗", "今天好热", "哈哈哈", "早上好", "外面风挺大",
    "下班了", "晚安", "谁在", "我回来了", "笑死我了",
    "周末去哪玩", "收到", "好的", "谢谢", "人呢",
    "在忙", "开会中", "吃饭去", "刚睡醒", "有点困",
    "这个不错", "真的假的", "牛", "厉害", "我服了",
    "无语", "随便", "都行", "看情况", "再说吧",
    "你咋不上天", "明天见", "加油", "稳住", "我先撤了",
    "等我一下", "马上到", "路上堵", "公交太挤", "打车贵",
    "手机没电了", "充电中", "信号不好", "你说啥", "没听清",
    "再说一遍", "有意思", "笑死", "好家伙", "行吧",
)


class FakeMsg:
    """最小 Message 替身：playhub 只读这几个字段（wxbot_framework/sdk.py:24-37）。

    每条消息都拿一个新的 `native_message_id`：冒烟里「签到」和「打卡」是同一
    action 的两次**不同**投递，若共用一个消息身份，§4.2.10 的消息级幂等会把
    后一个当重投吞掉（静默），冒烟就会误报成「该有回复却没回」。
    """

    _ids = itertools.count(1)

    def __init__(self, text: str, *, chat_id: str = SMOKE_ROOM, sender_id: str = SMOKE_UID,
                 nickname: str = SMOKE_NAME) -> None:
        self.text = text
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.nickname = nickname
        self.is_self = False
        self.sequence = next(self._ids)
        self.session_id = "smoke"
        self.data = {
            "sender_wechat_name": nickname,
            "native_message_id": f"smoke-{self.sequence:04d}",
        }


class FakeMember:
    def __init__(self, wxid: str) -> None:
        self.wxid = wxid


class FakeMembers:
    """成员反查：只认冒烟员自己，其余名字一律 KeyError → 走「没认出」分支。"""

    def __init__(self) -> None:
        self._names = {SMOKE_NAME: SMOKE_UID}

    def resolve(self, name, room=None):
        key = (name or "").lstrip("@＠")
        if key not in self._names:
            raise KeyError(key)
        return FakeMember(self._names[key])


class FakeCtx:
    def __init__(self) -> None:
        self.members = FakeMembers()


class FakeClock:
    def __init__(self, day: str = CLOCK_DAY) -> None:
        self.ts = int(datetime.fromisoformat(day).replace(hour=12, tzinfo=CN).timestamp())

    def __call__(self) -> int:
        return self.ts


@dataclass
class Row:
    """一个触发词的冒烟结果。"""

    keyword: str
    action: str
    replied: bool
    expected_silent: bool
    detail: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        if self.error:
            return False
        return self.replied is not self.expected_silent


@dataclass
class Report:
    total_keywords: int = 0
    rows: list[Row] = field(default_factory=list)
    chit_chat_sent: int = 0
    chit_chat_replies: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def replied(self) -> int:
        return sum(1 for row in self.rows if row.replied)

    @property
    def silent(self) -> int:
        return sum(1 for row in self.rows if not row.replied)

    @property
    def failures(self) -> list[Row]:
        return [row for row in self.rows if not row.ok]

    @property
    def ok(self) -> bool:
        return not self.problems and not self.failures and not self.chit_chat_replies


def _expects_silence(trigger, *, switches_on: frozenset[str] = frozenset()) -> bool:
    """裸词「不该有回包」的判定，和 `app.handle_message` 的短路顺序对齐：

    1. 高危开关关闭 → app 直接 return None（排在说明书卡之前）；
    2. 否则 `bare_behavior == "silent"` 且没有说明书卡 → None；
    3. 其余（自动执行 / 回说明书卡）都应当有回包。
    """
    gate = ACTION_SWITCH.get(trigger.action)
    if gate is not None and gate not in switches_on:
        return True
    if trigger.bare_behavior != "silent":
        return False
    return menus.menu_for(trigger.action) is None


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:48]
    return "(空)"


def run_smoke(*, verbose: bool = False) -> Report:
    """跑完整冒烟并返回结构化结果（测试可直接 import 调用）。"""
    report = Report()
    keywords = tuple(kw_mod.TABLE.keywords)
    report.total_keywords = len(keywords)

    if len(keywords) != kw_mod.KEYWORD_COUNT:
        report.problems.append(
            f"触发词数量不符：注册表有 {len(keywords)} 个，KEYWORD_COUNT 声明 {kw_mod.KEYWORD_COUNT}"
        )
    if kw_mod.KEYWORD_COUNT != 62:
        report.problems.append(f"KEYWORD_COUNT 应为 62，实际 {kw_mod.KEYWORD_COUNT}")
    if len(set(keywords)) != len(keywords):
        report.problems.append("触发词有重复")

    # 闲聊样本自检：不得包含任何触发词，也不得被路由成命令
    from plugins.playhub.router import route

    for sample in CHIT_CHAT:
        hit = [kw for kw in keywords if kw in sample]
        if hit:
            report.problems.append(f"闲聊样本「{sample}」含触发词 {hit}")
        if route(sample) is not None:
            report.problems.append(f"闲聊样本「{sample}」被路由成了命令")
    if len(CHIT_CHAT) != 50 or len(set(CHIT_CHAT)) != 50:
        report.problems.append(f"闲聊样本应为 50 条且不重复，实际 {len(CHIT_CHAT)} 条")

    with tempfile.TemporaryDirectory(prefix="playhub-smoke-") as tmp:
        tmp_path = Path(tmp)
        store = Store(tmp_path / "smoke.sqlite3", account="smoke")
        try:
            store.migrate()
            clock = FakeClock()
            app = App(
                store=store,
                rng=random.Random(SEED),
                now_fn=clock,
                repo_root=REPO_ROOT,
            )
            ctx = FakeCtx()

            for keyword in keywords:
                trigger = kw_mod.TABLE.trigger(keyword)
                row = Row(
                    keyword=keyword,
                    action=getattr(trigger, "action", ""),
                    replied=False,
                    expected_silent=_expects_silence(trigger),
                )
                try:
                    reply = app.handle_message(ctx, FakeMsg(keyword))
                except Exception:                        # noqa: BLE001 - 冒烟要抓全部异常
                    row.error = traceback.format_exc(limit=3).strip().splitlines()[-1]
                    report.rows.append(row)
                    continue
                if reply is not None:
                    row.replied = True
                    row.detail = _first_line(reply.text)
                    if reply.text == ERROR_TEXT:
                        row.error = f"落到异常兜底文案：{ERROR_TEXT}"
                report.rows.append(row)
                if verbose:
                    print(f"  {keyword:<4} -> {row.detail or '(静默)'}")

            # 闲聊：换全新房间，避免猜谜类残留局吃文本
            for sample in CHIT_CHAT:
                try:
                    reply = app.handle_message(
                        ctx, FakeMsg(sample, chat_id=CHAT_ROOM, sender_id="wxid_chat")
                    )
                except Exception:                        # noqa: BLE001
                    report.chit_chat_replies.append(f"{sample}（抛异常）")
                    continue
                report.chit_chat_sent += 1
                if reply is not None:
                    report.chit_chat_replies.append(f"{sample} -> {_first_line(reply.text)}")
        finally:
            store.close()

    return report


def _print_table(report: Report) -> None:
    print(f"触发词：{report.total_keywords} 个（注册表 KEYWORD_COUNT={kw_mod.KEYWORD_COUNT}）")
    print()
    header = f"{'关键词':<6}{'动作':<20}{'结果':<8}首行"
    print(header)
    print("-" * 92)
    width = max((len(row.action) for row in report.rows), default=10)
    for row in report.rows:
        if row.error:
            verdict = "异常"
        elif row.replied:
            verdict = "有回复"
        else:
            verdict = "静默"
        detail = row.error or row.detail or ("（设计如此）" if row.expected_silent else "—")
        print(f"{row.keyword:<6}{row.action:<{max(width, 12)}}{verdict:<8}{detail}")
    print("-" * 92)
    print(f"有回复：{report.replied} / {report.total_keywords}    静默：{report.silent}")
    print(f"闲聊：{report.chit_chat_sent} 条，误回复 {len(report.chit_chat_replies)} 条")
    if report.chit_chat_replies:
        for item in report.chit_chat_replies:
            print(f"  ! {item}")
    if report.failures:
        print("异常项：")
        for row in report.failures:
            reason = row.error or (
                f"应当{'静默' if row.expected_silent else '有回复'}，实际相反"
            )
            print(f"  ! {row.keyword}: {reason}")
    if report.problems:
        print("自检问题：")
        for problem in report.problems:
            print(f"  ! {problem}")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    verbose = "-v" in argv or "--verbose" in argv
    report = run_smoke(verbose=verbose)
    _print_table(report)
    print()
    if report.ok:
        print("冒烟通过：全部触发词行为符合预期，闲聊零误回复。")
        return 0
    print("冒烟失败：见上面的「!」行。")
    return 1


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(main())
