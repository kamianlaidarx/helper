"""抽签改版 + 解签（第 4 批）：同档随机、签号/版式、解签释义、跨天失效。

全部离线：临时库 + 真仓库的 `content/playhub/fortune.jsonl`（只读），可控时钟与 rng。
"""
from __future__ import annotations

import json
import random
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from plugins.playhub.app import App
from plugins.playhub.core import Store
from plugins.playhub.core.clock import CN
from plugins.playhub.engines.daily import (
    FORTUNE_EXPLAIN_MISSING,
    DailyEngine,
    FortuneEntry,
    _pick_entry,
    load_fortune_bank,
)

ROOM = "12345678@chatroom"
UID = "wxid_player_a"
NICK = "小明"
REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_CONTENT = REPO_ROOT / "content" / "playhub"


class FakeMsg:
    def __init__(self, text="", *, chat_id=ROOM, sender_id=UID, nickname=NICK, is_self=False):
        self.text = text
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.is_self = is_self
        self.data = {"sender_wechat_name": nickname}


class FakeClock:
    """按「日期」推进，取当天北京时区 12:00 的秒。"""

    def __init__(self, day="2026-11-01"):
        self.day = day

    def __call__(self) -> int:
        moment = datetime.fromisoformat(self.day).replace(
            hour=12, minute=0, second=0, tzinfo=CN
        )
        return int(moment.timestamp())

    def advance(self, days: int = 1) -> None:
        self.day = (datetime.fromisoformat(self.day) + timedelta(days=days)).date().isoformat()


class FixedRng:
    """`randrange(n)` 恒返回 0：签档与同档第一条都由测试钉死。"""

    def __init__(self) -> None:
        self.calls = 0

    def randrange(self, n: int) -> int:
        self.calls += 1
        return 0


class FakeCtx:
    class _Members:
        account = "acct"

        def resolve(self, name, room=None):
            return None

    members = _Members()


class FortuneCase(unittest.TestCase):
    with_banks = True
    day = "2026-11-01"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.store = Store(self.tmp / "playhub.sqlite3", account="acct")
        self.addCleanup(self.store.close)
        self.store.migrate()
        self.clock = FakeClock(self.day)
        self.ctx = FakeCtx()

    # -- 辅助 ---------------------------------------------------------------
    def app(self, *, rng=None, repo_root: Path | None = None) -> App:
        return App(
            store=self.store,
            rng=rng if rng is not None else FixedRng(),
            now_fn=self.clock,
            repo_root=repo_root if repo_root is not None else REPO_ROOT,
        )

    def say(self, text: str, *, app: App | None = None, uid: str = UID):
        app = app or self.app()
        return app.handle_message(self.ctx, FakeMsg(text, sender_id=uid))

    def wallet_rows(self, uid: str = UID) -> int:
        with self.store.tx(immediate=False) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM wallet WHERE room = ? AND uid = ?", (ROOM, uid)
            ).fetchone()[0]

    def ledger_count(self) -> int:
        with self.store.tx(immediate=False) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM wallet_ledger WHERE room = ?", (ROOM,)
            ).fetchone()[0]

    def counter_used(self, key: str, day: str | None = None, uid: str = UID) -> int:
        with self.store.tx(immediate=False) as conn:
            row = conn.execute(
                "SELECT used FROM daily_counter WHERE room = ? AND uid = ? AND day = ? AND key = ?",
                (ROOM, uid, day or self.clock.day, key),
            ).fetchone()
        return int(row[0]) if row is not None else 0


# ---------------------------------------------------------------------------
# 同档随机
# ---------------------------------------------------------------------------
class TierPickTest(unittest.TestCase):
    """同档多签时按 rng 随机挑；同档只有一条时不碰 rng。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def write_bank(self, rows) -> Path:
        path = self.root / "content" / "fortune.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )
        return path

    def test_same_tier_is_random_not_first(self):
        path = self.write_bank(
            [
                {"id": f"f-{i}", "no": i, "tier": "上上签", "text": f"同档第 {i} 签", "jie": "释义"}
                for i in range(1, 7)
            ]
        )
        bank = load_fortune_bank(path)
        self.assertEqual(len(bank), 6)
        picks = {
            _pick_entry(bank, "上上签", random.Random(seed)).text for seed in range(12)
        }
        self.assertGreater(len(picks), 1, "同档应当能挑出不止一条，不能固定取第一条")

    def test_same_seed_is_reproducible(self):
        path = self.write_bank(
            [
                {"id": f"f-{i}", "no": i, "tier": "上吉", "text": f"第 {i} 签"}
                for i in range(1, 6)
            ]
        )
        bank = load_fortune_bank(path)
        first = _pick_entry(bank, "上吉", random.Random(7))
        second = _pick_entry(bank, "上吉", random.Random(7))
        self.assertEqual(first, second)

    def test_single_entry_does_not_call_rng(self):
        bank = (FortuneEntry(id="f-1", no=1, tier="上上签", text="只有一条"),)
        rng = FixedRng()
        self.assertEqual(_pick_entry(bank, "上上签", rng).id, "f-1")
        self.assertEqual(rng.calls, 0)

    def test_missing_tier_returns_none(self):
        bank = (FortuneEntry(id="f-1", no=1, tier="上上签", text="只有一条"),)
        self.assertIsNone(_pick_entry(bank, "末吉", FixedRng()))


# ---------------------------------------------------------------------------
# 抽签版式
# ---------------------------------------------------------------------------
class FortuneCardTest(FortuneCase):
    def test_card_has_number_tier_and_explain_hint(self):
        reply = self.say("抽签")
        self.assertEqual(reply.action, "daily.fortune")
        self.assertIn("🙏您抽到了第3签 · 上上签！", reply.text)   # fort-0001 的签号
        self.assertIn("签诗：", reply.text)
        self.assertIn("-------------------", reply.text)
        self.assertIn("══", reply.text)
        self.assertIn("💡欲知详情可发送【解签】", reply.text)
        self.assertIn("├ 金币：+30", reply.text)
        self.assertIn("├ 钻石：+3", reply.text)
        self.assertIn("💰当前金币：30", reply.text)

    def test_draw_records_the_pick_for_today(self):
        self.say("抽签")
        with self.store.tx(immediate=False) as conn:
            row = conn.execute(
                "SELECT value FROM kv WHERE room = ? AND key = ?", (ROOM, f"fortune.pick.{UID}")
            ).fetchone()
        self.assertIsNotNone(row)
        record = json.loads(row[0])
        self.assertEqual(record["day"], self.clock.day)
        self.assertEqual(record["id"], "fort-0001")


# ---------------------------------------------------------------------------
# 解签
# ---------------------------------------------------------------------------
class ExplainTest(FortuneCase):
    def first_entry(self) -> FortuneEntry:
        rows = load_fortune_bank(REPO_CONTENT / "fortune.jsonl")
        self.assertIsNotNone(rows)
        return rows[0]         # FixedRng → 上上签的第一条

    def test_explains_todays_pick(self):
        entry = self.first_entry()
        self.say("抽签")
        reply = self.say("解签")
        self.assertEqual(reply.action, "daily.fortune_explain")
        self.assertTrue(reply.text.startswith(f"@{NICK}\n-------------------\n🔮解签："))
        self.assertIn(entry.jie, reply.text)

    def test_without_draw_prompts_first(self):
        reply = self.say("解签")
        self.assertEqual(reply.text, FORTUNE_EXPLAIN_MISSING)
        self.assertEqual(self.wallet_rows(), 0)
        self.assertEqual(self.counter_used("fortune"), 0)

    def test_yesterdays_pick_cannot_be_explained(self):
        self.say("抽签")
        self.clock.advance()
        reply = self.say("解签")
        self.assertEqual(reply.text, FORTUNE_EXPLAIN_MISSING)

    def test_explain_touches_no_wallet_and_no_counter(self):
        self.say("抽签")
        coins_before = self.ledger_count()
        used_before = self.counter_used("fortune")
        self.say("解签")
        self.say("解签")            # 不限次数，也不消耗抽签次数
        self.assertEqual(self.ledger_count(), coins_before)
        self.assertEqual(self.counter_used("fortune"), used_before)

    def test_missing_jie_falls_back(self):
        path = self.tmp / "content" / "fortune.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"id": "f-1", "no": 7, "tier": "上上签", "text": "没有释义的签"},
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        self.say("抽签")
        reply = self.say("解签")
        self.assertIn("🔮解签：这一签的释义还没写好", reply.text)

    def test_next_day_draw_gives_new_answer(self):
        self.say("抽签")
        self.clock.advance()
        self.assertIn("上上签", self.say("抽签").text)
        self.assertIn("🔮解签：", self.say("解签").text)


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
