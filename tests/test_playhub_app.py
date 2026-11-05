"""app 编排 / 金币榜 / 权限门禁单测（`docs/design/playhub.md` §2.5、§2.6.5 I1、§3.4、§7 第 1 批）。

覆盖四块：

1. `engines/ranking.py`：top10 顺序、同分按 uid 稳定排序、`period` 非 None 抛
   `ValueError`、未实现三榜抛 `NotImplementedError`、展示格式。
2. `app.py`：零回复路径（非关键词 / 未绑定动作 / `bare_behavior="silent"` 裸触发 /
   自己发的消息）与三条已实现动作的走通路径。
3. `§2.6.5 I1` 不变量：所有已实现动作的回包文本，归一化后**不等于**任何触发词。
4. `core/permissions.py` + `adapters/memberfeed.py`：只放行 owner/admin，
   `unknown` 失败关闭；适配层任何异常都折成 `unknown`。

不联网、不 sleep、不写仓库外路径：全部在 `tempfile.TemporaryDirectory` 里跑。
"""
from __future__ import annotations

import json
import re
import unittest
import tempfile
from datetime import datetime
from pathlib import Path

from plugins.playhub.adapters.memberfeed import MemberFeed
from plugins.playhub.app import ERROR_TEXT, HELP_TEXT, App, help_text
from plugins.playhub.core import Store, balance, credit, ensure_player
from plugins.playhub.core.clock import CN
from plugins.playhub.core.permissions import gate, is_allowed, normalize_role
from plugins.playhub.engines import Reply
from plugins.playhub.engines.daily import (
    FORTUNE_ALREADY,
    FORTUNE_NOT_READY,
    SIGNIN_ALREADY,
)
from plugins.playhub.engines.ranking import BOARD_EMPTY, RankRow, format_board, top
from plugins.playhub.keywords import TRIGGERS, TABLE
from plugins.playhub.textkit import normalize

ROOM = "12345678@chatroom"
UID = "wxid_player_a"
NICK = "小明"

# 固定时间：2026-11-01 12:00（Asia/Shanghai），避免依赖真实时钟。
NOW = int(datetime.fromisoformat("2026-11-01").replace(hour=12, tzinfo=CN).timestamp())


class FakeMsg:
    """最小 Message 替身：只带 app / 引擎真正读的字段。"""

    def __init__(self, text="", *, chat_id=ROOM, sender_id=UID, nickname=NICK, is_self=False):
        self.text = text
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.is_self = is_self
        self.data = {"sender_wechat_name": nickname}


class FakeClock:
    """可控时钟：取固定日期的北京时区 12:00。"""

    def __init__(self, day="2026-11-01"):
        self.day = day

    def __call__(self) -> int:
        return int(datetime.fromisoformat(self.day).replace(hour=12, tzinfo=CN).timestamp())


class FakeRng:
    """只实现 `randrange`：抽签档位由测试指定（值 = 0..11 的 roll）。"""

    def __init__(self, roll: int = 0):
        self.roll = roll

    def randrange(self, n: int) -> int:
        assert n == 12, f"抽签应当只抽 12 份，实际 {n}"
        return self.roll


class CaptureLogger:
    """接住 app 的日志，用来断言「静默路径零噪音」。"""

    def __init__(self):
        self.records: list[tuple[str, tuple]] = []

    def exception(self, msg, *args, **kwargs):        # 与 logging.Logger.exception 同形
        self.records.append((msg, args))

    def warning(self, msg, *args, **kwargs):
        self.records.append((msg, args))

    def debug(self, msg, *args, **kwargs):
        self.records.append((msg, args))

    def info(self, msg, *args, **kwargs):
        self.records.append((msg, args))


class StoreCase(unittest.TestCase):
    """一个临时目录 + 一个已迁移的 Store；子类按需叠加 App。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.clock = FakeClock()
        self.log = CaptureLogger()
        self._stores: list[Store] = []
        self.store = self._open(self.root / "playhub.db")

    # -- 夹具 ---------------------------------------------------------------
    def _open(self, path: Path) -> Store:
        store = Store(path, account="acct")
        store.migrate()
        self._stores.append(store)
        self.addCleanup(store.close)
        return store

    def new_app(self, name: str, *, store: Store | None = None, **kwargs) -> App:
        """在同一套夹具上装一个 App；`name` 只决定题库目录（`repo_root`）。

        要「首签 vs 打卡」这类隔离对照时，显式传入另一个 `Store`。
        """
        target = store if store is not None else self.store
        return App(
            store=target,
            rng=kwargs.pop("rng", FakeRng()),
            now_fn=self.clock,
            repo_root=kwargs.pop("repo_root", self.root / name),
            logger=self.log,
            **kwargs,
        )

    def write_bank(self, rows, *, base: Path | None = None) -> Path:
        """写 `<库目录>/content/fortune.jsonl`（§5.2 优先级 ①）。"""
        path = (base or (self.root / "playhub.db").parent) / "content" / "fortune.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
        return path

    # -- 断言辅助 ------------------------------------------------------------
    def coins(self, store: Store | None = None, uid: str = UID) -> int:
        with (store or self.store).tx(immediate=False) as conn:
            return balance(conn, room=ROOM, uid=uid, currency="coin")

    def diamonds(self, store: Store | None = None, uid: str = UID) -> int:
        with (store or self.store).tx(immediate=False) as conn:
            return balance(conn, room=ROOM, uid=uid, currency="diamond")

    def ledger(self, store: Store | None = None) -> list[tuple]:
        with (store or self.store).tx(immediate=False) as conn:
            return conn.execute(
                "SELECT operation_key, posting_no, currency, delta FROM wallet_ledger ORDER BY id"
            ).fetchall()

    def counters(self, store: Store | None = None) -> list[tuple]:
        with (store or self.store).tx(immediate=False) as conn:
            return conn.execute("SELECT day, key, used FROM daily_counter").fetchall()

    def seed_player(self, uid: str, nickname: str, coins: int, *, store: Store | None = None) -> None:
        target = store or self.store
        with target.tx() as conn:
            ensure_player(conn, room=ROOM, uid=uid, nickname=nickname, now=NOW)
            if coins:
                credit(
                    conn,
                    room=ROOM,
                    uid=uid,
                    currency="coin",
                    amount=coins,
                    reason="test_seed",
                    operation_key=f"seed:{uid}",
                    posting_no=0,
                    now=NOW,
                )


# ---------------------------------------------------------------------------
# 金币榜：查询层
# ---------------------------------------------------------------------------
class RankingTopTest(StoreCase):
    """§3.4 契约：top10、同分稳定、period 必须 None、未实现三榜抛异常。"""

    def seed_twelve(self) -> None:
        # 余额 100..1200：uid 序号越大余额越高，便于断言名次与顺序。
        for i in range(12):
            self.seed_player(f"wxid_p{i:02d}", f"玩家{i:02d}", 100 * (i + 1))

    def test_top10_of_12_players_in_order(self):
        self.seed_twelve()
        with self.store.tx(immediate=False) as conn:
            rows = top(conn, board="coin", room=ROOM, period=None, now=NOW)

        self.assertEqual(len(rows), 10)                       # 只出 10 行
        self.assertEqual([r.rank for r in rows], list(range(1, 11)))
        # 1200 → 200 递减（i=11 是 1200，i=2 是 200；i=0/1 被挤出前 10）
        self.assertEqual([r.score for r in rows], [1200, 1100, 1000, 900, 800, 700, 600, 500, 400, 300])
        self.assertEqual(rows[0].uid, "wxid_p11")
        self.assertEqual(rows[0].nickname, "玩家11")
        self.assertEqual(rows[-1].uid, "wxid_p02")
        self.assertNotIn("wxid_p00", [r.uid for r in rows])

    def test_tie_is_broken_by_uid_ascending(self):
        self.seed_player("wxid_b", "B", 500)
        self.seed_player("wxid_a", "A", 500)
        with self.store.tx(immediate=False) as conn:
            rows = top(conn, board="coin", room=ROOM, period=None, now=NOW)
        self.assertEqual([r.uid for r in rows], ["wxid_a", "wxid_b"])   # 同分按 uid 升序
        self.assertEqual([r.rank for r in rows], [1, 2])                # 名次连续

    def test_other_room_is_isolated(self):
        self.seed_player("wxid_a", "A", 500)
        with self.store.tx(immediate=False) as conn:
            rows = top(conn, board="coin", room="99999999@chatroom", period=None, now=NOW)
        self.assertEqual(rows, ())

    def test_coin_board_rejects_period(self):
        with self.store.tx(immediate=False) as conn:
            for period in ("day", "week", "month", "all"):
                with self.subTest(period=period):
                    with self.assertRaises(ValueError):
                        top(conn, board="coin", room=ROOM, period=period, now=NOW)

    def test_unimplemented_boards_raise(self):
        with self.store.tx(immediate=False) as conn:
            for board in ("activity", "marriage", "invite"):
                with self.subTest(board=board):
                    with self.assertRaises(NotImplementedError):
                        top(conn, board=board, room=ROOM, period="all", now=NOW)

    def test_unknown_board_raises_value_error(self):
        with self.store.tx(immediate=False) as conn:
            with self.assertRaises(ValueError):
                top(conn, board="airdrop", room=ROOM, period=None, now=NOW)

    def test_format_lines(self):
        rows = (
            RankRow(room=ROOM, uid="wxid_a", score=1200, rank=1, nickname="玩家11"),
            RankRow(room=ROOM, uid="wxid_b", score=200, rank=2, nickname=""),  # 没昵称回退 uid
        )
        self.assertEqual(
            format_board(rows),
            "=====🏆金币榜🏆=====\n🥇 1. 玩家11 1200 金币\n🥈 2. wxid_b 200 金币\n════════════════════\n⚠注：取当前余额，花掉的金币不在榜上",
        )

    def test_format_empty(self):
        self.assertEqual(format_board(()), "⚠ 提示：榜单还没有数据")


# ---------------------------------------------------------------------------
# 金币榜：入口层（动作可用性 / 静默）
# ---------------------------------------------------------------------------
class RankingCommandTest(StoreCase):
    """§7：入口先判可用性，未实现的榜「公开路径静默」。"""

    def setUp(self):
        super().setUp()
        self.app = self.new_app("app")

    def test_bare_command_shows_coin_board(self):
        self.seed_player("wxid_a", "小明", 300)
        reply = self.app.handle_message(None, FakeMsg("排行榜"))
        self.assertIsInstance(reply, Reply)
        self.assertEqual(reply.action, "ranking.top")
        self.assertIn("🥇 1. 小明 300 金币", reply.text)

    def test_alias_and_explicit_coin_arg(self):
        self.seed_player("wxid_a", "小明", 300)
        for text in ("榜单", "排行榜 金币"):
            with self.subTest(text=text):
                reply = self.app.handle_message(None, FakeMsg(text))
                self.assertIsNotNone(reply)
                self.assertIn("🥇 1. 小明 300 金币", reply.text)

    def test_empty_board(self):
        reply = self.app.handle_message(None, FakeMsg("排行榜"))
        self.assertIsNotNone(reply)
        self.assertEqual(reply.text, "⚠ 提示：榜单还没有数据")

    def test_unavailable_boards_are_silent(self):
        self.seed_player("wxid_a", "小明", 300)
        for text in ("排行榜 活跃", "排行榜 婚姻", "排行榜 邀请"):
            with self.subTest(text=text):
                self.assertIsNone(self.app.handle_message(None, FakeMsg(text)))
        self.assertEqual(self.log.records, [])            # 静默 = 零日志噪音


# ---------------------------------------------------------------------------
# app 编排：零回复路径
# ---------------------------------------------------------------------------
class AppSilentPathTest(StoreCase):
    """非关键词 / 未绑定动作 / silent 裸触发 / 自己发的消息：一律 None，且不写库。"""

    def setUp(self):
        super().setUp()
        self.app = self.new_app("app")

    def assertQuiet(self, text: str, *, is_self: bool = False) -> None:
        self.assertIsNone(self.app.handle_message(None, FakeMsg(text, is_self=is_self)), text)

    def test_non_keyword(self):
        for text in ("你好", "签到！", "今晚吃什么", "签到到", ""):
            with self.subTest(text=text):
                self.assertQuiet(text)

    def test_unbound_action(self):
        # 点歌/点唱 是 needs_arg+guide，但 music.request 不在当前批次 BINDINGS 里。
        # 第 2 批已绑 13 个 quiz 动作，`猜成语` 不再是"未绑定"的例子。
        for text in ("点歌", "点歌 晴天", "点唱", "天气 北京"):
            with self.subTest(text=text):
                self.assertQuiet(text)

    def test_silent_bare_trigger(self):
        # 礼物的裸发走触发卡（第 3 批上线后），静默只剩五个受开关管的动作
        for word in ("结婚", "宝宝", "闺蜜", "纳妾", "雇佣"):
            with self.subTest(word=word):
                trigger = TABLE.trigger(word)
                self.assertIsNotNone(trigger, f"{word} 应当是触发词")
                self.assertEqual(trigger.bare_behavior, "silent")
                self.assertQuiet(word)

    def test_self_message_is_ignored(self):
        self.assertQuiet("签到", is_self=True)
        self.assertEqual(self.ledger(), [])
        self.assertEqual(self.counters(), [])

    def test_silent_paths_write_nothing(self):
        for text in ("你好", "点歌 晴天", "礼物", "排行榜 活跃"):
            self.app.handle_message(None, FakeMsg(text))
        self.assertEqual(self.ledger(), [])
        self.assertEqual(self.counters(), [])
        self.assertEqual(self.log.records, [])

    def test_feature_switch_off_is_silent(self):
        # §2.5：kv 里 feature.<engine> 关闭时静默。
        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO kv (room, key, value, updated_at) VALUES (?, ?, ?, ?)",
                (ROOM, "feature.daily", "off", NOW),
            )
        self.assertQuiet("签到")
        self.assertEqual(self.ledger(), [])


# ---------------------------------------------------------------------------
# app 编排：已实现动作走通
# ---------------------------------------------------------------------------
class AppCommandTest(StoreCase):
    """签到 / 抽签 / 金币榜经 app 的完整链路（含异常转固定文案）。"""

    def setUp(self):
        super().setUp()
        self.app = self.new_app("app")

    def test_signin_through_app(self):
        reply = self.app.handle_message(None, FakeMsg("签到"))
        self.assertIsNotNone(reply)
        self.assertEqual(reply.action, "daily.signin")
        self.assertIn("├ 连签：1 天", reply.text)
        self.assertEqual(self.coins(), 30)
        self.assertEqual(len(self.ledger()), 1)

    def test_second_signin_same_day(self):
        self.app.handle_message(None, FakeMsg("签到"))
        reply = self.app.handle_message(None, FakeMsg("签到"))
        self.assertIsNotNone(reply)
        self.assertEqual(reply.text, SIGNIN_ALREADY)
        self.assertEqual(self.coins(), 30)                 # 不重复发币
        self.assertEqual(len(self.ledger()), 1)            # 流水只有一条

    def test_daka_equals_signin(self):
        other = self._open(self.root / "other" / "playhub.db")
        app_other = self.new_app("other", store=other)
        first = self.app.handle_message(None, FakeMsg("签到"))
        second = app_other.handle_message(None, FakeMsg("打卡"))
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(first.text, second.text)          # 文案与金额完全等价
        self.assertEqual(self.coins(store=other), 30)

    def test_fortune_without_bank_is_not_ready(self):
        # repo_root 是空临时目录，`content/playhub/fortune.jsonl` 不存在 → 不崩溃、不发币。
        reply = self.app.handle_message(None, FakeMsg("抽签"))
        self.assertIsNotNone(reply)
        self.assertEqual(reply.text, FORTUNE_NOT_READY)
        self.assertEqual(self.ledger(), [])
        with self.store.tx(immediate=False) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM player").fetchone()[0], 0)

    def test_fortune_through_app(self):
        self.write_bank([{"id": "f1", "v": 1, "tier": "中吉", "text": "中吉的签文"}])
        app = self.new_app("app2", store=self.store, rng=FakeRng(3))   # roll=3 → 中吉
        reply = app.handle_message(None, FakeMsg("抽签"))
        self.assertIsNotNone(reply)
        self.assertEqual(reply.action, "daily.fortune")
        self.assertIn("中吉", reply.text)
        self.assertIn("中吉的签文", reply.text)
        self.assertEqual(self.coins(), 12)

        again = app.handle_message(None, FakeMsg("求签"))
        self.assertEqual(again.text, FORTUNE_ALREADY)
        self.assertEqual(self.coins(), 12)

    def test_engine_exception_becomes_fixed_text(self):
        class Boom:
            name = "daily"

            def on_command(self, cmd, msg=None, ctx=None, game=None):
                raise RuntimeError("炸了")

        app = App(store=self.store, engines={"daily": Boom()}, logger=self.log)
        reply = app.handle_message(None, FakeMsg("签到"))
        self.assertIsNotNone(reply)
        self.assertEqual(reply.text, ERROR_TEXT)
        self.assertEqual(self.ledger(), [])
        self.assertEqual(len(self.log.records), 1)         # 异常要落日志（Logger.exception）


# ---------------------------------------------------------------------------
# §2.6.5 I1：回包文本不得等于任何触发词
# ---------------------------------------------------------------------------
class InvariantI1Test(StoreCase):
    """`normalize(t) ∉ KEYWORDS`：遍历关键词表，逐一断言。"""

    def assertI1(self, text: str) -> None:
        normalized = normalize(text)
        self.assertTrue(normalized)
        for trigger in TRIGGERS:                            # 60 个触发词逐个比对
            self.assertNotEqual(normalized, trigger.keyword, f"回包等于触发词：{trigger.keyword}")

    def collect(self, app: App) -> list[Reply]:
        replies = []
        for text in ("签到", "打卡", "签到", "抽签", "求签", "排行榜", "榜单", "排行榜 金币"):
            reply = app.handle_message(None, FakeMsg(text))
            if reply is not None:
                replies.append(reply)
        return replies

    def test_implemented_replies(self):
        self.write_bank([{"id": "f1", "v": 1, "tier": "上上签", "text": "上上签的签文"}])
        self.seed_player("wxid_a", "小明", 300)
        replies = self.collect(self.new_app("app"))

        actions = {reply.action for reply in replies}
        self.assertEqual(actions, {"daily.signin", "daily.fortune", "ranking.top"})
        for reply in replies:
            with self.subTest(action=reply.action, text=reply.text):
                self.assertI1(reply.text)

    def test_not_ready_and_empty_replies(self):
        # 签文库缺失 + 空榜：这两条文案也必须过 I1。
        app = self.new_app("app")
        empty = app.handle_message(None, FakeMsg("排行榜"))   # 签到会建号，所以空榜先查
        self.assertEqual(empty.text, "⚠ 提示：榜单还没有数据")
        replies = [empty, *self.collect(app)]
        self.assertTrue(any(reply.text == FORTUNE_NOT_READY for reply in replies))
        for reply in replies:
            with self.subTest(text=reply.text):
                self.assertI1(reply.text)

    def test_help_text(self):
        # 帮助文案（多行）以及每一行都不能等于触发词。
        self.assertI1(HELP_TEXT)
        for line in HELP_TEXT.splitlines():
            self.assertI1(line)


# ---------------------------------------------------------------------------
# 帮助
# ---------------------------------------------------------------------------
class HelpTextTest(unittest.TestCase):
    """§4.5 / §7：帮助是一张总览卡，列全部分组，并保留一行已知限制说明。"""

    def test_lists_current_features(self):
        text = help_text()
        self.assertEqual(text, HELP_TEXT)
        self.assertTrue(text.startswith("====="), text)     # §4.5：总览卡也要装饰头
        for word in ("签到", "抽签", "排行榜", "猜成语", "找茬", "问答",
                     "提示", "公布答案", "换一题", "结束"):
            self.assertIn(word, text)
        # 第 3 批的关系玩法必须出现在总览里，并标明默认关闭——藏起来等于让用户猜
        for word in ("结婚", "闺蜜", "结拜", "雇佣", "纳妾", "默认关闭"):
            self.assertIn(word, text)

    def test_keeps_known_limitation(self):
        # §8.6：帮助必须保留一行已知限制说明。
        self.assertIn("已知限制", help_text())


# ---------------------------------------------------------------------------
# 权限门禁（纯函数）
# ---------------------------------------------------------------------------
class PermissionTest(unittest.TestCase):
    """§7：owner/admin 放行，member/unknown/空/None 一律失败关闭。"""

    def test_allowed_roles(self):
        for role in ("owner", "admin"):
            with self.subTest(role=role):
                self.assertTrue(is_allowed(role))
                decision = gate(role)
                self.assertTrue(decision.allowed)
                self.assertEqual(decision.role, role)

    def test_denied_roles(self):
        for role in ("member", "unknown", "", "   ", None, 0, [], {}, object()):
            with self.subTest(role=role):
                self.assertFalse(is_allowed(role))
                self.assertFalse(gate(role).allowed)

    def test_unknown_role_reason(self):
        # 失败关闭的判定要能区分「未知」与「普通成员」，便于后续审计。
        self.assertEqual(gate("unknown").role, "unknown")
        self.assertFalse(gate("unknown").allowed)

    def test_misspelled_role_is_unknown(self):
        self.assertEqual(normalize_role("owner "), "owner")      # 首尾空白不敏感
        self.assertEqual(normalize_role("ADMIN"), "admin")       # 大小写不敏感
        self.assertEqual(normalize_role("administrator"), "unknown")
        self.assertEqual(normalize_role("群主"), "unknown")

    def test_gate_is_pure(self):
        # 纯函数：同样的输入永远同样的输出，不读外部状态。
        self.assertEqual(gate("member"), gate("member"))
        self.assertEqual(gate("owner"), gate("owner"))


class MemberFeedTest(unittest.TestCase):
    """适配层：只包一层 `ctx.group_member_role()`，异常/缺方法一律 `unknown`。"""

    class _Ctx:
        def __init__(self, result=None, *, raises=False):
            self.result = result
            self.raises = raises
            self.calls = []

        def group_member_role(self, room, wxid, *, expected_session=None):
            self.calls.append((room, wxid, expected_session))
            if self.raises:
                raise RuntimeError("框架炸了")
            return self.result

    def test_returns_normalized_role(self):
        for raw, expected in (("owner", "owner"), ("ADMIN", "admin"), ("member", "member"), (None, "unknown")):
            with self.subTest(raw=raw):
                feed = MemberFeed(self._Ctx(raw))
                self.assertEqual(feed.role(ROOM, UID), expected)

    def test_exception_becomes_unknown(self):
        feed = MemberFeed(self._Ctx(raises=True), logger=CaptureLogger())
        self.assertEqual(feed.role(ROOM, UID), "unknown")

    def test_missing_method_becomes_unknown(self):
        self.assertEqual(MemberFeed(object()).role(ROOM, UID), "unknown")

    def test_bad_arguments_become_unknown(self):
        ctx = self._Ctx("owner")
        feed = MemberFeed(ctx)
        self.assertEqual(feed.role("", UID), "unknown")
        self.assertEqual(feed.role(ROOM, ""), "unknown")
        self.assertEqual(feed.role(None, None), "unknown")
        self.assertEqual(ctx.calls, [])                    # 参数不全时不调框架

    def test_passes_expected_session(self):
        ctx = self._Ctx("owner")
        MemberFeed(ctx).role(ROOM, UID, expected_session="sess-1")
        self.assertEqual(ctx.calls, [(ROOM, UID, "sess-1")])


class LayeringTest(unittest.TestCase):
    """§7 分层约束的等价静态检查（本批没有 CI 脚本，先落成单测）。

    1. 引擎层不得直接写 `wallet` 表（余额只能经 `core/wallet.py`）。
    2. 抽签不得调用模块级 `random` 函数（随机源必须可注入）。
    """

    MIN = Path(__file__).resolve().parents[1] / "plugins" / "playhub"
    WALLET_WRITE = re.compile(r"[\"']\s*(?:insert\s+into|update|delete\s+from)\s+wallet\b", re.I)
    # 模块级随机函数调用（`random.Random(...)` 是实例化，不算）
    RANDOM_CALL = re.compile(
        r"\brandom\s*\.\s*(?:choice|choices|randint|randrange|shuffle|sample|uniform)\s*\(", re.I
    )
    RANDOM_SCALAR = re.compile(r"\brandom\s*\.\s*random\s*\(")   # 大小写敏感，排除 random.Random(

    def test_engines_never_write_wallet_table(self):
        for path in sorted((self.MIN / "engines").glob("*.py")):
            with self.subTest(file=path.name):
                self.assertEqual(self.WALLET_WRITE.findall(path.read_text(encoding="utf-8")), [])

    def test_fortune_has_no_module_level_random(self):
        src = (self.MIN / "engines" / "daily.py").read_text(encoding="utf-8")
        self.assertEqual(self.RANDOM_CALL.findall(src), [])
        self.assertEqual(self.RANDOM_SCALAR.findall(src), [])

    def test_default_rng_is_a_random_instance(self):
        import random

        from plugins.playhub.engines.daily import DailyEngine

        # 默认值必须是 `random.Random()` 实例（可复现、可替换），不是模块本身。
        self.assertIsInstance(DailyEngine()._rng, random.Random)


class HelpTest(StoreCase):
    """`帮助`：第 61 个触发词（第 1 批 A 案），直接回包、不经引擎、不落库。"""

    def test_help_replies_without_engine(self):
        app = self.new_app("help")
        reply = app.handle_message(None, FakeMsg("帮助"))
        self.assertIsNotNone(reply)
        self.assertEqual(reply.text, help_text())
        self.assertEqual(reply.action, "business.help")

    def test_help_writes_nothing(self):
        app = self.new_app("help")
        app.handle_message(None, FakeMsg("帮助"))
        self.assertEqual(self.ledger(), [])
        self.assertEqual(self.counters(), [])

    def test_help_is_a_normalized_keyword_of_its_own(self):
        self.assertIn("帮助", {t.keyword for t in TRIGGERS})
        self.assertEqual(TABLE.trigger("帮助").action, "business.help")

    def test_help_text_is_not_itself_a_keyword(self):
        """§2.6.5 I1 的自触发不变量对回包同样成立。"""
        self.assertNotIn(normalize(help_text()), {t.keyword for t in TRIGGERS})

    def test_help_with_argument_still_answers(self):
        app = self.new_app("help")
        reply = app.handle_message(None, FakeMsg("帮助 签到"))
        self.assertIsNotNone(reply)
        self.assertEqual(reply.text, help_text())


if __name__ == "__main__":                                  # pragma: no cover
    unittest.main()
