"""`lottery` 引擎单测：盲盒（免费 1 / 加购 5 钻 / 日限 2 / 掉落进背包 / 台账）与
开宝箱（三型金币、钻石命中、背包扣减、没有箱子时的获取提示），以及签到发箱
（§4.4「宝箱三型」取得方式：铜箱每日 1、金箱连签第 7 天 1）。

不联网、不 sleep、不写仓库外路径；rng 用固定桩，概率分支可测。
"""
from __future__ import annotations

import pathlib
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta

from plugins.playhub.app import App
from plugins.playhub.core import Store, balance, credit, items, switches
from plugins.playhub.core.clock import CN
from plugins.playhub.core.errors import WalletUninitialized

ROOM = "12345678@chatroom"
UID_A = "wxid_a"
NAME_A = "小明"

DAY = 86400


class FixedRng:
    """`randrange(n)` 恒等返回 0（命中第一个分支/最小概率），便于断言。"""

    def __init__(self, value: int = 0) -> None:
        self.value = value

    def randrange(self, n: int) -> int:
        return self.value % n if n else 0


class MissRng(FixedRng):
    """永远返回 `n-1`：盲盒抽到最末档（戒指）、宝箱钻石判定必不命中。"""

    def randrange(self, n: int) -> int:
        return max(0, n - 1)


class FakeMsg:
    def __init__(self, text: str = "", *, chat_id: str = ROOM, sender_id: str = UID_A, nickname: str = NAME_A):
        self.text = text
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.is_self = False
        self.data = {"sender_wechat_name": nickname}


class FakeCtx:
    """lottery 不解析昵称，只给一个占位成员表，保持与 App 契约一致。"""

    class _Members:
        def resolve(self, name, room=None):
            raise KeyError(name)

    def __init__(self) -> None:
        self.members = self._Members()


class FakeClock:
    def __init__(self) -> None:
        self.ts = int(datetime.fromisoformat("2026-11-01T12:00:00").replace(tzinfo=CN).timestamp())

    def __call__(self) -> int:
        return self.ts

    def advance(self, seconds: int = 1) -> None:
        self.ts += seconds

    def next_day(self) -> None:
        self.advance(DAY)


class LotteryTestBase(unittest.TestCase):
    rng_factory = FixedRng

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self.store = Store(self.tmp / "playhub.sqlite3", account="acct")
        self.store.migrate()
        self.clock = FakeClock()
        self.app = App(store=self.store, rng=self.rng_factory(), now_fn=self.clock, repo_root=self.tmp)
        self.ctx = FakeCtx()

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    # -- 辅助 ---------------------------------------------------------------
    def say(self, text: str) -> str:
        reply = self.app.handle_message(self.ctx, FakeMsg(text))
        self.assertIsNotNone(reply, f"「{text}」应当有回包")
        return reply.text

    def quiet(self, text: str) -> None:
        self.assertIsNone(self.app.handle_message(self.ctx, FakeMsg(text)), f"「{text}」应当静默")

    def give(self, amount: int, *, currency: str = "coin", uid: str = UID_A) -> None:
        with self.store.tx() as conn:
            from plugins.playhub.core.wallet import ensure_player

            ensure_player(conn, room=ROOM, uid=uid, nickname="", now=self.clock())
            credit(
                conn, room=ROOM, uid=uid, currency=currency, amount=amount,
                reason="test_seed", operation_key=f"seed:{uid}:{currency}:{amount}",
                posting_no=0, now=self.clock(),
            )

    def coins(self, uid: str = UID_A) -> int:
        with self.store.tx(immediate=False) as conn:
            try:
                return balance(conn, room=ROOM, uid=uid, currency="coin")
            except WalletUninitialized:
                return 0

    def diamonds(self, uid: str = UID_A) -> int:
        with self.store.tx(immediate=False) as conn:
            try:
                return balance(conn, room=ROOM, uid=uid, currency="diamond")
            except WalletUninitialized:
                return 0

    def bag(self, item_id: str, uid: str = UID_A) -> int:
        with self.store.tx(immediate=False) as conn:
            return items.qty(conn, ROOM, uid, item_id)

    def draws(self, source: str) -> list[sqlite3.Row]:
        with self.store.tx(immediate=False) as conn:
            return list(
                conn.execute(
                    "SELECT * FROM lottery_draw WHERE room = ? AND source = ? ORDER BY id",
                    (ROOM, source),
                )
            )


class BlindBoxTests(LotteryTestBase):
    def test_bare_draw_is_free_once(self) -> None:
        before = self.coins()
        text = self.say("盲盒")
        self.assertIn("🎰 盲盒开出", text)
        self.assertEqual(self.coins(), before, "盲盒不消耗金币")
        self.assertEqual(sum(self.bag(g.item_id) for g in items.GIFTS), 1, "只抽到 1 件")
        self.assertEqual(len(self.draws("blind_box")), 1)

    def test_second_draw_needs_purchase(self) -> None:
        self.say("盲盒")
        self.give(5, currency="diamond")
        text = self.say("盲盒 买次数")
        self.assertIn("🧾 加购成功", text)
        self.assertEqual(self.diamonds(), 0, "加购扣 5 钻")
        text = self.say("盲盒")
        self.assertIn("🎰 盲盒开出", text)
        self.assertEqual(len(self.draws("blind_box")), 2)

    def test_used_up_tells_how_to_buy(self) -> None:
        self.say("盲盒")
        text = self.say("盲盒")
        self.assertIn("今天的盲盒次数用完了", text)
        self.assertIn("盲盒 买次数", text)

    def test_buy_cap_two_per_day(self) -> None:
        self.give(100, currency="diamond")
        self.assertIn("加购成功", self.say("盲盒 买次数"))
        self.assertIn("加购成功", self.say("盲盒 买次数"))
        text = self.say("盲盒 买次数")
        self.assertIn("今天的加购次数用完了", text)
        self.assertEqual(self.diamonds(), 90, "只买两次，共花 10 钻")

    def test_buy_without_diamond(self) -> None:
        text = self.say("盲盒 买次数")
        self.assertIn("❌ 钻石不够", text)

    def test_batch_eight_limited_by_quota(self) -> None:
        text = self.say("盲盒 8")
        self.assertIn("少抽了 7 次", text)
        self.assertEqual(len(self.draws("blind_box")), 1, "免费只有 1 次")

    def test_loot_lands_in_bag_and_is_recorded(self) -> None:
        self.say("盲盒")
        row = self.draws("blind_box")[0]
        self.assertEqual(row["cost"], 0)
        self.assertIsNone(row["cost_currency"])
        self.assertEqual(self.bag(row["item_id"]), 1)
        self.assertIsNotNone(items.gift_of(row["item_id"]))

    def test_next_day_resets_quota(self) -> None:
        self.say("盲盒")
        self.clock.next_day()
        text = self.say("盲盒")
        self.assertIn("🎰 盲盒开出", text)

    def test_bad_count_is_usage(self) -> None:
        self.assertIn("用法：盲盒", self.say("盲盒 三个"))


class BlindBoxMissTests(LotteryTestBase):
    rng_factory = MissRng

    def test_worst_drop_is_last_loot(self) -> None:
        text = self.say("盲盒")
        self.assertIn("💍 戒指", text)
        self.assertIn("礼物进了背包", text)


class ChestTests(LotteryTestBase):
    def _add_chest(self, item_id: str, n: int = 1) -> None:
        with self.store.tx() as conn:
            items.add(conn, ROOM, UID_A, item_id, n)

    def test_without_chest_explains_source(self) -> None:
        text = self.say("开宝箱 铜")
        self.assertIn("📦 没有铜箱", text)
        self.assertIn("每天签到送 1 个", text)

    def test_silver_source_is_batch_four(self) -> None:
        text = self.say("开宝箱 银")
        self.assertIn("活跃榜周榜前 10", text)

    def test_open_bronze_gives_coins(self) -> None:
        self._add_chest(items.CHEST_BRONZE)
        text = self.say("开宝箱 铜")
        self.assertIn("🥉 打开铜箱", text)
        self.assertIn("金币：+50", text)
        self.assertEqual(self.coins(), 50)
        self.assertEqual(self.bag(items.CHEST_BRONZE), 0, "箱子被消耗")
        self.assertEqual(len(self.draws("chest")), 1)

    def test_open_gold_hits_diamond_with_fixed_rng(self) -> None:
        self._add_chest(items.CHEST_GOLD)
        text = self.say("开宝箱 金")
        self.assertIn("金币：+400", text)
        self.assertIn("钻石：+2", text)
        self.assertEqual(self.diamonds(), 2)

    def test_default_box_is_bronze(self) -> None:
        self._add_chest(items.CHEST_BRONZE)
        text = self.say("开宝箱")
        self.assertIn("打开铜箱", text)

    def test_unknown_box(self) -> None:
        self.assertIn("❓ 认不出这个箱子", self.say("开宝箱 铁"))

    def test_unbound_trigger_stays_silent(self) -> None:
        self.quiet("宝箱箱箱")


class ChestMissTests(LotteryTestBase):
    rng_factory = MissRng

    def test_no_diamond_when_miss(self) -> None:
        with self.store.tx() as conn:
            items.add(conn, ROOM, UID_A, items.CHEST_SILVER)
        text = self.say("开宝箱 银")
        self.assertIn("金币：+150", text)
        self.assertIn("这次没开出钻石", text)
        self.assertEqual(self.diamonds(), 0)


class SigninChestTests(LotteryTestBase):
    def test_first_signin_grants_bronze(self) -> None:
        text = self.say("签到")
        self.assertIn("铜箱：+1", text)
        self.assertEqual(self.bag(items.CHEST_BRONZE), 1)
        self.assertEqual(self.bag(items.CHEST_GOLD), 0)

    def test_seventh_streak_grants_gold(self) -> None:
        for i in range(7):
            if i:
                self.clock.next_day()
            self.say("签到")
        self.assertEqual(self.bag(items.CHEST_BRONZE), 7)
        self.assertEqual(self.bag(items.CHEST_GOLD), 1, "连签第 7 天发金箱")
        self.assertGreaterEqual(self.diamonds(), 3)

    def test_break_streak_no_gold(self) -> None:
        self.say("签到")
        self.clock.advance(3 * DAY)      # 断签
        self.say("签到")
        self.assertEqual(self.bag(items.CHEST_GOLD), 0)


class SwitchIndependenceTests(LotteryTestBase):
    def test_blind_box_has_no_switch(self) -> None:
        """盲盒/开宝箱不在 5 个开关里（§4.3.1），关掉社交也不该影响它。"""
        with self.store.tx() as conn:
            for name in switches.KEYS:
                conn.execute(
                    "INSERT INTO kv (room, key, value, updated_at) VALUES (?, ?, '0', ?) "
                    "ON CONFLICT(room, key) DO UPDATE SET value = '0'",
                    (ROOM, switches.kv_key(name), self.clock()),
                )
        self.assertIn("🎰 盲盒开出", self.say("盲盒"))


if __name__ == "__main__":
    unittest.main()
