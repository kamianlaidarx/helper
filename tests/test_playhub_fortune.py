"""黄历 / 星座 / 求财神三件「日更」玩法的单测（`docs/design/playhub.md` §4、§5.2）。

三者共用 `engines/daily.py`：黄历与星座是纯展示（不碰钱包），求财神是每日一次
的固定金币产出（+20，`daily_counter` 的 `wealth` 键）。

覆盖点：
- 三个词都能触发并回包，回包动作名与 `keywords.TRIGGERS` 对齐；
- 同一天结果稳定（黄历按日期哈希选行、星座按「日期 + 星座」派生）；
- 求财神核对余额变化、每日次数上限、跨天重置、`wallet_ledger` 只有一条分录；
- 题库缺失时回「未就绪」且**不**扣次数、不发币。

不联网、不 sleep、不写仓库外路径：题库按 §5.2 的「账号工作区优先」规则拷进
`tempfile.TemporaryDirectory()`，因此每个用例用的都是自己的那份文案库。
"""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta

from plugins.playhub.app import App
from plugins.playhub.core import Store, balance
from plugins.playhub.core.clock import CN
from plugins.playhub.core.errors import WalletUninitialized
from plugins.playhub.engines import daily as daily_engine

ROOM = "12345678@chatroom"
UID_A = "wxid_a"
NAME_A = "小明"
UID_B = "wxid_b"
NAME_B = "小红"

DAY = 86400

#: 仓库自带的文案库：测试只**拷贝**它，用来验证「内容确实来自题库」。
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO_CONTENT = REPO_ROOT / "content" / "playhub"
BANKS = ("almanac.jsonl", "horoscope.jsonl", "wealth.jsonl")


class FakeMsg:
    """最小 Message 替身：只带 app / 引擎真正读的字段（与既有 playhub 测试同形）。"""

    def __init__(
        self,
        text: str = "",
        *,
        chat_id: str = ROOM,
        sender_id: str = UID_A,
        nickname: str = NAME_A,
        is_self: bool = False,
    ) -> None:
        self.text = text
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.nickname = nickname
        self.is_self = is_self
        self.data = {"sender_wechat_name": nickname}


class FakeClock:
    """可控时钟：取固定日期的北京时区 12:00，`advance_days` 跨日。"""

    def __init__(self, day: str = "2026-11-01") -> None:
        self.ts = int(datetime.fromisoformat(day).replace(hour=12, tzinfo=CN).timestamp())

    def __call__(self) -> int:
        return self.ts

    def advance(self, seconds: int = 0) -> None:
        self.ts += seconds

    def advance_days(self, days: int = 1) -> None:
        self.advance(DAY * days)


class FixedRng:
    """`randrange(n)` 恒返回 0：本文件用不到随机，但 App 契约要求可注入。"""

    def randrange(self, n: int) -> int:
        return 0


class FakeCtx:
    """黄历/星座/求财神都不解析昵称，只给一个占位成员表，保持与 App 契约一致。"""

    class _Members:
        def resolve(self, name, room=None):
            raise KeyError(name)

    def __init__(self) -> None:
        self.members = self._Members()


def _copy_banks(target: pathlib.Path) -> pathlib.Path:
    """把仓库文案库拷进临时账号工作区（§5.2 的第 ① 优先级路径）。"""
    content = target / "content"
    content.mkdir(parents=True, exist_ok=True)
    for name in BANKS:
        src = REPO_CONTENT / name
        if src.is_file():
            (content / name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return content


class FortuneCase(unittest.TestCase):
    """共享夹具：临时库 + 临时题库 + 可控时钟。"""

    with_banks = True
    day = "2026-11-01"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name)
        self.content = _copy_banks(self.tmp) if self.with_banks else self.tmp / "content"
        self.store = Store(self.tmp / "playhub.sqlite3", account="acct")
        self.addCleanup(self.store.close)
        self.store.migrate()
        self.clock = FakeClock(self.day)
        self.app = App(store=self.store, rng=FixedRng(), now_fn=self.clock, repo_root=self.tmp)
        self.ctx = FakeCtx()

    # -- 辅助 ---------------------------------------------------------------
    def say(self, text: str, *, uid: str = UID_A, nickname: str = NAME_A):
        reply = self.app.handle_message(self.ctx, FakeMsg(text, sender_id=uid, nickname=nickname))
        self.assertIsNotNone(reply, f"「{text}」应当有回包")
        return reply

    def coins(self, uid: str = UID_A) -> int:
        with self.store.tx(immediate=False) as conn:
            try:
                return balance(conn, room=ROOM, uid=uid, currency="coin")
            except WalletUninitialized:
                return 0

    def ledger(self, *, reason: str | None = None) -> list:
        sql = "SELECT * FROM wallet_ledger WHERE room = ?"
        params: list = [ROOM]
        if reason is not None:
            sql += " AND reason = ?"
            params.append(reason)
        with self.store.tx(immediate=False) as conn:
            return list(conn.execute(sql + " ORDER BY id", params))

    def wallet_rows(self, uid: str = UID_A) -> int:
        with self.store.tx(immediate=False) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM wallet WHERE room = ? AND uid = ?", (ROOM, uid)
            ).fetchone()[0]

    def counter_used(self, key: str, day: str, uid: str = UID_A) -> int:
        with self.store.tx(immediate=False) as conn:
            row = conn.execute(
                "SELECT used FROM daily_counter WHERE room = ? AND uid = ? AND day = ? AND key = ?",
                (ROOM, uid, day, key),
            ).fetchone()
        return int(row[0]) if row is not None else 0


# ---------------------------------------------------------------------------
# 黄历
# ---------------------------------------------------------------------------
class AlmanacTests(FortuneCase):
    def test_bare_almanac_replies_with_today(self) -> None:
        reply = self.say("黄历")
        self.assertEqual(reply.action, "daily.almanac")
        text = reply.text
        self.assertIn("2026-11-01", text)
        self.assertIn("周日", text)
        for label in ("干支", "五行", "冲煞", "吉神", "宜", "忌"):
            self.assertIn(label, text, f"黄历卡缺「{label}」")

    def test_alias_returns_the_same_day_card(self) -> None:
        self.assertEqual(self.say("黄历").text, self.say("老黄历").text)

    def test_same_day_result_is_stable(self) -> None:
        first = self.say("黄历").text
        self.clock.advance(3600 * 5)          # 同一天晚些时候再发
        second = self.say("黄历").text
        self.assertEqual(first, second, "同一天的黄历必须是同一张（按日期轮转）")

    def test_tomorrow_and_yesterday_offsets(self) -> None:
        tomorrow = self.say("黄历 明天").text
        self.assertIn("2026-11-02", tomorrow)
        self.assertIn("周一", tomorrow)
        self.assertIn("明天", tomorrow)

        yesterday = self.say("黄历 昨天").text
        self.assertIn("2026-10-31", yesterday)
        self.assertIn("周六", yesterday)
        self.assertIn("昨天", yesterday)

    def test_content_comes_from_the_bank(self) -> None:
        """干支/冲煞必须来自 `almanac.jsonl`，不能是编的。"""
        rows = [
            json.loads(line)
            for line in (self.content / "almanac.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        text = self.say("黄历").text
        self.assertTrue(
            any(str(row.get("gz") or "") and f"{row['gz']}日" in text for row in rows),
            "回包里的干支不在题库里",
        )
        self.assertTrue(any(str(row.get("chong") or "") in text for row in rows), "冲煞不在题库里")

    def test_does_not_touch_wallet(self) -> None:
        self.say("黄历")
        self.assertEqual(self.ledger(), [], "黄历是纯展示，不写流水")
        self.assertEqual(self.wallet_rows(), 0, "黄历不该建钱包")

    def test_missing_bank_replies_with_hint(self) -> None:
        # Windows 下临时目录删除时若连接还开着会 PermissionError，所以先关库再出 with。
        with tempfile.TemporaryDirectory() as bare:
            store = Store(pathlib.Path(bare) / "playhub.sqlite3", account="acct")
            store.migrate()
            app = App(
                store=store, rng=FixedRng(), now_fn=FakeClock(self.day), repo_root=pathlib.Path(bare)
            )
            try:
                reply = app.handle_message(self.ctx, FakeMsg("黄历"))
            finally:
                store.close()
        self.assertIsNotNone(reply)
        self.assertIn("黄历还没准备好", reply.text)
        self.assertIn("almanac.jsonl", reply.text)


# ---------------------------------------------------------------------------
# 星座
# ---------------------------------------------------------------------------
class HoroscopeTests(FortuneCase):
    def test_explicit_sign_replies(self) -> None:
        reply = self.say("星座 狮子")
        self.assertEqual(reply.action, "daily.horoscope")
        text = reply.text
        self.assertIn("狮子座", text)
        for label in ("爱情", "工作", "财运", "健康", "幸运数字", "幸运颜色"):
            self.assertIn(label, text, f"星座卡缺「{label}」")

    def test_sign_name_without_suffix_is_accepted(self) -> None:
        self.assertIn("狮子座", self.say("星座 我是狮子").text)

    def test_explicit_sign_is_stable_within_a_day(self) -> None:
        first = self.say("星座 狮子").text
        self.clock.advance(3600 * 3)
        second = self.say("星座 狮子").text
        self.assertEqual(first, second, "同一星座同一天结果不变")

    def test_same_sign_differs_between_people_is_not_required(self) -> None:
        """设计只说「同星座同日相同」，两人看同一星座本就该一样。"""
        mine = self.say("星座 狮子", uid=UID_A, nickname=NAME_A).text
        other = self.say("星座 狮子", uid=UID_B, nickname=NAME_B).text
        self.assertEqual(mine, other)

    def test_derived_sign_is_stable_per_user(self) -> None:
        first = self.say("运势 随便").text     # 参数里没星座名 → 按日期+群+人派生
        self.clock.advance(60)
        second = self.say("运势 随便").text
        self.assertEqual(first, second, "没指定星座时同一天同一人必须稳定")
        self.assertIn("没给星座", first, "派生星座要在卡面上说明")

    def test_bare_horoscope_returns_guide_card(self) -> None:
        """`星座` 是 guide：裸发只回说明书，不消耗也不发币（§2.6.6）。"""
        reply = self.say("星座")
        self.assertIn("【星座 狮子】", reply.text)
        self.assertNotIn("今日运势 · ", reply.text, "说明书卡不是结果卡")

    def test_does_not_touch_wallet(self) -> None:
        self.say("星座 狮子")
        self.assertEqual(self.ledger(), [])
        self.assertEqual(self.wallet_rows(), 0)


# ---------------------------------------------------------------------------
# 求财神
# ---------------------------------------------------------------------------
class WealthTests(FortuneCase):
    def test_grants_fixed_coins(self) -> None:
        reply = self.say("求财神")
        self.assertEqual(reply.action, "daily.wealth")
        self.assertEqual(self.coins(), daily_engine.WEALTH_COIN)
        self.assertIn("财神到", reply.text)
        self.assertIn(f"+{daily_engine.WEALTH_COIN}", reply.text)
        self.assertIn(f"当前金币：{daily_engine.WEALTH_COIN}", reply.text)

    def test_single_ledger_entry_with_expected_key(self) -> None:
        self.say("求财神")
        rows = self.ledger(reason=daily_engine.WEALTH_REASON)
        self.assertEqual(len(rows), 1, "一次求财神只写一条分录")
        row = rows[0]
        self.assertEqual(row["currency"], "coin")
        self.assertEqual(row["delta"], daily_engine.WEALTH_COIN)
        self.assertEqual(row["balance_after"], daily_engine.WEALTH_COIN)
        self.assertEqual(row["operation_key"], f"wealth:2026-11-01:{UID_A}")

    def test_second_call_same_day_is_refused(self) -> None:
        self.say("求财神")
        reply = self.say("拜财神")              # 别名走同一个动作
        self.assertIn(daily_engine.WEALTH_ALREADY, reply.text)
        self.assertEqual(self.coins(), daily_engine.WEALTH_COIN, "同一天只能发一次")
        self.assertEqual(len(self.ledger(reason=daily_engine.WEALTH_REASON)), 1)
        self.assertEqual(self.counter_used(daily_engine.WEALTH_KEY, "2026-11-01"), 1)

    def test_next_day_resets(self) -> None:
        self.say("求财神")
        self.clock.advance_days(1)
        reply = self.say("求财神")
        self.assertIn("财神到", reply.text)
        self.assertEqual(self.coins(), daily_engine.WEALTH_COIN * 2)
        self.assertEqual(self.counter_used(daily_engine.WEALTH_KEY, "2026-11-02"), 1)

    def test_per_user_limit(self) -> None:
        self.say("求财神", uid=UID_A, nickname=NAME_A)
        self.say("求财神", uid=UID_B, nickname=NAME_B)      # 另一个人不受影响
        self.assertEqual(self.coins(UID_A), daily_engine.WEALTH_COIN)
        self.assertEqual(self.coins(UID_B), daily_engine.WEALTH_COIN)

    def test_missing_bank_does_not_charge_or_consume_counter(self) -> None:
        # Windows 下临时目录删除时若连接还开着会 PermissionError，所以先关库再出 with。
        with tempfile.TemporaryDirectory() as bare:
            store = Store(pathlib.Path(bare) / "playhub.sqlite3", account="acct")
            store.migrate()
            root = pathlib.Path(bare)
            clock = FakeClock(self.day)
            app = App(store=store, rng=FixedRng(), now_fn=clock, repo_root=root)
            try:
                reply = app.handle_message(self.ctx, FakeMsg("求财神"))
                self.assertIsNotNone(reply)
                self.assertIn(daily_engine.WEALTH_NOT_READY, reply.text)
                with store.tx(immediate=False) as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM wallet_ledger").fetchone()[0], 0)
                    used = conn.execute(
                        "SELECT COUNT(*) FROM daily_counter WHERE key = ?", (daily_engine.WEALTH_KEY,)
                    ).fetchone()[0]
                self.assertEqual(used, 0, "题库缺失不该吃掉当天次数")

                # 补上题库后同一天还能正常求到（证明上一次没消耗次数）
                content = root / "content"
                content.mkdir(parents=True, exist_ok=True)
                (content / "wealth.jsonl").write_text(
                    (REPO_CONTENT / "wealth.jsonl").read_text(encoding="utf-8"), encoding="utf-8"
                )
                second = app.handle_message(self.ctx, FakeMsg("求财神"))
                self.assertIn("财神到", second.text)
            finally:
                store.close()

    def test_quote_comes_from_bank(self) -> None:
        texts = [
            json.loads(line).get("text", "")
            for line in (self.content / "wealth.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        reply = self.say("求财神").text
        self.assertTrue(
            any(t and t in reply for t in texts),
            "签文必须来自 wealth.jsonl（不自己编）",
        )


class FortuneBankTests(FortuneCase):
    """`抽签/求签` 的随包题库（`content/playhub/fortune.jsonl`）：存在且能被读出来。"""

    with_banks = False

    def test_fortune_draw_is_usable_with_shipped_content(self) -> None:
        app = App(store=self.store, rng=FixedRng(), now_fn=self.clock, repo_root=REPO_ROOT)
        reply = app.handle_message(self.ctx, FakeMsg("抽签"))
        self.assertIsNotNone(reply)
        self.assertEqual(reply.action, "daily.fortune")
        self.assertNotIn("未就绪", reply.text, "随包内容里应当有 fortune.jsonl")

        texts = [
            json.loads(line).get("text", "")
            for line in (REPO_CONTENT / "fortune.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertTrue(texts, "fortune.jsonl 不该是空的")
        self.assertTrue(
            any(text and text in reply.text for text in texts),
            "签文必须来自 fortune.jsonl（不自己编）",
        )


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
