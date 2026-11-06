"""幂等重投：同一条消息投两次，金币 / 背包 / 分录 / 状态 / 回包只生效一次。

微信侧重投是常态（断线重连、`session` 重放、宿主重试）。设计见
`docs/design/playhub.md` §4.2.10：`App.handle_message` 在 `dispatch` 之前 claim
`idem` 表，键按以下顺序取：

1. `msg:<room>:<native_message_id>:<action>`（`native_message_id` 跨重连稳定）；
2. `<session_id>:<sequence>:<action>`（`sequence > 0` 且 `session_id` 非空）；
3. 两者都没有 → 不生成键，不做幂等（照常执行）。

重复投递**静默返回 `None`**，业务状态不发生任何变化。本文件按此契约断言：
每条用例先投一次（断言有回包、且不是 `ERROR_TEXT`），再把**同一个消息对象**投
第二次，断言 `None` 且数据库快照逐项相同。

覆盖面：
- 有状态位的命令：签到（含铜箱）、求财神、开宝箱、免费盲盒、求婚、清求婚次数；
- 每次重投都合法、只能靠消息身份拦住的命令：送礼（背包取货 / 金币代买，同秒与
  跨秒）、盲盒加购；
- 静默命令（裸「闺蜜」）两次都不冒泡，也不占 `idem` 行；
- 键的四种身份形态：原生消息号、重连（同号不同 session/sequence）、退回
  session+sequence、两者都没有时照常执行、同号跨群各自执行一次、同号不同动作。

不联网、不 sleep（时钟是假的，跨秒靠 `clock.advance`）。
"""
from __future__ import annotations

import itertools
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
SEED_DIAMOND = 20

REPO_CONTENT = pathlib.Path(__file__).resolve().parents[1] / "content" / "playhub"

#: 默认 `native_message_id` 的流水号：每构造一条新消息就换一个，保证「不同消息」
#: 不会因为共用一个号而被误判成重投；同一对象投两次仍然是同一个号。
_NATIVE_SEQ = itertools.count(1)


class FakeMsg:
    """最小 Message 替身，带上重投判定真正用到的三个身份字段。

    真实 `Message`（`wxbot_framework/sdk.py:24-37`）有 `sequence`、`session_id`，
    以及 `data["native_message_id"]`（微信服务端消息号，空串或 `"0"` 表示没有）。
    """

    def __init__(
        self,
        text: str,
        *,
        chat_id: str = ROOM,
        sender_id: str = UID_A,
        nickname: str = NAME_A,
        session_id: str = "sess-1",
        sequence: int = 7,
        native_message_id: str | None = None,
        is_self: bool = False,
    ) -> None:
        if native_message_id is None:
            native_message_id = f"{next(_NATIVE_SEQ):06d}"
        self.text = text
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.nickname = nickname
        self.session_id = session_id
        self.sequence = sequence
        self.is_self = is_self
        self.data = {
            "sender_wechat_name": nickname,
            "native_message_id": native_message_id,
        }


class FakeMember:
    def __init__(self, wxid: str) -> None:
        self.wxid = wxid


class FakeMembers:
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
    """`randrange(n)` 恒 0：开宝箱必中钻石档，结果可复现。"""

    def randrange(self, n: int) -> int:
        return 0


class RedeliveryCase(unittest.TestCase):
    day = "2026-11-01"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name)
        content = self.tmp / "content"
        content.mkdir()
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
    def msg(self, text: str, *, uid: str = UID_A, nickname: str = NAME_A, **kwargs) -> FakeMsg:
        return FakeMsg(text, sender_id=uid, nickname=nickname, **kwargs)

    def deliver(self, message, *, expect_reply: bool = True):
        """投递一次，返回回包文本（无回包返回 None）。"""
        reply = self.app.handle_message(self.ctx, message)
        if expect_reply:
            self.assertIsNotNone(reply, f"「{message.text}」这一次投递应当有回包")
            self.assertNotEqual(reply.text, ERROR_TEXT, "不该走异常兜底")
            return reply.text
        return None if reply is None else reply.text

    def say(self, text: str, **kwargs):
        return self.deliver(self.msg(text, **kwargs))

    def redeliver(self, message) -> None:
        """核心断言：同一条消息再投一次 → 静默 `None`，且数据库快照完全不变。"""
        before = self.state()
        second = self.app.handle_message(self.ctx, message)
        self.assertIsNone(second, f"「{message.text}」重复投递必须静默（不回复）")
        self.assertEqual(self.state(), before, "重复投递不该改动任何状态（钱/背包/分录/计数）")

    def deliver_twice(self, text: str, **kwargs):
        """同一个消息对象投两次，返回第一次回包（第二次已断言为 None 且无副作用）。"""
        message = self.msg(text, **kwargs)
        first = self.deliver(message)
        self.redeliver(message)
        return first

    def state(self) -> tuple:
        """可比较的数据库快照：钱包、背包、分录、计数、关系、幂等行、kv。"""
        with self.store.tx(immediate=False) as conn:
            wallets = [tuple(r) for r in conn.execute(
                "SELECT room, uid, currency, balance FROM wallet ORDER BY room, uid, currency"
            )]
            inventory = [tuple(r) for r in conn.execute(
                "SELECT room, uid, item_id, qty FROM inventory ORDER BY room, uid, item_id"
            )]
            ledger = [tuple(r) for r in conn.execute(
                "SELECT id, room, uid, currency, delta, balance_after, reason, operation_key, "
                "posting_no, at FROM wallet_ledger ORDER BY id"
            )]
            counters = [tuple(r) for r in conn.execute(
                "SELECT room, uid, day, key, used, cap FROM daily_counter ORDER BY room, uid, day, key"
            )]
            pairs = [tuple(r) for r in conn.execute(
                "SELECT id, room, kind, low_uid, high_uid, state, since, ended_at "
                "FROM pair ORDER BY id"
            )]
            idem = [tuple(r) for r in conn.execute(
                "SELECT key, scope FROM idem ORDER BY key"
            )]
            kv = [tuple(r) for r in conn.execute(
                "SELECT room, key, value FROM kv ORDER BY room, key"
            )]
        return (wallets, inventory, ledger, counters, pairs, idem, kv)

    def enable(self, name: str) -> None:
        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO kv (room, key, value, updated_at) VALUES (?, ?, '1', ?) "
                "ON CONFLICT(room, key) DO UPDATE SET value = '1'",
                (ROOM, switches.kv_key(name), self.clock()),
            )

    def give(self, uid: str, amount: int, currency: str, *, room: str = ROOM) -> None:
        with self.store.tx() as conn:
            ensure_player(conn, room=room, uid=uid, nickname="", now=self.clock())
            credit(
                conn, room=room, uid=uid, currency=currency, amount=amount,
                reason="test_seed", operation_key=f"seed:{room}:{uid}:{currency}:{amount}",
                posting_no=0, now=self.clock(),
            )

    def _balance(self, uid: str, currency: str, room: str = ROOM) -> int:
        with self.store.tx(immediate=False) as conn:
            try:
                return balance(conn, room=room, uid=uid, currency=currency)
            except WalletUninitialized:
                return 0

    def coins(self, uid: str = UID_A, *, room: str = ROOM) -> int:
        return self._balance(uid, "coin", room)

    def diamonds(self, uid: str = UID_A, *, room: str = ROOM) -> int:
        return self._balance(uid, "diamond", room)

    def bag(self, item_id: str, uid: str = UID_A) -> int:
        with self.store.tx(immediate=False) as conn:
            return items.qty(conn, ROOM, uid, item_id)

    def bag_total(self, uid: str = UID_A) -> int:
        with self.store.tx(immediate=False) as conn:
            return conn.execute(
                "SELECT COALESCE(SUM(qty), 0) FROM inventory WHERE room = ? AND uid = ?",
                (ROOM, uid),
            ).fetchone()[0]

    def put_in_bag(self, item_id: str, n: int = 1, uid: str = UID_A) -> None:
        with self.store.tx() as conn:
            ensure_player(conn, room=ROOM, uid=uid, nickname="", now=self.clock())
            items.seed(conn)
            items.add(conn, ROOM, uid, item_id, n)

    def ledger(self, reason: str | None = None) -> list:
        with self.store.tx(immediate=False) as conn:
            if reason is None:
                return list(conn.execute("SELECT * FROM wallet_ledger ORDER BY id"))
            return list(
                conn.execute(
                    "SELECT * FROM wallet_ledger WHERE reason = ? ORDER BY id", (reason,)
                )
            )

    def real_ledger(self) -> list:
        """去掉测试注入的种子流水。"""
        return [row for row in self.ledger() if row["reason"] != "test_seed"]

    def counter_used(self, key: str, uid: str = UID_A) -> int:
        with self.store.tx(immediate=False) as conn:
            row = conn.execute(
                "SELECT used FROM daily_counter WHERE room = ? AND uid = ? AND day = ? AND key = ?",
                (ROOM, uid, self.day, key),
            ).fetchone()
        return 0 if row is None else int(row[0])

    def idem_rows(self) -> list[tuple[str, str]]:
        with self.store.tx(immediate=False) as conn:
            return [
                (row[0], row[1])
                for row in conn.execute("SELECT key, scope FROM idem ORDER BY key")
            ]

    def assert_reconciled(self, room: str = ROOM) -> None:
        with self.store.tx(immediate=False) as conn:
            for row in conn.execute(
                """
                SELECT w.uid, w.currency, w.balance, COALESCE((
                    SELECT SUM(l.delta) FROM wallet_ledger l
                    WHERE l.room = w.room AND l.uid = w.uid AND l.currency = w.currency
                ), 0) AS delta_sum
                FROM wallet w WHERE w.room = ?
                """,
                (room,),
            ):
                self.assertEqual(
                    row["balance"], row["delta_sum"],
                    f"重投后账本不平：{row['uid']}/{row['currency']} "
                    f"余额 {row['balance']} ≠ 流水合计 {row['delta_sum']}",
                )


class RedeliveryTests(RedeliveryCase):
    """有天然状态位的命令：第二次投递被幂等键拦下，状态只前进一次。"""

    def test_signin_redelivery_credits_once(self) -> None:
        first = self.deliver_twice("签到")
        self.assertIn("签到成功", first)
        self.assertNotIn(daily_engine.SIGNIN_ALREADY, first)
        signin_rows = self.ledger(daily_engine.SIGNIN_REASON)
        self.assertEqual(len(signin_rows), 1, "签到金币流水只该有一条")
        self.assertEqual(signin_rows[0]["delta"], daily_engine.signin_coin(1))
        self.assertEqual(self.coins(), SEED_COIN + daily_engine.signin_coin(1))
        self.assertEqual(self.counter_used(daily_engine.SIGNIN_KEY), 1)
        self.assert_reconciled()

    def test_signin_redelivery_grants_chest_once(self) -> None:
        self.deliver_twice("签到")
        self.assertEqual(self.bag(items.CHEST_BRONZE), 1, "铜箱只该发一个")

    def test_wealth_redelivery_credits_once(self) -> None:
        first = self.deliver_twice("求财神")
        self.assertIn("财神到", first)
        self.assertEqual(len(self.ledger(daily_engine.WEALTH_REASON)), 1)
        self.assertEqual(self.coins(), SEED_COIN + daily_engine.WEALTH_COIN)
        self.assertEqual(self.counter_used(daily_engine.WEALTH_KEY), 1)
        self.assert_reconciled()

    def test_chest_redelivery_opens_once(self) -> None:
        self.say("签到")
        chest = items.chest_of(items.CHEST_BRONZE)
        before = self.coins()
        first = self.deliver_twice("开宝箱 铜")
        self.assertIn("打开铜箱", first)
        self.assertEqual(self.coins(), before + chest.coin, "箱子只该开一次")
        self.assertEqual(self.bag(items.CHEST_BRONZE), 0)
        self.assertEqual(len(self.ledger(lottery_engine.COIN_CHEST_REASON)), 1)
        self.assert_reconciled()

    def test_blindbox_redelivery_draws_once(self) -> None:
        first = self.deliver_twice("盲盒")
        self.assertIn("@", first)
        self.assertEqual(self.bag_total(), 1, "免费抽一次只该进 1 件")
        self.assertEqual(self.counter_used(lottery_engine.FREE_KEY), 1)
        self.assert_reconciled()

    def test_proposal_redelivery_creates_single_pair(self) -> None:
        first = self.deliver_twice(f"结婚 @{NAME_B}")
        self.assertIn("求婚已发出", first)
        with self.store.tx(immediate=False) as conn:
            pairs = conn.execute(
                "SELECT COUNT(*) FROM pair WHERE room = ? AND state = 'pending'", (ROOM,)
            ).fetchone()[0]
        self.assertEqual(pairs, 1, "同一条求婚消息重投只该产生一条 pending")
        self.assertEqual(self.counter_used(social_engine.PROPOSE_KEY), 1)

    def test_silent_command_redelivery_stays_silent(self) -> None:
        """裸「闺蜜」是静默命令（亲情开关默认关）：两次都不冒泡，也不写幂等行。"""
        message = self.msg("闺蜜")
        self.assertIsNone(self.app.handle_message(self.ctx, message))
        self.assertIsNone(self.app.handle_message(self.ctx, message), "重投也不该突然回一句")
        self.assertEqual(self.real_ledger(), [], "静默命令不该动钱")
        self.assertEqual(self.idem_rows(), [], "被静默的消息不该占 idem 行")


class PaidCommandRedeliveryTests(RedeliveryCase):
    """每次投递都合法的扣费命令：只能靠消息身份拦住第二次。"""

    def test_gift_same_second_redelivery_charges_once(self) -> None:
        gift = items.gift_of("rose")
        first = self.deliver_twice(f"礼物 @{NAME_B} 玫瑰")
        self.assertIn("礼物已送到", first)
        self.assertEqual(self.coins(), SEED_COIN - gift.price, "同一条消息只该扣一次钱")
        self.assertEqual(len(self.ledger(social_engine.COIN_GIFT)), 1)
        self.assert_reconciled()

    def test_gift_cross_second_redelivery_charges_once(self) -> None:
        """宿主晚一秒重投（operation_key 里的时间戳会变），仍须只扣一次。"""
        gift = items.gift_of("rose")
        message = self.msg(f"礼物 @{NAME_B} 玫瑰")
        self.deliver(message)
        self.clock.advance(1)
        self.redeliver(message)
        self.assertEqual(self.coins(), SEED_COIN - gift.price, "跨秒重投不该扣两次钱")
        self.assertEqual(len(self.ledger(social_engine.COIN_GIFT)), 1)

    def test_gift_from_inventory_is_taken_once(self) -> None:
        """背包里已有玫瑰、身上没钱：第一次取走，重投不该再往下走一步。"""
        with self.store.tx() as conn:
            ensure_player(conn, room=ROOM, uid=UID_C, nickname=NAME_C, now=self.clock())
        self.put_in_bag("rose", uid=UID_C)
        first = self.deliver_twice(f"礼物 @{NAME_B} 玫瑰", uid=UID_C, nickname=NAME_C)
        self.assertIn("背包取 1 件", first)
        self.assertEqual(self.bag("rose", UID_C), 0, "背包只该少一件")
        self.assertEqual(self.coins(UID_C), 0, "钱包没被碰过")
        self.assertEqual(self.real_ledger(), [])

    def test_gift_with_exact_funds_never_overdraws(self) -> None:
        """只够买一件礼物时，重投不会扣成负数（也不会扣两次）。"""
        with self.store.tx() as conn:
            ensure_player(conn, room=ROOM, uid=UID_C, nickname=NAME_C, now=self.clock())
        self.give(UID_C, items.gift_of("rose").price, "coin")
        self.deliver_twice(f"礼物 @{NAME_B} 玫瑰", uid=UID_C, nickname=NAME_C)
        self.assertEqual(self.coins(UID_C), 0)
        self.assertEqual(len(self.ledger(social_engine.COIN_GIFT)), 1)
        self.assert_reconciled()

    def test_blindbox_purchase_redelivery_buys_once(self) -> None:
        first = self.deliver_twice("盲盒 买次数")
        self.assertIn("@", first)
        self.assertEqual(
            self.diamonds(), SEED_DIAMOND - lottery_engine.BLIND_BOX_BUY_PRICE,
            "同一条消息只该买一次",
        )
        self.assertEqual(len(self.ledger(lottery_engine.DIAMOND_BUY_REASON)), 1)
        self.assertEqual(self.counter_used(lottery_engine.BUY_KEY), 1)
        self.assert_reconciled()

    def test_reset_proposals_redelivery_charges_once(self) -> None:
        """清求婚次数（15 钻/次）：先攒一次求婚，再清一次，重投不再扣钻。"""
        self.say(f"结婚 @{NAME_B}")
        self.assertEqual(self.counter_used(social_engine.PROPOSE_KEY), 1)
        first = self.deliver_twice("结婚 清求婚次数")
        self.assertIn("清零", first)
        reset_rows = self.ledger(social_engine.RESET_REASON)
        self.assertEqual(len(reset_rows), 1)
        self.assertEqual(reset_rows[0]["delta"], -social_engine.RESET_COST)
        self.assertEqual(self.diamonds(), SEED_DIAMOND - social_engine.RESET_COST)
        self.assertEqual(self.counter_used(social_engine.PROPOSE_KEY), 0)
        self.assert_reconciled()


class MessageIdentityTests(RedeliveryCase):
    """§4.2.10 的键选择：原生消息号 > session+sequence > 不做幂等。"""

    def test_reconnect_same_native_id_is_deduplicated(self) -> None:
        """① 同一 `native_message_id`、不同 session/sequence（重连后重投）。"""
        first = FakeMsg("签到", native_message_id="900001", session_id="sess-1", sequence=7)
        self.assertIn("签到成功", self.deliver(first))
        self.assertEqual(
            self.idem_rows(), [(f"msg:{ROOM}:900001:daily.signin", "daily.signin")],
            "键应当是 msg:<room>:<native_id>:<action>，scope 等于 action",
        )

        again = FakeMsg("签到", native_message_id="900001", session_id="sess-2", sequence=42)
        self.redeliver(again)
        self.assertEqual(self.coins(), SEED_COIN + daily_engine.signin_coin(1))
        self.assertEqual(self.counter_used(daily_engine.SIGNIN_KEY), 1)

    def test_native_id_zero_falls_back_to_session_sequence(self) -> None:
        """② `native_message_id == "0"` 视为没有，退回 session+sequence。"""
        first = FakeMsg("签到", native_message_id="0", session_id="sess-x", sequence=5)
        self.assertIn("签到成功", self.deliver(first))
        self.assertEqual(self.idem_rows(), [("sess-x:5:daily.signin", "daily.signin")])
        again = FakeMsg("签到", native_message_id="0", session_id="sess-x", sequence=5)
        self.redeliver(again)
        self.assertEqual(self.coins(), SEED_COIN + daily_engine.signin_coin(1))

        # 同 session 的新序号是**另一条**消息：放行，落到状态位「今天已经签过了」
        newer = FakeMsg("签到", native_message_id="0", session_id="sess-x", sequence=6)
        self.assertIn(daily_engine.SIGNIN_ALREADY, self.deliver(newer))

    def test_empty_native_id_falls_back_to_session_sequence(self) -> None:
        """② `native_message_id` 为空串时同样退回 session+sequence。"""
        first = FakeMsg("求财神", native_message_id="", session_id="sess-y", sequence=11)
        self.assertIn("财神到", self.deliver(first))
        self.assertEqual(self.idem_rows(), [("sess-y:11:daily.wealth", "daily.wealth")])
        again = FakeMsg("求财神", native_message_id="", session_id="sess-y", sequence=11)
        self.redeliver(again)
        self.assertEqual(self.coins(), SEED_COIN + daily_engine.WEALTH_COIN)
        self.assertEqual(len(self.ledger(daily_engine.WEALTH_REASON)), 1)

    def test_no_identity_executes_each_time(self) -> None:
        """③ 两个身份都没有 → 不做幂等，第二次照常执行（由状态位拒绝）。"""
        message = FakeMsg("签到", native_message_id="", session_id="", sequence=0)
        self.assertIn("签到成功", self.deliver(message))
        second = self.deliver(message)
        self.assertIn(daily_engine.SIGNIN_ALREADY, second, "没有身份就该照常走到状态位")
        self.assertEqual(self.idem_rows(), [], "编不出键就不该写 idem 行")
        self.assertEqual(self.coins(), SEED_COIN + daily_engine.signin_coin(1))
        self.assert_reconciled()

    def test_same_native_id_in_two_rooms_each_run_once(self) -> None:
        """④ 键含 room：同一条消息在两个群各自生效一次，群内重投仍静默。"""
        self.give(UID_A, SEED_COIN, "coin", room=ROOM_B)
        in_a = FakeMsg("签到", native_message_id="777777")
        in_b = FakeMsg("签到", native_message_id="777777", chat_id=ROOM_B)
        self.assertIn("签到成功", self.deliver(in_a))
        self.assertIn("签到成功", self.deliver(in_b))

        self.assertEqual(self.coins(room=ROOM), SEED_COIN + daily_engine.signin_coin(1))
        self.assertEqual(self.coins(room=ROOM_B), SEED_COIN + daily_engine.signin_coin(1))
        self.assertEqual(
            self.idem_rows(),
            [
                (f"msg:{ROOM}:777777:daily.signin", "daily.signin"),
                (f"msg:{ROOM_B}:777777:daily.signin", "daily.signin"),
            ],
        )
        self.redeliver(in_a)
        self.assertEqual(self.coins(room=ROOM), SEED_COIN + daily_engine.signin_coin(1))

    def test_distinct_actions_share_native_id(self) -> None:
        """键含 action：原生消息号相同的不同动作互不影响。"""
        signin = FakeMsg("签到", native_message_id="555555")
        wealth = FakeMsg("求财神", native_message_id="555555")
        self.assertIn("签到成功", self.deliver(signin))
        self.assertIn("财神到", self.deliver(wealth))
        self.assertEqual(
            [key for key, _ in self.idem_rows()],
            [f"msg:{ROOM}:555555:daily.signin", f"msg:{ROOM}:555555:daily.wealth"],
        )
        self.assertEqual(
            self.coins(), SEED_COIN + daily_engine.signin_coin(1) + daily_engine.WEALTH_COIN
        )


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
