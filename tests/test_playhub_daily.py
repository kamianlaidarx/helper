"""签到 / 抽签引擎单测：数值、连签、断签、幂等、签文库缺失。
（`docs/design/playhub.md` §4.4 数值表、§7 第 1 批验收标准 1）

不联网、不 sleep、不写仓库外路径：全部在 `tempfile.TemporaryDirectory` 里跑。
"""
from __future__ import annotations

import json
import random
import tempfile
import re
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from plugins.playhub.core import Store, balance, ensure_player
from plugins.playhub.core.clock import CN
from plugins.playhub.engines import GameRuntime
from plugins.playhub.engines.daily import (
    FORTUNE_ALREADY,
    FORTUNE_NOT_READY,
    FORTUNE_TIERS,
    FORTUNE_TOTAL_WEIGHT,
    SIGNIN_ALREADY,
    SIGNIN_COIN_MAX,
    SIGNIN_DIAMOND_REASON,
    DailyEngine,
    load_fortune_bank,
    next_streak,
    signin_coin,
    signin_diamond,
)
from plugins.playhub.router import route

ROOM = "12345678@chatroom"
UID = "wxid_player_a"
NICK = "小明"


class FakeMsg:
    """最小 Message 替身：只带引擎真正读的字段。"""

    def __init__(self, text="", *, chat_id=ROOM, sender_id=UID, nickname=NICK, is_self=False):
        self.text = text
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.is_self = is_self
        self.data = {"sender_wechat_name": nickname}


class FakeClock:
    """可控时钟：按「日期」推进，取当天北京时区 12:00 的秒。"""

    def __init__(self, day="2026-11-01"):
        self.day = day

    def __call__(self) -> int:
        d = datetime.fromisoformat(self.day).replace(hour=12, minute=0, second=0, tzinfo=CN)
        return int(d.timestamp())

    def add(self, days: int = 1) -> None:
        self.day = (date.fromisoformat(self.day) + timedelta(days=days)).isoformat()


class FakeRng:
    """只实现 `randrange`：签档完全由测试指定（值 = 0..11 的 roll）。"""

    def __init__(self, roll: int):
        self.roll = roll
        self.calls = 0

    def randrange(self, n: int) -> int:
        assert n == FORTUNE_TOTAL_WEIGHT, f"抽签应当只抽 12 份，实际 {n}"
        self.calls += 1
        return self.roll


BANK_ROWS = [
    {"id": "fortune-0001", "v": 1, "tier": "上上签", "text": "上上签的签文"},
    {"id": "fortune-0002", "v": 1, "tier": "上吉", "text": "上吉的签文"},
    {"id": "fortune-0003", "v": 1, "tier": "中吉", "text": "中吉的签文"},
    {"id": "fortune-0004", "v": 1, "tier": "小吉", "text": "小吉的签文"},
    {"id": "fortune-0005", "v": 1, "tier": "末吉", "text": "末吉的签文"},
]


class DailyCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.clock = FakeClock()
        self.store = self._open(self.root / "playhub.db")
        self.addCleanup(self.store.close)

    # -- 夹具 ---------------------------------------------------------------
    def _open(self, path: Path) -> Store:
        store = Store(path, account="acct")
        store.migrate()
        return store

    def engine(self, rng=None, repo_root=None) -> DailyEngine:
        # repo_root 指到临时目录：保证 ② 仓库默认题库在本用例里必然不存在（不读真仓库）
        return DailyEngine(rng=rng, now_fn=self.clock, repo_root=repo_root or self.root)

    def game(self, store=None) -> GameRuntime:
        return GameRuntime(store=self.store if store is None else store)

    def signin(self, text="签到", *, msg=None, rng=None, store=None):
        cmd = route(text)
        self.assertIsNotNone(cmd)
        return self.engine(rng=rng).on_command(
            cmd, msg or FakeMsg(), None, self.game(store)
        )

    def fortune(self, text="抽签", *, rng, msg=None, store=None):
        cmd = route(text)
        self.assertIsNotNone(cmd)
        return self.engine(rng=rng).on_command(
            cmd, msg or FakeMsg(), None, self.game(store)
        )

    def _balance(self, currency: str, uid: str = UID, store=None) -> int:
        with (self.store if store is None else store).tx(immediate=False) as conn:
            return balance(conn, room=ROOM, uid=uid, currency=currency)

    def coins(self, uid: str = UID) -> int:
        return self._balance("coin", uid)

    def diamonds(self, uid: str = UID) -> int:
        return self._balance("diamond", uid)

    def ledger(self, where: str = "1=1") -> list:
        with self.store.tx(immediate=False) as conn:
            return conn.execute(
                "SELECT uid, currency, delta, reason, operation_key, posting_no "
                f"FROM wallet_ledger WHERE room = ? AND {where} ORDER BY id",
                (ROOM,),
            ).fetchall()

    def counter_rows(self) -> int:
        with self.store.tx(immediate=False) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM daily_counter WHERE room = ?", (ROOM,)
            ).fetchone()[0]

    def write_bank(self, rows=BANK_ROWS, *, path: Path | None = None) -> Path:
        target = path or (self.root / "content" / "fortune.jsonl")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
            encoding="utf-8",
        )
        return target


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------
class SigninFormulaTest(unittest.TestCase):
    def test_coin_curve_matches_4_4(self):
        expected = {1: 30, 2: 35, 3: 40, 6: 55, 10: 75, 11: 80, 12: 80, 30: 80}
        for streak, coin in expected.items():
            with self.subTest(streak=streak):
                self.assertEqual(signin_coin(streak), coin)

    def test_cap_is_80(self):
        self.assertEqual(signin_coin(999), 80)
        self.assertEqual(SIGNIN_COIN_MAX, 80)

    def test_diamond_only_on_full_weeks(self):
        for streak in range(1, 22):
            with self.subTest(streak=streak):
                self.assertEqual(signin_diamond(streak), 3 if streak % 7 == 0 else 0)

    def test_next_streak(self):
        self.assertEqual(next_streak("2026-10-31", 5, "2026-11-01"), 6)
        self.assertEqual(next_streak("2026-10-30", 5, "2026-11-01"), 1)   # 断签
        self.assertEqual(next_streak("", 0, "2026-11-01"), 1)             # 首签
        self.assertEqual(next_streak("2026-10-31", 0, "2026-11-01"), 1)   # 坏数据兜底


# ---------------------------------------------------------------------------
# 签到
# ---------------------------------------------------------------------------
class SigninTest(DailyCase):
    def test_first_signin_gives_30(self):
        reply = self.signin()
        self.assertTrue(reply.text.startswith("@小明\n🎁 签到成功"), reply.text)
        self.assertIn("├ 连签：1 天", reply.text)
        self.assertIn("├ 金币：+30", reply.text)
        self.assertIn("💰当前金币：30", reply.text)
        self.assertEqual(self.coins(), 30)
        self.assertEqual(self.diamonds(), 0)
        rows = self.ledger()
        self.assertEqual(len(rows), 1)
        self.assertEqual(tuple(rows[0])[1:4], ("coin", 30, "signin"))

    def test_next_day_gives_35(self):
        self.signin()
        self.clock.add()
        reply = self.signin()
        self.assertIn("├ 连签：2 天", reply.text)
        self.assertIn("├ 金币：+35", reply.text)
        self.assertIn("💰当前金币：65", reply.text)
        self.assertEqual(self.coins(), 65)

    def test_seventh_day_adds_3_diamonds(self):
        for day in range(1, 8):
            reply = self.signin()
            if day < 7:
                self.assertEqual(self.diamonds(), 0)
                self.assertNotIn("钻石", reply.text)
            self.clock.add()
        # 第 7 天（循环最后一天签完后再加了一天，这里回读第 7 天那次的结果）
        self.assertEqual(self.diamonds(), 3)
        self.assertEqual(self.coins(), sum(signin_coin(d) for d in range(1, 8)))
        diamond_rows = self.ledger("currency = 'diamond'")
        self.assertEqual(len(diamond_rows), 1)
        self.assertEqual(diamond_rows[0][2], 3)
        self.assertEqual(diamond_rows[0][3], SIGNIN_DIAMOND_REASON)

    def test_seventh_day_reply_mentions_diamond(self):
        for _ in range(6):
            self.signin()
            self.clock.add()
        reply = self.signin()
        self.assertIn("├ 钻石：+3", reply.text)
        self.assertIn("├ 金币：+60", reply.text)    # 30 + 5×min(7-1,10) = 60

    def test_fourteenth_day_gives_another_3(self):
        for _ in range(13):
            self.signin()
            self.clock.add()
        self.signin()
        self.assertEqual(self.diamonds(), 6)
        self.assertEqual(len(self.ledger("currency = 'diamond'")), 2)

    def test_coin_caps_at_80(self):
        deltas = []
        for _ in range(16):
            reply = self.signin()
            matched = re.search(r"├ 金币：\+(\d+)", reply.text)
            self.assertIsNotNone(matched, reply.text)
            deltas.append(int(matched.group(1)))
            self.clock.add()
        self.assertEqual(deltas[:3], [30, 35, 40])
        self.assertEqual(deltas[9], 75)            # 第 10 天
        self.assertEqual(deltas[10:], [80] * 6)    # 第 11 天起封顶 80

    def test_break_streak_resets_to_1(self):
        self.signin()
        self.clock.add(2)                          # 隔一天没签
        reply = self.signin()
        self.assertIn("├ 连签：1 天", reply.text)
        self.assertIn("💰当前金币：60", reply.text)

    def test_same_day_twice_no_double_credit(self):
        self.signin()
        reply = self.signin()
        self.assertEqual(reply.text, SIGNIN_ALREADY)
        self.assertEqual(self.coins(), 30)
        self.assertEqual(len(self.ledger("reason = 'signin'")), 1)
        with self.store.tx(immediate=False) as conn:
            used = conn.execute(
                "SELECT used FROM daily_counter WHERE room = ? AND uid = ? AND key = 'signin'",
                (ROOM, UID),
            ).fetchone()[0]
        self.assertEqual(used, 1)

    def test_daka_alias_equals_signin(self):
        other_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(other_tmp.cleanup)
        other = self._open(Path(other_tmp.name) / "playhub.db")
        self.addCleanup(other.close)

        signin_reply = self.signin("签到")
        daka_reply = self.signin("打卡", msg=FakeMsg("打卡"), store=other)
        self.assertEqual(signin_reply.text, daka_reply.text)

        with other.tx(immediate=False) as conn:
            self.assertEqual(balance(conn, room=ROOM, uid=UID, currency="coin"), 30)
        self.assertEqual(self.coins(), 30)

    def test_missing_identity_is_silent(self):
        self.assertIsNone(self.signin(msg=FakeMsg(sender_id="")))
        self.assertIsNone(self.signin(msg=FakeMsg(chat_id="")))
        self.assertEqual(self.ledger(), [])

    def test_operation_key_is_per_day(self):
        self.signin()
        keys = {row[4] for row in self.ledger()}
        self.assertEqual(keys, {"signin:2026-11-01:" + UID})


# ---------------------------------------------------------------------------
# 抽签
# ---------------------------------------------------------------------------
class FortuneTest(DailyCase):
    def setUp(self):
        super().setUp()
        # 抽签用例要读「余额不变」，而 wallet.balance() 对未建号抛 WalletUninitialized
        # （§3.2.1：钱包行缺失按初始化异常处理）。这里先建号但不发币，余额仍是 0。
        with self.store.tx() as conn:
            ensure_player(conn, room=ROOM, uid=UID, nickname=NICK, now=0)

    def test_total_weight_is_12(self):
        self.assertEqual(FORTUNE_TOTAL_WEIGHT, 12)
        self.assertEqual([t.weight for t in FORTUNE_TIERS], [1, 2, 4, 3, 2])
        self.assertEqual([t.coin for t in FORTUNE_TIERS], [30, 20, 12, 8, 5])
        self.assertEqual([t.diamond for t in FORTUNE_TIERS], [3, 0, 0, 0, 0])

    def test_five_tiers_by_roll(self):
        self.write_bank()
        cases = [
            (0, "上上签", 30, 3),
            (1, "上吉", 20, 0),
            (2, "上吉", 20, 0),
            (3, "中吉", 12, 0),
            (6, "中吉", 12, 0),
            (7, "小吉", 8, 0),
            (9, "小吉", 8, 0),
            (10, "末吉", 5, 0),
            (11, "末吉", 5, 0),
        ]
        for roll, name, coin, diamond in cases:
            with self.subTest(roll=roll):
                before = self.coins()
                self.clock.add()
                reply = self.fortune(rng=FakeRng(roll))
                self.assertTrue(reply.text.startswith(f"@小明\n🎋 抽到「{name}」"), reply.text)
                self.assertIn(f"├ 金币：+{coin}", reply.text)
                self.assertEqual(self.coins(), before + coin)
                if diamond:
                    self.assertIn(f"├ 钻石：+{diamond}", reply.text)

    def test_weight_ratio_over_1200_draws(self):
        rng = random.Random(2026)
        counts = {t.name: 0 for t in FORTUNE_TIERS}
        engine = self.engine(rng=rng)
        for _ in range(1200):
            counts[engine.draw_tier().name] += 1
        for tier in FORTUNE_TIERS:
            expected = 1200 * tier.weight / FORTUNE_TOTAL_WEIGHT
            with self.subTest(tier=tier.name):
                self.assertLessEqual(abs(counts[tier.name] - expected), 60, counts)
        self.assertEqual(sum(counts.values()), 1200)

    def test_once_per_day(self):
        self.write_bank()
        first = self.fortune(rng=FakeRng(0))
        self.assertIn("上上签", first.text)
        second = self.fortune(rng=FakeRng(0))
        self.assertEqual(second.text, FORTUNE_ALREADY)
        self.assertEqual(self.coins(), 30)
        self.assertEqual(self.diamonds(), 3)
        self.assertEqual(len(self.ledger("reason = 'fortune'")), 1)
        self.assertEqual(len(self.ledger("reason = 'fortune_best'")), 1)

    def test_bank_missing_is_not_ready_and_charges_nothing(self):
        self.assertFalse((self.root / "content" / "fortune.jsonl").exists())
        reply = self.fortune(rng=FakeRng(0))
        self.assertEqual(reply.text, FORTUNE_NOT_READY)
        self.assertEqual(self.coins(), 0)
        self.assertEqual(self.ledger(), [])
        self.assertEqual(self.counter_rows(), 0)   # 次数没被消耗

    def test_tier_missing_in_bank_is_not_ready(self):
        self.write_bank([{"id": "fortune-0009", "v": 1, "tier": "中吉", "text": "只有中吉"}])
        reply = self.fortune(rng=FakeRng(0))       # 上上签那一档为空
        self.assertEqual(reply.text, FORTUNE_NOT_READY)
        self.assertEqual(self.coins(), 0)
        self.assertEqual(self.counter_rows(), 0)

    def test_text_comes_from_bank(self):
        self.write_bank()
        reply = self.fortune(rng=FakeRng(10))
        self.assertIn("末吉的签文", reply.text)

    def test_bank_override_wins_over_repo_default(self):
        # ① 账号工作区覆盖存在时完全覆盖 ②（§5.2）
        repo_root = self.root / "repo"
        self.write_bank(
            [{"id": "f-1", "v": 1, "tier": "上上签", "text": "仓库默认签文"}],
            path=repo_root / "content" / "playhub" / "fortune.jsonl",
        )
        self.write_bank([{"id": "f-2", "v": 1, "tier": "上上签", "text": "账号覆盖签文"}])
        reply = self.fortune(rng=FakeRng(0))
        self.assertIn("账号覆盖签文", reply.text)
        self.assertNotIn("仓库默认签文", reply.text)

    def test_repo_default_used_when_no_override(self):
        repo_root = self.root / "repo"
        self.write_bank(
            [{"id": "f-1", "v": 1, "tier": "上上签", "text": "仓库默认签文"}],
            path=repo_root / "content" / "playhub" / "fortune.jsonl",
        )
        cmd = route("抽签")
        reply = self.engine(rng=FakeRng(0), repo_root=repo_root).on_command(
            cmd, FakeMsg(), None, self.game()
        )
        self.assertIn("仓库默认签文", reply.text)

    def test_bad_lines_are_skipped(self):
        path = self.root / "content" / "fortune.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            'not json\n'
            '{"id": "f-1", "v": 1, "text": "缺签等"}\n'
            '{"id": "f-2", "v": 1, "tier": "上上签"}\n'
            '{"id": "f-3", "v": 1, "tier": "上上签", "text": "唯一可用签文"}\n',
            encoding="utf-8",
        )
        rows = load_fortune_bank(path)
        self.assertEqual(rows, (("上上签", "唯一可用签文"),))
        reply = self.fortune(rng=FakeRng(0))
        self.assertIn("唯一可用签文", reply.text)

    def test_q_and_tag_fallback(self):
        self.write_bank([{"id": "f-1", "v": 1, "tag": ["上上签"], "q": "用 q 字段的签文"}])
        reply = self.fortune(rng=FakeRng(0))
        self.assertIn("用 q 字段的签文", reply.text)

    def test_qiusign_alias(self):
        self.write_bank()
        reply = self.fortune("求签", rng=FakeRng(0))
        self.assertIn("上上签", reply.text)


if __name__ == "__main__":
    unittest.main()
