"""验证 playhub 最小内核：迁移、档案、两币钱包、幂等与并发锁语义。

只测内核，不涉及群消息、路由或任何玩法。
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from plugins.playhub.core import (
    Currency,
    Player,
    Store,
    WalletEntry,
    balance,
    credit,
    debit,
    ensure_player,
    profile,
)
from plugins.playhub.core.clock import now_ts
from plugins.playhub.core.errors import (
    DuplicatePosting,
    Insufficient,
    InvalidAmount,
    InvalidCurrency,
    WalletUninitialized,
)

ROOM = "12345678@chatroom"
UID = "wxid_player_a"


class StoreCase(unittest.TestCase):
    """公共夹具：每个用例一个独立的临时库 + 一条已迁移的连接。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "playhub.db"
        self.store = Store(self.path, account="acct")
        self.addCleanup(self.store.close)
        self.store.migrate()

    # ---------- 小工具 ----------

    def _create(self, uid: str = UID, nickname: str | None = "小明") -> None:
        with self.store.tx() as conn:
            ensure_player(conn, room=ROOM, uid=uid, nickname=nickname, now=now_ts())

    def _balance(self, currency: Currency = "coin", uid: str = UID) -> int:
        with self.store.tx(immediate=False) as conn:
            return balance(conn, room=ROOM, uid=uid, currency=currency)

    def _ledger_rows(self) -> list[sqlite3.Row]:
        return self.store.conn.execute("SELECT * FROM wallet_ledger ORDER BY id").fetchall()


class TestMigrate(StoreCase):
    def test_migrate_twice_keeps_schema_version(self) -> None:
        # 第一次由 setUp 完成，这里是第二次。
        self.store.migrate()
        self.assertEqual(self.store.schema_version, 3)

        names = {
            row["name"]
            for row in self.store.conn.execute("SELECT name FROM sqlite_master").fetchall()
        }
        for name in (
            "meta",
            "player",
            "wallet",
            "wallet_ledger",
            "idem",
            "audit",
            "kv",
            "coin_ledger",
            "v_rank_coin",
        ):
            self.assertIn(name, names)

        # 视图只读：新代码禁止 INSERT（这里确认它确实是 view 而非同名的物理表）
        kind = self.store.conn.execute(
            "SELECT type FROM sqlite_master WHERE name = 'coin_ledger'"
        ).fetchone()["type"]
        self.assertEqual(kind, "view")


class TestProfile(StoreCase):
    def test_ensure_player_is_idempotent(self) -> None:
        self._create()
        self._create(nickname="换个昵称也不改档案")

        players = self.store.conn.execute(
            "SELECT COUNT(*) AS n FROM player WHERE room = ?", (ROOM,)
        ).fetchone()["n"]
        self.assertEqual(players, 1)

        rows = self.store.conn.execute(
            "SELECT currency, balance FROM wallet WHERE room = ? ORDER BY currency", (ROOM,)
        ).fetchall()
        # 建号必须同时初始化两个币种，且都为 0
        self.assertEqual([(r["currency"], r["balance"]) for r in rows], [("coin", 0), ("diamond", 0)])

        with self.store.tx(immediate=False) as conn:
            row = profile(conn, room=ROOM, uid=UID)
        self.assertIsInstance(row, Player)
        self.assertEqual(row.uid, UID)
        # nickname 用首次建档的值，重复 ensure 不覆盖
        self.assertEqual(row.nickname, "小明")

    def test_profile_missing_returns_none(self) -> None:
        with self.store.tx(immediate=False) as conn:
            self.assertIsNone(profile(conn, room=ROOM, uid=UID))


class TestCredit(StoreCase):
    def test_credit_moves_balance_and_returns_entry(self) -> None:
        self._create()
        with self.store.tx() as conn:
            entry = credit(
                conn,
                room=ROOM,
                uid=UID,
                currency="coin",
                amount=50,
                reason="signin_day1",
                operation_key="op-1",
                posting_no=0,
                now=now_ts(),
                ref="msg-1",
            )

        self.assertIsInstance(entry, WalletEntry)
        self.assertEqual(entry.delta, 50)
        self.assertGreater(entry.delta, 0)
        # balance 是提交后的余额，不是变动前
        self.assertEqual(entry.balance, 50)
        self.assertEqual(self._balance("coin"), 50)
        self.assertEqual(self._balance("diamond"), 0)

        rows = self._ledger_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["delta"], 50)
        self.assertEqual(rows[0]["balance_after"], 50)
        self.assertEqual(rows[0]["ref"], "msg-1")

    def test_credit_rejects_bad_amount_and_currency(self) -> None:
        self._create()
        for amount in (0, -1, 1.5, 50.0, True, "10"):
            with self.subTest(amount=amount):
                with self.assertRaises(InvalidAmount):
                    with self.store.tx() as conn:
                        credit(
                            conn,
                            room=ROOM,
                            uid=UID,
                            currency="coin",
                            amount=amount,  # type: ignore[arg-type]
                            reason="signin_day1",
                            operation_key="op-bad",
                            posting_no=0,
                            now=now_ts(),
                        )
        for currency in ("gold", "", "COIN", None):
            with self.subTest(currency=currency):
                with self.assertRaises(InvalidCurrency):
                    with self.store.tx() as conn:
                        credit(
                            conn,
                            room=ROOM,
                            uid=UID,
                            currency=currency,  # type: ignore[arg-type]
                            amount=10,
                            reason="signin_day1",
                            operation_key="op-bad",
                            posting_no=0,
                            now=now_ts(),
                        )
        # 全部被拒后余额与流水都不应变动
        self.assertEqual(self._balance("coin"), 0)
        self.assertEqual(self._ledger_rows(), [])


class TestDebit(StoreCase):
    def test_debit_success_and_negative_delta(self) -> None:
        self._create()
        with self.store.tx() as conn:
            credit(
                conn, room=ROOM, uid=UID, currency="coin", amount=100,
                reason="signin_day1", operation_key="op-1", posting_no=0, now=now_ts(),
            )
        with self.store.tx() as conn:
            entry = debit(
                conn, room=ROOM, uid=UID, currency="coin", amount=80,
                reason="blind_box", operation_key="op-2", posting_no=0, now=now_ts(),
            )
        self.assertEqual(entry.delta, -80)
        self.assertEqual(entry.balance, 20)
        self.assertEqual(self._balance("coin"), 20)

    def test_debit_insufficient_raises_and_keeps_balance(self) -> None:
        self._create()
        with self.store.tx() as conn:
            credit(
                conn, room=ROOM, uid=UID, currency="coin", amount=10,
                reason="signin_day1", operation_key="op-1", posting_no=0, now=now_ts(),
            )

        with self.assertRaises(Insufficient) as caught:
            with self.store.tx() as conn:
                debit(
                    conn, room=ROOM, uid=UID, currency="coin", amount=11,
                    reason="blind_box", operation_key="op-2", posting_no=0, now=now_ts(),
                )
        self.assertEqual(caught.exception.balance, 10)
        self.assertEqual(caught.exception.amount, 11)
        # 余额不变，且失败的分录没有落库
        self.assertEqual(self._balance("coin"), 10)
        self.assertEqual(len(self._ledger_rows()), 1)

    def test_cross_currency_cannot_cover(self) -> None:
        self._create()
        with self.store.tx() as conn:
            credit(
                conn, room=ROOM, uid=UID, currency="coin", amount=100,
                reason="signin_day1", operation_key="op-1", posting_no=0, now=now_ts(),
            )
        with self.assertRaises(Insufficient):
            with self.store.tx() as conn:
                debit(
                    conn, room=ROOM, uid=UID, currency="diamond", amount=1,
                    reason="blind_box_buy_attempts", operation_key="op-2", posting_no=0,
                    now=now_ts(),
                )
        self.assertEqual(self._balance("coin"), 100)
        self.assertEqual(self._balance("diamond"), 0)


class TestConcurrency(StoreCase):
    def test_only_one_of_two_connections_can_spend(self) -> None:
        """两条连接各扣 80：BEGIN IMMEDIATE 只让一条拿到写锁，另一条之后被判余额不足。"""
        self._create()
        with self.store.tx() as conn:
            credit(
                conn, room=ROOM, uid=UID, currency="coin", amount=100,
                reason="signin_day1", operation_key="op-seed", posting_no=0, now=now_ts(),
            )

        # 第二条连接用极短 busy_timeout，拿不到锁就立刻失败，测试不用等 3 秒。
        other = Store(self.path, account="acct", timeout=0.05)
        self.addCleanup(other.close)

        with self.store.tx() as conn:
            debit(
                conn, room=ROOM, uid=UID, currency="coin", amount=80,
                reason="blind_box", operation_key="op-a", posting_no=0, now=now_ts(),
            )
            # 第一条还没提交，第二条连写锁都拿不到
            with self.assertRaises(sqlite3.OperationalError):
                with other.tx():
                    pass

        # 第一条提交后再来：余额只剩 20，不足 80
        with self.assertRaises(Insufficient):
            with other.tx() as conn:
                debit(
                    conn, room=ROOM, uid=UID, currency="coin", amount=80,
                    reason="blind_box", operation_key="op-b", posting_no=0, now=now_ts(),
                )

        self.assertEqual(self._balance("coin"), 20)
        self.assertEqual(len(self._ledger_rows()), 2)  # 入账 + 唯一一笔扣款


class TestIdempotency(StoreCase):
    def test_duplicate_posting_is_rejected(self) -> None:
        self._create()
        with self.store.tx() as conn:
            credit(
                conn, room=ROOM, uid=UID, currency="coin", amount=30,
                reason="signin_day1", operation_key="op-dup", posting_no=0, now=now_ts(),
            )

        # 同一 operation_key + posting_no 重放：必须抛错，不能被吞成"成功"
        with self.assertRaises(DuplicatePosting):
            with self.store.tx() as conn:
                credit(
                    conn, room=ROOM, uid=UID, currency="coin", amount=30,
                    reason="signin_day1", operation_key="op-dup", posting_no=0, now=now_ts(),
                )

        self.assertEqual(self._balance("coin"), 30)
        self.assertEqual(len(self._ledger_rows()), 1)

    def test_same_operation_different_posting_no_is_allowed(self) -> None:
        """一笔业务同时奖励两种币：同一 operation_key 配不同 posting_no 是合法分录组。"""
        self._create()
        with self.store.tx() as conn:
            credit(
                conn, room=ROOM, uid=UID, currency="coin", amount=5,
                reason="signin_day1", operation_key="op-pair", posting_no=0, now=now_ts(),
            )
            credit(
                conn, room=ROOM, uid=UID, currency="diamond", amount=1,
                reason="signin_day7", operation_key="op-pair", posting_no=1, now=now_ts(),
            )
        self.assertEqual(self._balance("coin"), 5)
        self.assertEqual(self._balance("diamond"), 1)
        self.assertEqual(len(self._ledger_rows()), 2)


class TestUninitializedWallet(StoreCase):
    def test_missing_wallet_row_is_not_treated_as_zero(self) -> None:
        # 玩家没建过：钱包行缺失属于初始化异常，不能被当成余额 0
        with self.assertRaises(WalletUninitialized):
            with self.store.tx() as conn:
                debit(
                    conn, room=ROOM, uid="wxid_never_created", currency="coin", amount=1,
                    reason="blind_box", operation_key="op-x", posting_no=0, now=now_ts(),
                )
        self.assertEqual(self._ledger_rows(), [])


if __name__ == "__main__":
    unittest.main()
