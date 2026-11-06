"""全局账本对账：`wallet.balance` 必须等于该 `(room, uid, currency)` 的流水之和。

`docs/design/playhub.md` §1121「决定一：独立 wallet 表」与 §6.2「事务纪律」把
`core/wallet.py` 定成唯一的金融写路径，`wallet_ledger` 是它唯一的账本。只要有一条
分录漏写、写重、或余额被绕过钱包直接改，本文件的对账断言就会炸。

做法：先把会动钱包的命令按真实顺序跑一遍（签到 / 求财神 / 开宝箱 / 送礼 /
结婚 / 度蜜月 / 申请离婚 / 拒绝离婚退款 / 盲盒加购），再对全库做四项断言：

1. 每个 `(room, uid, currency)` 的 `wallet.balance == SUM(wallet_ledger.delta)`；
2. 每条分录的 `balance_after` == 该组按 id 累加的 running sum（分录顺序自洽）；
3. 没有任何负余额（`wallet.balance` 与 `balance_after` 都 >= 0，schema 的 CHECK 之外再兜一层）；
4. 余额不足的失败命令不留半分钱痕迹（不发币、不写流水、不出现负数）。

不联网、不 sleep、不写仓库外路径。
"""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from datetime import datetime

from plugins.playhub.app import ERROR_TEXT, App
from plugins.playhub.core import Store, balance, credit, ensure_player, items, switches
from plugins.playhub.core.clock import CN
from plugins.playhub.core.errors import WalletUninitialized
from plugins.playhub.engines import daily as daily_engine
from plugins.playhub.engines import lottery as lottery_engine
from plugins.playhub.engines import social as social_engine

ROOM = "12345678@chatroom"
ROOM_B = "87654321@chatroom"

UID_A = "wxid_a"
NAME_A = "小明"
UID_B = "wxid_b"
NAME_B = "小红"
UID_C = "wxid_c"
NAME_C = "小刚"

SEED_COIN = 1000
SEED_DIAMOND = 60

REPO_CONTENT = pathlib.Path(__file__).resolve().parents[1] / "content" / "playhub"

#: 关键报表：每个钱包行 vs 它自己的流水合计。
RECONCILE_SQL = """
SELECT w.room, w.uid, w.currency, w.balance,
       COALESCE((
           SELECT SUM(l.delta) FROM wallet_ledger l
           WHERE l.room = w.room AND l.uid = w.uid AND l.currency = w.currency
       ), 0) AS delta_sum
FROM wallet w
ORDER BY w.room, w.uid, w.currency
"""


class FakeMsg:
    """最小 Message 替身（与既有 playhub 测试同形）。"""

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


class FakeMember:
    def __init__(self, wxid: str) -> None:
        self.wxid = wxid


class FakeMembers:
    """成员表反查：只认登记过的昵称，未登记抛 KeyError（与社交测试一致）。"""

    def __init__(self, names: dict[str, str]) -> None:
        self._names = names

    def resolve(self, name, room=None):
        key = (name or "").lstrip("@＠")
        if key not in self._names:
            raise KeyError(key)
        return FakeMember(self._names[key])


class FakeCtx:
    def __init__(self) -> None:
        self.members = FakeMembers({NAME_A: UID_A, NAME_B: UID_B, NAME_C: UID_C})


class FakeClock:
    def __init__(self, day: str = "2026-11-01") -> None:
        self.ts = int(datetime.fromisoformat(day).replace(hour=12, tzinfo=CN).timestamp())

    def __call__(self) -> int:
        return self.ts

    def advance(self, seconds: int = 1) -> None:
        self.ts += seconds


class FixedRng:
    """`randrange(n)` 恒返回 0（宝箱必中钻石），保证流程可复现。"""

    def randrange(self, n: int) -> int:
        return 0


class LedgerCase(unittest.TestCase):
    day = "2026-11-01"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name)
        content = self.tmp / "content"
        content.mkdir()
        # §5.2 的 ① 账号工作区题库：求财神要它才发币
        (content / "wealth.jsonl").write_text(
            (REPO_CONTENT / "wealth.jsonl").read_text(encoding="utf-8"), encoding="utf-8"
        )
        self.store = Store(self.tmp / "playhub.sqlite3", account="acct")
        self.addCleanup(self.store.close)
        self.store.migrate()
        self.clock = FakeClock(self.day)
        self.app = App(store=self.store, rng=FixedRng(), now_fn=self.clock, repo_root=self.tmp)
        self.ctx = FakeCtx()
        self.enable(switches.MARRIAGE)
        self.give(UID_A, SEED_COIN, "coin")
        self.give(UID_A, SEED_DIAMOND, "diamond")

    # -- 辅助 ---------------------------------------------------------------
    def say(self, text: str, *, uid: str = UID_A, nickname: str = NAME_A, room: str = ROOM) -> str:
        reply = self.app.handle_message(
            self.ctx, FakeMsg(text, chat_id=room, sender_id=uid, nickname=nickname)
        )
        self.assertIsNotNone(reply, f"「{text}」应当有回包")
        self.assertNotEqual(reply.text, ERROR_TEXT, f"「{text}」不该走异常兜底")
        return reply.text

    def enable(self, name: str) -> None:
        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO kv (room, key, value, updated_at) VALUES (?, ?, '1', ?) "
                "ON CONFLICT(room, key) DO UPDATE SET value = '1'",
                (ROOM, switches.kv_key(name), self.clock()),
            )

    def give(self, uid: str, amount: int, currency: str) -> None:
        with self.store.tx() as conn:
            ensure_player(conn, room=ROOM, uid=uid, nickname="", now=self.clock())
            credit(
                conn,
                room=ROOM,
                uid=uid,
                currency=currency,
                amount=amount,
                reason="test_seed",
                operation_key=f"seed:{uid}:{currency}:{amount}",
                posting_no=0,
                now=self.clock(),
            )

    def coins(self, uid: str = UID_A, room: str = ROOM) -> int:
        with self.store.tx(immediate=False) as conn:
            try:
                return balance(conn, room=room, uid=uid, currency="coin")
            except WalletUninitialized:
                return 0

    def diamonds(self, uid: str = UID_A, room: str = ROOM) -> int:
        with self.store.tx(immediate=False) as conn:
            try:
                return balance(conn, room=room, uid=uid, currency="diamond")
            except WalletUninitialized:
                return 0

    def bag(self, item_id: str, uid: str = UID_A) -> int:
        with self.store.tx(immediate=False) as conn:
            return items.qty(conn, ROOM, uid, item_id)

    def ledger_rows(self) -> list:
        with self.store.tx(immediate=False) as conn:
            return list(conn.execute("SELECT * FROM wallet_ledger ORDER BY id"))

    def reasons(self) -> set[str]:
        with self.store.tx(immediate=False) as conn:
            return {row[0] for row in conn.execute("SELECT DISTINCT reason FROM wallet_ledger")}

    # -- 断言 ---------------------------------------------------------------
    def assert_reconciled(self) -> None:
        """① 钱包余额 == 该组流水之和。"""
        with self.store.tx(immediate=False) as conn:
            rows = list(conn.execute(RECONCILE_SQL))
        self.assertTrue(rows, "至少得有一个钱包行")
        for row in rows:
            self.assertEqual(
                row["balance"],
                row["delta_sum"],
                f"对账不平：room={row['room']} uid={row['uid']} currency={row['currency']} "
                f"余额 {row['balance']} ≠ 流水合计 {row['delta_sum']}",
            )

    def assert_balance_after_is_running_sum(self) -> None:
        """② 每条分录的 `balance_after` 等于该组按 id 累加的结果。"""
        running: dict[tuple[str, str, str], int] = {}
        for row in self.ledger_rows():
            key = (row["room"], row["uid"], row["currency"])
            running[key] = running.get(key, 0) + int(row["delta"])
            self.assertEqual(
                row["balance_after"], running[key],
                f"分录 #{row['id']} 的 balance_after 与累加值不符（{key}）",
            )

    def assert_no_negative_balance(self) -> None:
        """③ 任何地方都不许出现负余额。"""
        with self.store.tx(immediate=False) as conn:
            worst = conn.execute("SELECT MIN(balance) FROM wallet").fetchone()[0]
            worst_after = conn.execute("SELECT MIN(balance_after) FROM wallet_ledger").fetchone()[0]
        self.assertGreaterEqual(worst if worst is not None else 0, 0, "出现负余额")
        self.assertGreaterEqual(worst_after if worst_after is not None else 0, 0, "分录里出现负余额")

    def assert_ledger_is_consistent(self) -> None:
        self.assert_reconciled()
        self.assert_balance_after_is_running_sum()
        self.assert_no_negative_balance()


class MixedOperationTests(LedgerCase):
    """跑一遍混合操作，再整体对账。"""

    def run_mixed_operations(self, *, room: str = ROOM) -> None:
        # 1. 签到：+30 金币（连签第 1 天）+ 1 个铜箱
        self.say("签到", room=room)
        # 2. 求财神：固定 +20 金币
        self.say("求财神", room=room)
        # 3. 开宝箱：铜箱 +50 金币（FixedRng 命中钻石档）
        self.say("开宝箱 铜", room=room)
        # 4. 送礼：背包没有玫瑰 → 代买 -20 金币
        self.say(f"礼物 @{NAME_B} 玫瑰", room=room)
        # 5. 结婚 → 6. 对方同意（关系状态，不动钱）
        self.say(f"结婚 @{NAME_B}", room=room)
        self.say("结婚 同意", uid=UID_B, nickname=NAME_B, room=room)
        # 7. 度蜜月：-520 金币
        self.say("结婚 度蜜月", room=room)
        # 8. 申请离婚：-200 金币
        self.say("结婚 申请离婚", room=room)
        # 9. 对方拒绝离婚：+200 金币退款
        self.say("结婚 拒绝离婚", uid=UID_B, nickname=NAME_B, room=room)
        # 10. 盲盒加购：-5 钻石（钻石 sink）
        self.say("盲盒 买次数", room=room)
        # 11. 用掉次数
        self.say("盲盒", room=room)

    def test_mixed_operations_reconcile(self) -> None:
        self.run_mixed_operations()
        self.assert_ledger_is_consistent()

    def test_mixed_operations_cover_more_than_five_kinds(self) -> None:
        self.run_mixed_operations()
        reasons = self.reasons()
        for expected in (
            "signin",
            daily_engine.WEALTH_REASON,
            lottery_engine.COIN_CHEST_REASON,
            social_engine.COIN_GIFT,
            social_engine.COIN_HONEYMOON,
            social_engine.COIN_DIVORCE,
            social_engine.COIN_DIVORCE_REFUND,
            lottery_engine.DIAMOND_BUY_REASON,
        ):
            self.assertIn(expected, reasons, f"混合操作应当产生「{expected}」流水")
        self.assertGreaterEqual(len(reasons - {"test_seed"}), 5, "至少 5 种命令动过钱")

    def test_final_balances_match_the_command_arithmetic(self) -> None:
        """最终余额必须等于按各命令单价算出来的值（独立复算，不信对账本身）。"""
        self.run_mixed_operations()
        gift = items.gift_of("rose")
        chest = items.chest_of(items.CHEST_BRONZE)
        self.assertIsNotNone(gift)
        self.assertIsNotNone(chest)
        expected_coin = (
            SEED_COIN
            + daily_engine.signin_coin(1)
            + daily_engine.WEALTH_COIN
            + chest.coin
            - gift.price
            - social_engine.HONEYMOON_COST
            - social_engine.DIVORCE_COST
            + social_engine.DIVORCE_COST
        )
        self.assertEqual(self.coins(UID_A), expected_coin)
        self.assertEqual(
            self.diamonds(UID_A),
            SEED_DIAMOND + chest.diamond - lottery_engine.BLIND_BOX_BUY_PRICE,
        )

    def test_second_user_wallets_reconcile_too(self) -> None:
        self.run_mixed_operations()
        # 第二个人只签到 + 求财神，账本同样要平
        self.say("签到", uid=UID_B, nickname=NAME_B)
        self.say("求财神", uid=UID_B, nickname=NAME_B)
        self.assert_ledger_is_consistent()
        self.assertEqual(
            self.coins(UID_B), daily_engine.signin_coin(1) + daily_engine.WEALTH_COIN
        )


class RoomIsolationTests(LedgerCase):
    """对账按 `(room, uid, currency)` 分组：另一个群的操作不能串到本群。"""

    def test_two_rooms_reconcile_independently(self) -> None:
        MixedOperationTests.run_mixed_operations(self)
        before = self.coins(UID_A)

        with self.store.tx() as conn:
            ensure_player(conn, room=ROOM_B, uid=UID_A, nickname="", now=self.clock())
            conn.execute(
                "INSERT INTO kv (room, key, value, updated_at) VALUES (?, ?, '1', ?) "
                "ON CONFLICT(room, key) DO UPDATE SET value = '1'",
                (ROOM_B, switches.kv_key(switches.MARRIAGE), self.clock()),
            )
        self.say("签到", room=ROOM_B)
        self.say("求财神", room=ROOM_B)

        self.assert_ledger_is_consistent()
        self.assertEqual(self.coins(UID_A), before, "别群的签到不该动本群余额")
        self.assertEqual(
            self.coins(UID_A, room=ROOM_B),
            daily_engine.signin_coin(1) + daily_engine.WEALTH_COIN,
        )


class FailedOperationTests(LedgerCase):
    """余额不足的失败路径：不发币、不写流水、不留负数。"""

    def test_insufficient_gift_leaves_no_trace(self) -> None:
        with self.store.tx() as conn:
            ensure_player(conn, room=ROOM, uid=UID_C, nickname=NAME_C, now=self.clock())
            credit(
                conn, room=ROOM, uid=UID_C, currency="coin", amount=10,
                reason="test_seed", operation_key="seed:c:coin:10", posting_no=0, now=self.clock(),
            )
        before_rows = len(self.ledger_rows())
        text = self.say(f"礼物 @{NAME_B} 戒指", uid=UID_C, nickname=NAME_C)
        self.assertIn("金币不够", text)
        self.assertEqual(self.coins(UID_C), 10, "失败不该扣钱")
        self.assertEqual(len(self.ledger_rows()), before_rows, "失败不该写流水")
        self.assert_ledger_is_consistent()

    def test_insufficient_blindbox_purchase_leaves_no_trace(self) -> None:
        with self.store.tx() as conn:
            ensure_player(conn, room=ROOM, uid=UID_C, nickname=NAME_C, now=self.clock())
        before_rows = len(self.ledger_rows())
        text = self.say("盲盒 买次数", uid=UID_C, nickname=NAME_C)
        self.assertIn("钻石不够", text)
        self.assertEqual(self.diamonds(UID_C), 0)
        self.assertEqual(len(self.ledger_rows()), before_rows)
        self.assert_ledger_is_consistent()


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
