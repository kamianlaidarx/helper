"""钱包写序（`docs/design/playhub.md` §4.2.9）：重复分录在动账之前就被挡下。

为什么值得单独测：原来的实现是「先 UPDATE，再写分录」，UNIQUE 冲突发生在
余额已经改完之后——外层事务虽然会回滚，但只要调用方把两步拆进不同事务
（后台工资、离婚退款正是这么写的），钱就动了两次。现在的顺序是：

1. 先按 `(room, operation_key, posting_no)` 查分录；命中直接 `DuplicatePosting`，
   余额一分不动；
2. 否则在 SAVEPOINT 里 UPDATE + 写分录，任何异常 `ROLLBACK TO` 再上抛。

全部离线：临时目录 + 一个已迁移的 `Store`。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from plugins.playhub.core import Store, balance, credit, debit, ensure_player
from plugins.playhub.core import wallet
from plugins.playhub.core.errors import DuplicatePosting, Insufficient

ROOM = "12345678@chatroom"
UID = "wxid_player_a"
NOW = 1_760_000_000


class WalletOrderTest(unittest.TestCase):
    """重复分录、失败分录、SAVEPOINT 回滚三种情形。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "playhub.db"
        self.store = Store(self.path, account="acct")
        self.store.migrate()
        self.addCleanup(self.store.close)
        with self.store.tx() as conn:
            ensure_player(conn, room=ROOM, uid=UID, nickname="小明", now=NOW)

    # -- 夹具 ---------------------------------------------------------------
    def coins(self) -> int:
        with self.store.tx(immediate=False) as conn:
            return balance(conn, room=ROOM, uid=UID, currency="coin")

    def ledger(self) -> list[tuple[str, int, int]]:
        with self.store.tx(immediate=False) as conn:
            return [
                (str(key), int(no), int(delta))
                for key, no, delta in conn.execute(
                    "SELECT operation_key, posting_no, delta FROM wallet_ledger ORDER BY id"
                ).fetchall()
            ]

    def credit(self, amount: int, *, key: str = "op", posting_no: int = 0) -> None:
        with self.store.tx() as conn:
            credit(
                conn,
                room=ROOM,
                uid=UID,
                currency="coin",
                amount=amount,
                reason="test",
                operation_key=key,
                posting_no=posting_no,
                now=NOW,
            )

    def debit(self, amount: int, *, key: str = "op", posting_no: int = 0) -> None:
        with self.store.tx() as conn:
            debit(
                conn,
                room=ROOM,
                uid=UID,
                currency="coin",
                amount=amount,
                reason="test",
                operation_key=key,
                posting_no=posting_no,
                now=NOW,
            )

    # -- 重复分录：先挡后动 ------------------------------------------------
    def test_duplicate_credit_leaves_balance_untouched(self):
        self.credit(100)
        with self.assertRaises(DuplicatePosting):
            self.credit(100)
        self.assertEqual(self.coins(), 100)                  # 只加了一次
        self.assertEqual(len(self.ledger()), 1)              # 也只有一条流水

    def test_duplicate_debit_leaves_balance_untouched(self):
        self.credit(100, key="seed")
        self.debit(40)
        with self.assertRaises(DuplicatePosting):
            self.debit(40)
        self.assertEqual(self.coins(), 60)                   # 没扣第二次
        self.assertEqual(self.ledger()[-1], ("op", 0, -40))

    def test_duplicate_is_checked_before_sufficiency(self):
        # 这条最像真实事故：重复的工资单在余额不足时也必须报「重复」而不是
        # 「余额不足」——否则后台会把一张已付的工资当成没付，反复重试。
        self.credit(100, key="seed")
        self.debit(100)
        with self.assertRaises(DuplicatePosting):
            self.debit(500)                                  # 余额 0，但先撞重复
        self.assertEqual(self.coins(), 0)

    def test_same_key_other_posting_no_is_a_new_posting(self):
        # 工资的两笔（扣雇主 posting 0 / 加员工 posting 1）靠 posting_no 区分。
        self.credit(50, key="sched:salary:1:2026-11-03", posting_no=0)
        self.credit(50, key="sched:salary:1:2026-11-03", posting_no=1)
        self.assertEqual(self.coins(), 100)
        self.assertEqual(len(self.ledger()), 2)

    def test_duplicate_inside_outer_transaction_only_rolls_back_to_savepoint(self):
        # 调用方自己开事务时，重复分录不能把外层事务一起作废（后台一个事务里
        # 可能还要写审计/kv）。
        with self.store.tx() as conn:
            credit(
                conn, room=ROOM, uid=UID, currency="coin", amount=10,
                reason="test", operation_key="outer", posting_no=0, now=NOW,
            )
            with self.assertRaises(DuplicatePosting):
                credit(
                    conn, room=ROOM, uid=UID, currency="coin", amount=10,
                    reason="test", operation_key="outer", posting_no=0, now=NOW,
                )
            self.assertEqual(balance(conn, room=ROOM, uid=UID, currency="coin"), 10)
        self.assertEqual(self.coins(), 10)

    # -- 失败分录：SAVEPOINT 回滚 ------------------------------------------
    def test_failed_ledger_write_rolls_back_balance(self):
        def boom(*args, **kwargs):
            raise RuntimeError("写分录炸了")

        original = wallet._write_ledger
        wallet._write_ledger = boom
        self.addCleanup(setattr, wallet, "_write_ledger", original)
        with self.assertRaises(RuntimeError):
            self.credit(100)
        self.assertEqual(self.coins(), 0)                    # 余额回滚
        self.assertEqual(self.ledger(), [])

        wallet._write_ledger = original
        self.credit(100)                                     # 回滚干净，连接还能用
        self.assertEqual(self.coins(), 100)

    def test_insufficient_rolls_back_and_leaves_connection_usable(self):
        with self.assertRaises(Insufficient):
            self.debit(10)
        self.credit(10)                                      # SAVEPOINT 没漏在外面
        self.assertEqual(self.coins(), 10)


if __name__ == "__main__":
    unittest.main()
