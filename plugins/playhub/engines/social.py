"""社交引擎（第 3 批）：婚姻段 + 闺蜜 / 结拜 / 雇佣 / 纳妾（同一套 `pair` 状态机）。

数值一字不改地照 `docs/design/playhub.md:1359-1362` 与 `§4.4`：

- 求婚：每日 3 次（`daily_counter` 的 `propose_marry`，日界 Asia/Shanghai）。
- 度蜜月：520 金币/次，同对夫妻冷却 24h，`honeymoon_count + 1`，**纯展示无收益**。
- 申请离婚：发起方付 200 金币，对方 24h 内 `结婚 同意离婚` / `结婚 拒绝离婚`；
  超时失效并**全额退费**；不提供单方强离。
- 清求婚次数：15 钻石（`marriage_reset_proposals`），当日计数为 0 时拒绝支付。
- 一人一婚：`pair` 的两个部分唯一索引兜「低位/高位」各一侧，跨侧重复靠事务内 SELECT。

两条落地口径（文档没写，这里定下来并同步进设计文档）：

1. **求婚 24h 自动失效**：pending 的 `pair` 行超过 24h 视为放弃，状态置 `ended`。
   没有它，`pair_active_pair` 会把两个人永久锁在「有一条待确认请求」上。
2. **超时退款是惰性的**：本批没有定时器，`_expire()` 在任何婚姻子命令开头执行，
   把该群过期的求婚与离婚申请一起收掉。批量定时动作属第 4 批。

写路径：余额只经 `core/wallet.py` 的 `credit()` / `debit()`，本文件不出现
`INSERT INTO wallet` / `UPDATE wallet`；所有关系状态变更写一行 `audit`。

闺蜜 / 结拜 / 雇佣 / 纳妾共用一套「发起 → 对方同意 → active → 解除」流程（`RELATIONS` 表）：

- 四者都**允许一个人同时有多条**（多个闺蜜、多个结拜兄弟、多份雇佣），只有 `marriage`
  受「一人一婚」约束（`pair` 的两个部分唯一索引就是为婚姻建的）。
- 纳妾多两条门槛：发起人**必须已有婚姻**；钻石余额 **≥ 30**（准入门槛，**不扣款**，
  v1 没有任何钻石扣款入口，见 §4.4）。
- `雇佣 工资` 只**展示**约定的日工资（默认 20 金币/日，存在 `pair.meta.wage`），
  真正的发放是第 4 批的 `scheduled_pay_salary`，本文件不发钱。
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass

from ..core import balance, clock, credit, debit, ensure_player, items, render
from ..core.errors import DuplicatePosting, Insufficient
from ..textkit import normalize
from . import Reply, msg_nickname, msg_room, msg_uid, store_of

# ---------------------------------------------------------------------------
# 数值与键（§4.4）
# ---------------------------------------------------------------------------
KIND_MARRIAGE = "marriage"

PROPOSE_CAP = 3                    # 求婚每日 3 次
PROPOSE_KEY = "propose_marry"      # daily_counter.key
PROPOSE_TTL = 24 * 3600            # 未确认的求婚 24h 失效（文档未写，见模块 docstring）

HONEYMOON_COST = 520               # 金币/次
HONEYMOON_COOLDOWN = 24 * 3600     # 同对夫妻冷却
COIN_HONEYMOON = "honeymoon"

DIVORCE_COST = 200                 # 金币/次（发起方付）
DIVORCE_TTL = 24 * 3600            # 对方 24h 内确认
COIN_DIVORCE = "divorce_fee"
COIN_DIVORCE_REFUND = "divorce_refund"   # 超时/拒绝时全额退回

RESET_COST = 15                    # 钻石
RESET_REASON = "marriage_reset_proposals"

# 子命令（都写在 `结婚` 的 args 里，不注册新的顶层词，见 `:595`）
SUB_ACCEPT = "同意"
SUB_REJECT = "拒绝"
SUB_MINE = "我的婚姻"
SUB_HONEYMOON = "度蜜月"
SUB_DIVORCE = "申请离婚"
SUB_DIVORCE_YES = "同意离婚"
SUB_DIVORCE_NO = "拒绝离婚"
SUB_STATS = "婚姻统计"
SUB_RESET = "清求婚次数"

SUBCMDS = frozenset(
    {SUB_ACCEPT, SUB_REJECT, SUB_MINE, SUB_HONEYMOON, SUB_DIVORCE,
     SUB_DIVORCE_YES, SUB_DIVORCE_NO, SUB_STATS, SUB_RESET}
)

USAGE = "结婚 @某人 / 结婚 我的婚姻·度蜜月·申请离婚·婚姻统计·清求婚次数"
TARGET_UNKNOWN = "没认出「{name}」是谁。让对方在本群说过话，或换成本群绑定过的称呼再试。"
SELF_TARGET = "不能对自己做这件事。"

# --- 另外四种关系（同一套 pair 状态机）--------------------------------------
KIND_SISTER = "sister"
KIND_BROTHER = "brother"
KIND_EMPLOY = "employ"
KIND_CONCUBINE = "concubine"

SUB_DISSOLVE = "解除"
SUB_WAGE = "工资"

WAGE_PER_DAY = 20                  # 雇佣日工资（金币，由雇主出；发放属第 4 批定时任务）
CONCUBINE_MIN_DIAMONDS = 30        # 准入门槛，不扣款（§4.4）

_KIN_SUBS = frozenset({SUB_ACCEPT, SUB_REJECT, SUB_DISSOLVE})

# --- 配对（纯计算，不落库：`hash(room, day, a, b)` 确定性）--------------------
MATCH_MAX = 100

#: 缘分分档：(下限, 评语)。按分数从高到低取第一个命中的。
MATCH_TIERS = (
    (90, "天生一对，群里其他人可以先退了"),
    (75, "很配，说你们没聊过我不信"),
    (60, "有戏，再处一处看"),
    (40, "普普通通，缘分得靠自己挣"),
    (20, "当朋友吧，别多想"),
    (0, "缘分未到，明天再摇一次"),
)

# --- 礼物（礼价与取物方式见 `core/items.py` 的模块 docstring）-----------------
COIN_GIFT = "gift_purchase"

# --- 宝宝（v1 基础版：看 / 喂养 / 改名 / 超极说明）-----------------------------
SUB_LOOK = "看"
SUB_FEED = "喂养"
SUB_RENAME = "改名"
SUB_SUPER = "超极"
BABY_SUBS = frozenset({SUB_LOOK, SUB_FEED, SUB_RENAME, SUB_SUPER})

BABY_FEED_COST = 20                 # 金币/次（文档未定，落地口径）
BABY_FEED_CAP = 3                   # 每日 3 次
BABY_FEED_KEY = "baby_feed"
COIN_BABY_FEED = "baby_feed"
BABY_CARE_GAIN = 10                 # 亲密度 +10，上限 100
BABY_EXP_GAIN = 5                   # 经验 +5

BABY_STAGES = ("胎儿", "婴儿", "幼儿", "儿童", "少年", "超极")
#: 升到下一阶段所需经验；`stage` 5（超极）v2 才开放，这里不给阈值。
BABY_STAGE_EXP = (0, 100, 300, 600, 1000)
BABY_NAME_MAX = 8


@dataclass(frozen=True)
class Relation:
    """一种 `pair` 关系的语义差异：只剩"叫什么、有哪些子命令、有什么门槛"。"""

    kind: str
    action: str
    label: str
    subs: frozenset
    needs_marriage: bool = False
    min_diamonds: int = 0
    wage: int = 0


RELATIONS = {
    "social.sister": Relation(KIND_SISTER, "social.sister", "闺蜜", _KIN_SUBS),
    "social.brother": Relation(KIND_BROTHER, "social.brother", "结拜", _KIN_SUBS),
    "social.concubine": Relation(
        KIND_CONCUBINE, "social.concubine", "纳妾", _KIN_SUBS,
        needs_marriage=True, min_diamonds=CONCUBINE_MIN_DIAMONDS,
    ),
    "social.employ": Relation(
        KIND_EMPLOY, "social.employ", "雇佣", _KIN_SUBS | {SUB_WAGE}, wage=WAGE_PER_DAY,
    ),
}


# ---------------------------------------------------------------------------
# 行读写
# ---------------------------------------------------------------------------
def _active_marriage(conn: sqlite3.Connection, room: str, uid: str):
    return _active_pair(conn, room, uid, KIND_MARRIAGE)


def _active_pair(conn: sqlite3.Connection, room: str, uid: str, kind: str):
    """某人在某类关系里的 active 行（结婚用它判"一人一婚"，其余 kind 允许并存）。"""
    return conn.execute(
        "SELECT * FROM pair WHERE room = ? AND kind = ? AND state = 'active' "
        "AND (low_uid = ? OR high_uid = ?) LIMIT 1",
        (room, kind, uid, uid),
    ).fetchone()


def _active_between(conn, room: str, low: str, high: str, kind: str):
    return conn.execute(
        "SELECT * FROM pair WHERE room = ? AND kind = ? AND state = 'active' "
        "AND low_uid = ? AND high_uid = ? LIMIT 1",
        (room, kind, low, high),
    ).fetchone()


def _active_all(conn, room: str, uid: str, kind: str):
    return conn.execute(
        "SELECT * FROM pair WHERE room = ? AND kind = ? AND state = 'active' "
        "AND (low_uid = ? OR high_uid = ?) ORDER BY id",
        (room, kind, uid, uid),
    ).fetchall()


def _pending_for(conn: sqlite3.Connection, room: str, uid: str, kind: str = KIND_MARRIAGE):
    """待**本人**确认的请求（`by_uid` 是对方，自己不可能是发起人）。"""
    return conn.execute(
        "SELECT * FROM pair WHERE room = ? AND kind = ? AND state = 'pending' "
        "AND (low_uid = ? OR high_uid = ?) AND by_uid <> ? "
        "ORDER BY id DESC LIMIT 1",
        (room, kind, uid, uid, uid),
    ).fetchone()


def _pending_between(conn: sqlite3.Connection, room: str, low: str, high: str,
                     kind: str = KIND_MARRIAGE):
    return conn.execute(
        "SELECT * FROM pair WHERE room = ? AND kind = ? AND state = 'pending' "
        "AND low_uid = ? AND high_uid = ? LIMIT 1",
        (room, kind, low, high),
    ).fetchone()


def _partner_of(row, uid: str) -> str:
    return row["high_uid"] if row["low_uid"] == uid else row["low_uid"]


def _meta(row) -> dict:
    raw = row["meta"]
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_meta(conn, pair_id: int, data: dict) -> None:
    conn.execute(
        "UPDATE pair SET meta = ? WHERE id = ?",
        (json.dumps(data, ensure_ascii=False, sort_keys=True), pair_id),
    )


def _counter_used(conn, *, room: str, uid: str, day: str, key: str, cap: int) -> int:
    row = conn.execute(
        "SELECT used FROM daily_counter WHERE room = ? AND uid = ? AND day = ? AND key = ?",
        (room, uid, day, key),
    ).fetchone()
    if row is None:
        return 0
    used = int(row[0])
    return used if used < cap else cap


def _counter_consume(conn, *, room: str, uid: str, day: str, key: str, cap: int, now: int) -> None:
    conn.execute(
        """
        INSERT INTO daily_counter (room, uid, day, key, used, cap, updated_at)
        VALUES (?, ?, ?, ?, 1, ?, ?)
        ON CONFLICT(room, uid, day, key)
        DO UPDATE SET used = used + 1, cap = excluded.cap, updated_at = excluded.updated_at
        """,
        (room, uid, day, key, cap, now),
    )


def _audit(conn, *, now: int, room: str, actor: str, action: str,
           target: str | None = None, result: str = "ok", detail: dict | None = None) -> None:
    """关系操作必须可追溯（`:1627`），`detail` 只写结构化字段，禁止写消息正文。"""
    conn.execute(
        "INSERT INTO audit (at, room, actor, action, target, result, detail) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (now, room, actor, action, target, result,
         json.dumps(detail, ensure_ascii=False, sort_keys=True) if detail else None),
    )


def _divorce_key(pair_id: int) -> str:
    return f"divorce:{pair_id}"


def _pair_id_of(divorce_key: str) -> int:
    """`divorce:<pair_id>` → pair_id；键名被手改坏时返回 0（调用方会当无效单丢弃）。"""
    try:
        return int(divorce_key.split(":", 1)[1])
    except (IndexError, ValueError):
        return 0


def _read_divorce(conn, room: str, pair_id: int) -> dict | None:
    row = conn.execute(
        "SELECT value FROM kv WHERE room = ? AND key = ?", (room, _divorce_key(pair_id))
    ).fetchone()
    if row is None:
        return None
    try:
        data = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_divorce(conn, room: str, pair_id: int, data: dict, now: int) -> None:
    conn.execute(
        """
        INSERT INTO kv (room, key, value, updated_at) VALUES (?, ?, ?, ?)
        ON CONFLICT(room, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (room, _divorce_key(pair_id), json.dumps(data, ensure_ascii=False, sort_keys=True), now),
    )


def _drop_divorce(conn, room: str, pair_id: int) -> None:
    conn.execute("DELETE FROM kv WHERE room = ? AND key = ?", (room, _divorce_key(pair_id)))


def _cooldown_left(conn, *, room: str, uid: str, key: str, now: int) -> int:
    row = conn.execute(
        "SELECT ready_at FROM cooldown WHERE room = ? AND uid = ? AND key = ?",
        (room, uid, key),
    ).fetchone()
    if row is None:
        return 0
    return max(0, int(row[0]) - now)


def _set_cooldown(conn, *, room: str, uid: str, key: str, ready_at: int) -> None:
    conn.execute(
        """
        INSERT INTO cooldown (room, uid, key, ready_at) VALUES (?, ?, ?, ?)
        ON CONFLICT(room, uid, key) DO UPDATE SET ready_at = excluded.ready_at
        """,
        (room, uid, key, ready_at),
    )


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------
def _first_name(args: str) -> str:
    """从参数里取「对方叫什么」：去掉前导 `@`，只取第一个分隔符之前的片段。"""
    text = args.strip()
    while text[:1] in ("@", "＠"):
        text = text[1:].lstrip()
    for sep in (" ", "#", "＃"):
        cut = text.find(sep)
        if cut > 0:
            text = text[:cut]
    return text.strip()


def _split_sub(args: str, subs: frozenset) -> tuple[str | None, str]:
    """`解除 @张三` → (`解除`, `@张三`)；首段不是子命令 → (None, 原参数)。"""
    text = (args or "").strip()
    if not text:
        return None, ""
    for sep in (" ", "\u3000", "#", "＃"):
        cut = text.find(sep)
        if 0 < cut:
            head, rest = text[:cut], text[cut + 1:].lstrip()
            break
    else:
        head, rest = text, ""
    head = normalize(head)
    return (head, rest) if head in subs else (None, text)


def _diamond_balance(conn, room: str, uid: str) -> int:
    try:
        return balance(conn, room=room, uid=uid, currency="diamond")
    except Exception:                     # 未建号 = 0 钻
        return 0


def _display(conn, room: str, uid: str) -> str:
    row = conn.execute(
        "SELECT nickname FROM player WHERE room = ? AND uid = ?", (room, uid)
    ).fetchone()
    name = (row["nickname"] if row else "") or ""
    return name or uid


def _split_target_item(args: str) -> tuple[str, str]:
    """`@小红 玫瑰` → (`@小红`, `玫瑰`)；`@小红#玫瑰` 同理（只拆第一个分隔符）。"""
    text = (args or "").strip()
    if not text:
        return "", ""
    for sep in (" ", "\u3000", "#", "＃"):
        cut = text.find(sep)
        if 0 < cut:
            return text[:cut].strip(), text[cut + 1:].strip()
    return text, ""


def _match_score(room: str, day: str, low: str, high: str) -> int:
    """确定性缘分分：同一天同一对永远同一个数，不需要落库。"""
    raw = f"{room}|{day}|{low}|{high}".encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    return int(digest[:8], 16) % (MATCH_MAX + 1)


def _gift_key(low: str, high: str) -> str:
    return f"gifts:{low}:{high}"


def _bump_gift_count(conn, room: str, low: str, high: str, now: int) -> None:
    a, b = (low, high) if low <= high else (high, low)
    conn.execute(
        """
        INSERT INTO kv (room, key, value, updated_at) VALUES (?, ?, '1', ?)
        ON CONFLICT(room, key)
        DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT),
                      updated_at = excluded.updated_at
        """,
        (room, _gift_key(a, b), now),
    )


def _stage_need(stage: int) -> int:
    """升到**下一**阶段所需的累计经验（`BABY_STAGE_EXP[i]` 是到第 i 阶的门槛）。

    `stage` 4（少年）之后是 v2 的超极，返回 0 表示 v1 不再升。
    """
    nxt = stage + 1
    return BABY_STAGE_EXP[nxt] if 0 <= nxt < len(BABY_STAGE_EXP) else 0


def _stage_for(exp: int, stage: int) -> int:
    """按累计经验推到能到的最高的、v1 允许的阶段（4 = 少年，5 留给 v2 超极）。"""
    reached = stage
    for index, need in enumerate(BABY_STAGE_EXP):
        if index == 0:
            continue
        if exp >= need:
            reached = index
    return min(reached, 4)


class SocialEngine:
    """`social` 引擎：婚姻段。

    与 `DailyEngine` 一样，时间源可注入（测试用 `FakeClock`），不碰模块级 `time`。
    """

    name = "social"

    def __init__(self, *, now_fn=None, logger=None):
        self._now = now_fn if now_fn is not None else clock.now_ts
        self._log = logger if logger is not None else logging.getLogger("playhub.engines.social")
        self._nick = ""            # 当前发言人昵称，`on_command` 入口赋值（回调单线程串行）

    # -- 卡面（§4.5：点人 → 主行 → 明细 → 当前）------------------------------
    def _ok(self, headline: str, rows=(), *, tail: str = "",
            action: str = "social.marry") -> Reply:
        return Reply(render.result(self._nick, headline, rows, tail=tail), action)

    def _no(self, text: str, *, tip: str = "", action: str = "social.marry") -> Reply:
        rows = [render.tip("💡", tip)] if tip else []
        return Reply(render.result(self._nick, f"❌ {text}", rows), action)

    # -- 分发 ---------------------------------------------------------------
    def on_command(self, cmd, msg=None, ctx=None, game=None) -> Reply | None:
        action = getattr(cmd, "action", "")
        if action == "social.marry":
            return self.marry(cmd, msg, ctx, game)
        relation = RELATIONS.get(action)
        if relation is not None:
            return self.relation(relation, cmd, msg, ctx, game)
        if action == "social.card":
            return self.card(cmd, msg, ctx, game)
        if action == "social.match":
            return self.match(cmd, msg, ctx, game)
        if action == "social.gift":
            return self.gift(cmd, msg, ctx, game)
        if action == "baby.view":
            return self.baby(cmd, msg, ctx, game)
        return None

    def marry(self, cmd, msg=None, ctx=None, game=None) -> Reply | None:
        room, uid = msg_room(msg), msg_uid(msg)
        if not room or not uid:
            return None
        store = store_of(game)
        now = self._now()
        nickname = msg_nickname(msg, ctx) or None
        self._nick = nickname or ""
        args = getattr(cmd, "args", "") or ""
        sub = normalize(args.strip())

        with store.tx() as conn:
            self._expire(conn, room, now)
            ensure_player(conn, room=room, uid=uid, nickname=nickname, now=now)
            if sub in SUBCMDS:
                return getattr(self, self._HANDLERS[sub])(conn, room, uid, now)
            target, err = self._resolve_target(cmd, ctx, room, uid, args)
            if target is None:
                return self._no(err or USAGE)
            return self.propose(conn, room, uid, target, now, args)

    _HANDLERS = {
        SUB_ACCEPT: "accept",
        SUB_REJECT: "reject",
        SUB_MINE: "mine",
        SUB_HONEYMOON: "honeymoon",
        SUB_DIVORCE: "divorce",
        SUB_DIVORCE_YES: "divorce_yes",
        SUB_DIVORCE_NO: "divorce_no",
        SUB_STATS: "stats",
        SUB_RESET: "reset",
    }

    @staticmethod
    def _mention_uid(cmd, self_uid: str) -> str | None:
        """取第一个认得出 wxid、且不是自己的艾特。"""
        for mention in getattr(cmd, "mentions", ()) or ():
            uid = getattr(mention, "uid", None)
            if uid and uid != self_uid:
                return uid
        return None

    def _resolve_target(self, cmd, ctx, room: str, self_uid: str, args: str) -> tuple[str | None, str | None]:
        """把「和谁」解析成 wxid：① 结构化艾特 ② `@昵称` / `昵称` 文本 → 成员表反查。

        框架的 `Message` 没有结构化艾特列表（`wxbot_framework/sdk.py:25-58` 只有 text/chat_id/data），
        所以真实群里只能走 ②：`ctx.members.resolve(昵称, 群)`。解析不到就明确说「没认出」，
        不静默、也不瞎猜一个 uid。
        """
        uid = self._mention_uid(cmd, self_uid)
        if uid:
            return uid, None
        name = _first_name(args)
        if not name:
            return None, USAGE
        members = getattr(ctx, "members", None) if ctx is not None else None
        resolve = getattr(members, "resolve", None)
        if not callable(resolve):
            return None, TARGET_UNKNOWN.format(name=name)
        try:
            member = resolve(name, room)
        except Exception:                 # 未登记/重名/表不可用，一律当成「没认出」
            self._log.debug("解析结婚对象失败：room=%s name=%s", room, name)
            return None, TARGET_UNKNOWN.format(name=name)
        target = getattr(member, "wxid", "") or ""
        if not target:
            return None, TARGET_UNKNOWN.format(name=name)
        if target == self_uid:
            return None, SELF_TARGET
        return target, None

    # -- 求婚 ---------------------------------------------------------------
    def propose(self, conn, room: str, uid: str, target: str, now: int, args: str) -> Reply:
        low, high = (uid, target) if uid <= target else (target, uid)
        if _active_marriage(conn, room, uid) is not None:
            return self._no("你已经有婚姻了，先申请离婚才能再求婚。",
                            tip="发「结婚 申请离婚」")
        if _active_marriage(conn, room, target) is not None:
            return self._no("对方已经有婚姻了。")
        if _pending_between(conn, room, low, high) is not None:
            return self._no("已经有一条待确认的求婚了，等对方回复吧。")

        day = clock.day_of(now)
        used = _counter_used(conn, room=room, uid=uid, day=day, key=PROPOSE_KEY, cap=PROPOSE_CAP)
        if used >= PROPOSE_CAP:
            return self._no(
                f"今天求婚次数用完了（{PROPOSE_CAP} 次/天），明天再来。",
                tip=f"发「结婚 清求婚次数」，{RESET_COST} 钻石清零",
            )
        _counter_consume(conn, room=room, uid=uid, day=day, key=PROPOSE_KEY, cap=PROPOSE_CAP, now=now)
        conn.execute(
            "INSERT INTO pair (room, kind, low_uid, high_uid, state, by_uid, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
            (room, KIND_MARRIAGE, low, high, uid, now),
        )
        _audit(conn, now=now, room=room, actor=uid, action="cmd:结婚", target=target,
               detail={"sub": "propose", "left_today": PROPOSE_CAP - used - 1})
        return self._ok(
            "💍 求婚已发出",
            [
                render.row("├ ", "等对方发「结婚 同意」就成婚"),
                render.row("⏳ ", "24 小时内有效，过期自动作废"),
                render.field("🔁 ", "今天还能求", f"{PROPOSE_CAP - used - 1} 次"),
            ],
        )

    # -- 同意 / 拒绝 ---------------------------------------------------------
    def accept(self, conn, room: str, uid: str, now: int) -> Reply:
        row = _pending_for(conn, room, uid)
        if row is None:
            return self._no("没有待你确认的求婚。")
        if _active_marriage(conn, room, uid) is not None:
            return self._no("你已经有婚姻了。")
        partner = _partner_of(row, uid)
        if _active_marriage(conn, room, partner) is not None:
            conn.execute(
                "UPDATE pair SET state = 'ended', ended_at = ? WHERE id = ?", (now, row["id"])
            )
            return self._no("对方已经有婚姻了，这条求婚作废。")
        conn.execute(
            "UPDATE pair SET state = 'active', since = ? WHERE id = ?", (now, row["id"])
        )
        _audit(conn, now=now, room=room, actor=uid, action="cmd:结婚", target=partner,
               detail={"sub": "accept", "pair_id": row["id"]})
        return self._ok(
            "💘 已成婚",
            [render.row("📅 ", "发「结婚 我的婚姻」看婚龄"),
             render.field("🌴 ", "度蜜月", f"{HONEYMOON_COST} 金币一次，24 小时冷却")],
        )

    def reject(self, conn, room: str, uid: str, now: int) -> Reply:
        row = _pending_for(conn, room, uid)
        if row is None:
            return self._no("没有待你确认的求婚。")
        conn.execute(
            "UPDATE pair SET state = 'ended', ended_at = ? WHERE id = ?", (now, row["id"])
        )
        _audit(conn, now=now, room=room, actor=uid, action="cmd:结婚",
               target=_partner_of(row, uid), detail={"sub": "reject", "pair_id": row["id"]})
        return self._ok("🕊 已拒绝这条求婚")

    # -- 我的婚姻 -----------------------------------------------------------
    def mine(self, conn, room: str, uid: str, now: int) -> Reply:
        row = _active_marriage(conn, room, uid)
        if row is None:
            pending = _pending_for(conn, room, uid)
            if pending is not None:
                return self._no(
                    "你目前未婚，但有一条待你确认的求婚。",
                    tip="发「结婚 同意」或「结婚 拒绝」",
                )
            return self._no("你目前未婚。", tip="发「结婚 @某人」开始")
        partner = _partner_of(row, uid)
        days = max(0, (now - int(row["since"] or now)) // 86400)
        count = int(_meta(row).get("honeymoon_count", 0))
        pending = _read_divorce(conn, room, row["id"])
        rows = [
            render.field("💍 ", "另一半", _display(conn, room, partner)),
            render.field("📅 ", "婚龄", f"{days} 天"),
            render.field("🌴 ", "蜜月", f"{count} 次"),
        ]
        tail = ""
        if pending is not None:
            who = "你" if pending.get("by") == uid else "对方"
            tail = f"⚠ {who}已提出离婚，24 小时内回「结婚 同意离婚」或「结婚 拒绝离婚」"
        return self._ok("💘 我的婚姻", rows, tail=tail)

    # -- 度蜜月 -------------------------------------------------------------
    def honeymoon(self, conn, room: str, uid: str, now: int) -> Reply:
        row = _active_marriage(conn, room, uid)
        if row is None:
            return self._no("你得先结婚。", tip="发「结婚 @某人」")
        pair_id = int(row["id"])
        key = f"honeymoon:{pair_id}"
        left = _cooldown_left(conn, room=room, uid=row["low_uid"], key=key, now=now)
        if left > 0:
            return self._no(f"刚度过蜜月，还要等 {max(1, left // 3600)} 小时。")
        try:
            entry = debit(
                conn, room=room, uid=uid, currency="coin", amount=HONEYMOON_COST,
                reason=COIN_HONEYMOON, operation_key=f"honeymoon:{pair_id}:{now}",
                posting_no=0, now=now, ref=str(pair_id),
            )
        except Insufficient as exc:
            return self._no(
                f"金币不够，度蜜月要 {HONEYMOON_COST} 金币。",
                tip=f"你现在有 {exc.balance} 金币，发「签到」攒一点",
            )
        except DuplicatePosting:
            self._log.warning("蜜月重复流水被拦截：room=%s pair=%s", room, pair_id)
            return self._no("这次蜜月已经记过账了。")
        meta = _meta(row)
        meta["honeymoon_count"] = int(meta.get("honeymoon_count", 0)) + 1
        _write_meta(conn, pair_id, meta)
        _set_cooldown(conn, room=room, uid=row["low_uid"], key=key, ready_at=now + HONEYMOON_COOLDOWN)
        _audit(conn, now=now, room=room, actor=uid, action="cmd:结婚",
               target=_partner_of(row, uid), detail={"sub": "honeymoon", "pair_id": pair_id})
        return self._ok(
            f"🌴 蜜月愉快（第 {meta['honeymoon_count']} 次）",
            [
                render.field("💰 ", "金币", f"-{HONEYMOON_COST}"),
                render.current("💰 ", "金币", entry.balance),
                render.row("⏳ ", "24 小时后才能再去"),
            ],
        )

    # -- 离婚 ---------------------------------------------------------------
    def divorce(self, conn, room: str, uid: str, now: int) -> Reply:
        row = _active_marriage(conn, room, uid)
        if row is None:
            return self._no("你得先结婚。", tip="发「结婚 @某人」")
        pair_id = int(row["id"])
        if _read_divorce(conn, room, pair_id) is not None:
            return self._no("已经有一条离婚申请在等对方确认。")
        try:
            debit(
                conn, room=room, uid=uid, currency="coin", amount=DIVORCE_COST,
                reason=COIN_DIVORCE, operation_key=f"divorce:{pair_id}:{now}",
                posting_no=0, now=now, ref=str(pair_id),
            )
        except Insufficient as exc:
            return self._no(
                f"金币不够，申请离婚要 {DIVORCE_COST} 金币。",
                tip=f"你现在有 {exc.balance}",
            )
        except DuplicatePosting:
            self._log.warning("离婚重复流水被拦截：room=%s pair=%s", room, pair_id)
            return self._no("这次离婚申请已经记过账了。")
        _write_divorce(conn, room, pair_id,
                       {"by": uid, "at": now, "deadline": now + DIVORCE_TTL,
                        "low": row["low_uid"], "high": row["high_uid"]}, now)
        _audit(conn, now=now, room=room, actor=uid, action="cmd:结婚",
               target=_partner_of(row, uid), detail={"sub": "divorce_apply", "pair_id": pair_id})
        return self._ok(
            "💔 离婚申请已发出",
            [
                render.field("💰 ", "已付手续费", f"{DIVORCE_COST} 金币"),
                render.row("⏳ ", "对方 24 小时内回「结婚 同意离婚」即解除"),
                render.row("🕊 ", "超时失效，手续费全额退回"),
            ],
        )

    def divorce_yes(self, conn, room: str, uid: str, now: int) -> Reply:
        row = _active_marriage(conn, room, uid)
        if row is None:
            return self._no("你得先结婚。", tip="发「结婚 @某人」")
        pair_id = int(row["id"])
        pending = _read_divorce(conn, room, pair_id)
        if pending is None:
            return self._no("没有待确认的离婚申请。")
        if pending.get("by") == uid:
            return self._no("离婚申请是你提的，得由对方确认。")
        conn.execute("UPDATE pair SET state = 'ended', ended_at = ? WHERE id = ?", (now, pair_id))
        _drop_divorce(conn, room, pair_id)
        _audit(conn, now=now, room=room, actor=uid, action="cmd:结婚",
               target=_partner_of(row, uid), detail={"sub": "divorce_ok", "pair_id": pair_id})
        return self._ok("💔 已解除婚姻关系")

    def divorce_no(self, conn, room: str, uid: str, now: int) -> Reply:
        row = _active_marriage(conn, room, uid)
        if row is None:
            return self._no("你得先结婚。", tip="发「结婚 @某人」")
        pair_id = int(row["id"])
        pending = _read_divorce(conn, room, pair_id)
        if pending is None:
            return self._no("没有待确认的离婚申请。")
        by = str(pending.get("by"))
        _drop_divorce(conn, room, pair_id)
        credit(
            conn, room=room, uid=by, currency="coin", amount=DIVORCE_COST,
            reason=COIN_DIVORCE_REFUND, operation_key=f"divorce_refund:{pair_id}:{now}",
            posting_no=0, now=now, ref=str(pair_id),
        )
        _audit(conn, now=now, room=room, actor=uid, action="cmd:结婚",
               target=by, detail={"sub": "divorce_refused", "pair_id": pair_id})
        return self._ok(
            "🕊 已拒绝离婚",
            [render.row("💰 ", f"{DIVORCE_COST} 金币已退回给对方")],
        )

    # -- 统计 / 清次数 -------------------------------------------------------
    def stats(self, conn, room: str, uid: str, now: int) -> Reply:
        row = _active_marriage(conn, room, uid)
        day = clock.day_of(now)
        proposed = conn.execute(
            "SELECT COUNT(*) FROM pair WHERE room = ? AND kind = ? AND by_uid = ?",
            (room, KIND_MARRIAGE, uid),
        ).fetchone()[0]
        divorced = conn.execute(
            "SELECT COUNT(*) FROM pair WHERE room = ? AND kind = ? AND state = 'ended' "
            "AND ended_at IS NOT NULL AND (low_uid = ? OR high_uid = ?)",
            (room, KIND_MARRIAGE, uid, uid),
        ).fetchone()[0]
        used = _counter_used(conn, room=room, uid=uid, day=day, key=PROPOSE_KEY, cap=PROPOSE_CAP)
        rows = []
        if row is not None:
            days = max(0, (now - int(row["since"] or now)) // 86400)
            rows.append(render.field("📅 ", "已婚", f"{days} 天"))
            rows.append(render.field("🌴 ", "蜜月", f"{int(_meta(row).get('honeymoon_count', 0))} 次"))
        else:
            rows.append(render.row("💤 ", "当前未婚"))
        rows.append(render.field("💍 ", "求婚发起", f"{int(proposed)} 次"))
        rows.append(render.field("🔁 ", "今天的求婚", f"{used}/{PROPOSE_CAP} 次"))
        rows.append(render.field("💔 ", "离婚", f"{int(divorced)} 次"))
        return self._ok("📊 婚姻统计", rows)

    def reset(self, conn, room: str, uid: str, now: int) -> Reply:
        day = clock.day_of(now)
        used = _counter_used(conn, room=room, uid=uid, day=day, key=PROPOSE_KEY, cap=PROPOSE_CAP)
        if used <= 0:
            return self._no("当前无需清空。", tip="今天还没求过婚，不用花钻石")
        try:
            debit(
                conn, room=room, uid=uid, currency="diamond", amount=RESET_COST,
                reason=RESET_REASON, operation_key=f"reset_proposals:{day}:{uid}",
                posting_no=0, now=now,
            )
        except Insufficient as exc:
            return self._no(
                f"钻石不够，清求婚次数要 {RESET_COST} 钻。",
                tip=f"你现在有 {exc.balance} 钻",
            )
        except DuplicatePosting:
            return self._no("今天的求婚次数已经清过一次了。")
        conn.execute(
            "DELETE FROM daily_counter WHERE room = ? AND uid = ? AND day = ? AND key = ?",
            (room, uid, day, PROPOSE_KEY),
        )
        _audit(conn, now=now, room=room, actor=uid, action="cmd:结婚",
               detail={"sub": "reset", "day": day})
        return self._ok(
            "🧹 求婚次数已清零",
            [render.field("💎 ", "钻石", f"-{RESET_COST}"),
             render.field("🔁 ", "今天还能求", f"{PROPOSE_CAP} 次")],
        )

    # -- 闺蜜 / 结拜 / 雇佣 / 纳妾（同一套流程）-------------------------------
    def relation(self, rel: Relation, cmd, msg=None, ctx=None, game=None) -> Reply | None:
        room, uid = msg_room(msg), msg_uid(msg)
        if not room or not uid:
            return None
        store = store_of(game)
        now = self._now()
        nickname = msg_nickname(msg, ctx) or None
        self._nick = nickname or ""
        args = getattr(cmd, "args", "") or ""

        with store.tx() as conn:
            self._expire(conn, room, now)
            ensure_player(conn, room=room, uid=uid, nickname=nickname, now=now)

            sub, rest = _split_sub(args, rel.subs)
            if sub == SUB_ACCEPT:
                return self.rel_accept(conn, room, uid, now, rel)
            if sub == SUB_REJECT:
                return self.rel_reject(conn, room, uid, now, rel)
            if sub == SUB_WAGE:
                return self.rel_wage(conn, room, uid, rel)
            if sub == SUB_DISSOLVE:
                target, err = self._resolve_target(cmd, ctx, room, uid, rest)
                if target is None:
                    return self._no(err or f"用法：{rel.label} 解除 @某人", action=rel.action)
                return self.rel_dissolve(conn, room, uid, target, now, rel)

            target, err = self._resolve_target(cmd, ctx, room, uid, args)
            if target is None:
                return self._no(
                    err or f"用法：{rel.label} @某人 / {rel.label} 同意·拒绝·解除 @某人",
                    action=rel.action,
                )
            return self.rel_propose(conn, room, uid, target, now, rel)

    def rel_propose(self, conn, room: str, uid: str, target: str, now: int,
                    rel: Relation) -> Reply:
        low, high = (uid, target) if uid <= target else (target, uid)
        if rel.needs_marriage and _active_marriage(conn, room, uid) is None:
            return self._no("得先有婚姻才能纳妾。", tip="发「结婚 @某人」", action=rel.action)
        if _pending_between(conn, room, low, high, rel.kind) is not None:
            return self._no(f"已经有一条待确认的{rel.label}邀请，等对方回复吧。", action=rel.action)
        if _active_between(conn, room, low, high, rel.kind) is not None:
            return self._no(f"你们已经是{rel.label}关系了。", action=rel.action)
        if rel.min_diamonds:
            got = _diamond_balance(conn, room, uid)
            if got < rel.min_diamonds:
                return self._no(
                    f"{rel.label}要求钻石余额 ≥ {rel.min_diamonds}。",
                    tip=f"你现在有 {got} 钻；这是门槛不扣款",
                    action=rel.action,
                )
        conn.execute(
            "INSERT INTO pair (room, kind, low_uid, high_uid, state, by_uid, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
            (room, rel.kind, low, high, uid, now),
        )
        _audit(conn, now=now, room=room, actor=uid, action=f"cmd:{rel.label}", target=target,
               detail={"sub": "propose"})
        return self._ok(
            f"📨 {rel.label}邀请已发出",
            [render.row("├ ", f"等对方发「{rel.label} 同意」"),
             render.row("⏳ ", "24 小时内有效，过期自动作废")],
            action=rel.action,
        )

    def rel_accept(self, conn, room: str, uid: str, now: int, rel: Relation) -> Reply:
        row = _pending_for(conn, room, uid, rel.kind)
        if row is None:
            return self._no(f"没有待你确认的{rel.label}邀请。", action=rel.action)
        meta = _meta(row)
        if rel.wage:
            meta["wage"] = rel.wage
        conn.execute(
            "UPDATE pair SET state = 'active', since = ?, meta = ? WHERE id = ?",
            (now, json.dumps(meta, ensure_ascii=False, sort_keys=True), row["id"]),
        )
        _audit(conn, now=now, room=room, actor=uid, action=f"cmd:{rel.label}",
               target=row["by_uid"], detail={"sub": "accept"})
        return self._ok(f"🤝 已结成{rel.label}关系", action=rel.action)

    def rel_reject(self, conn, room: str, uid: str, now: int, rel: Relation) -> Reply:
        row = _pending_for(conn, room, uid, rel.kind)
        if row is None:
            return self._no(f"没有待你确认的{rel.label}邀请。", action=rel.action)
        conn.execute(
            "UPDATE pair SET state = 'ended', ended_at = ? WHERE id = ?", (now, row["id"])
        )
        _audit(conn, now=now, room=room, actor=uid, action=f"cmd:{rel.label}",
               target=row["by_uid"], detail={"sub": "reject"})
        return self._ok(f"🕊 已拒绝这次{rel.label}邀请", action=rel.action)

    def rel_dissolve(self, conn, room: str, uid: str, target: str, now: int,
                     rel: Relation) -> Reply:
        low, high = (uid, target) if uid <= target else (target, uid)
        row = _active_between(conn, room, low, high, rel.kind)
        if row is None:
            pending = _pending_between(conn, room, low, high, rel.kind)
            if pending is not None:
                conn.execute(
                    "UPDATE pair SET state = 'ended', ended_at = ? WHERE id = ?",
                    (now, pending["id"]),
                )
                _audit(conn, now=now, room=room, actor=uid, action=f"cmd:{rel.label}",
                       target=target, detail={"sub": "cancel"})
                return self._ok(f"↩️ 已撤回这条{rel.label}邀请", action=rel.action)
            return self._no(f"你们之间没有{rel.label}关系。", action=rel.action)
        conn.execute(
            "UPDATE pair SET state = 'ended', ended_at = ? WHERE id = ?", (now, row["id"])
        )
        _audit(conn, now=now, room=room, actor=uid, action=f"cmd:{rel.label}",
               target=target, detail={"sub": "dissolve"})
        return self._ok(f"💔 {rel.label}关系已解除", action=rel.action)

    def rel_wage(self, conn, room: str, uid: str, rel: Relation) -> Reply:
        rows = _active_all(conn, room, uid, rel.kind)
        if not rows:
            return self._no("你还没有雇佣关系。", tip="发「雇佣 @某人」", action=rel.action)
        lines = []
        for row in rows:
            wage = int(_meta(row).get("wage", rel.wage) or rel.wage)
            if uid == row["by_uid"]:
                lines.append(f"{_display(conn, room, _partner_of(row, uid))}：每日 {wage} 金币")
            else:
                lines.append(f"被 {_display(conn, room, row['by_uid'])} 雇佣：每日 {wage} 金币")
        return self._ok(
            "💼 雇佣工资",
            [render.row("├ ", line) for line in lines],
            tail="实际发放由定时任务执行（第 4 批接入），这一屏只是约定，不是余额",
            action=rel.action,
        )

    # -- 关系卡 -------------------------------------------------------------
    def card(self, cmd, msg=None, ctx=None, game=None) -> Reply | None:
        """`关系卡`：不看的人省略参数就是自己；`关系卡 @某人` 看别人。"""
        room, uid = msg_room(msg), msg_uid(msg)
        if not room or not uid:
            return None
        store = store_of(game)
        args = getattr(cmd, "args", "") or ""
        nickname = msg_nickname(msg, ctx) or None
        self._nick = nickname or ""
        with store.tx() as conn:
            # 卡上要显示昵称，靠 `player.nickname`；没打过交道的账号先补一行。
            ensure_player(conn, room=room, uid=uid, nickname=nickname, now=self._now())
            target = uid
            if args.strip():
                target, err = self._resolve_target(cmd, ctx, room, uid, args)
                if target is None:
                    return self._no(err or "用法：关系卡 / 关系卡 @某人", action="social.card")
            return self.card_of(conn, room, target, self._now())

    def card_of(self, conn, room: str, target: str, now: int) -> Reply:
        name = _display(conn, room, target)
        rows: list[str] = []
        marriage = _active_pair(conn, room, target, KIND_MARRIAGE)
        if marriage is None:
            rows.append(render.row("💤 ", "未婚"))
        else:
            days = max(0, (now - int(marriage["since"] or now)) // 86400)
            partner = _display(conn, room, _partner_of(marriage, target))
            rows.append(render.field("💍 ", "婚姻", f"{partner} · {days} 天"))
        for kind, label, icon in (
            (KIND_SISTER, "闺蜜", "👭"),
            (KIND_BROTHER, "结拜", "🤝"),
            (KIND_EMPLOY, "雇佣", "🧾"),
            (KIND_CONCUBINE, "纳妾", "🏮"),
        ):
            found = _active_all(conn, room, target, kind)
            if not found:
                continue
            names = "、".join(_display(conn, room, _partner_of(row, target)) for row in found[:3])
            more = f" 等 {len(found)} 人" if len(found) > 3 else ""
            rows.append(render.field(icon + " ", label, f"{names}{more}"))
        babies = conn.execute(
            "SELECT name FROM baby WHERE room = ? AND (owner_uid = ? OR parent_low = ? OR parent_high = ?) "
            "ORDER BY id",
            (room, target, target, target),
        ).fetchall()
        if babies:
            names = "、".join(str(row["name"]) for row in babies[:3])
            rows.append(render.field("🍼 ", "宝宝", names))
        tail = "🫥 这张卡几乎是空的，去认识点人吧" if rows == [render.row("💤 ", "未婚")] else ""
        return Reply(render.result(name, "🪪 关系卡", rows, tail=tail), "social.card")

    # -- 配对（纯计算，不落库）----------------------------------------------
    def match(self, cmd, msg=None, ctx=None, game=None) -> Reply | None:
        room, uid = msg_room(msg), msg_uid(msg)
        if not room or not uid:
            return None
        store = store_of(game)
        now = self._now()
        self._nick = msg_nickname(msg, ctx) or ""
        args = getattr(cmd, "args", "") or ""
        with store.tx() as conn:
            if not args.strip():
                return self._no("配对要看两个人的缘分。", tip="发「配对 @某人」", action="social.match")
            target, err = self._resolve_target(cmd, ctx, room, uid, args)
            if target is None:
                return self._no(err or "用法：配对 @某人", action="social.match")
            low, high = (uid, target) if uid <= target else (target, uid)
            score = _match_score(room, clock.day_of(now), low, high)
            tier = next(text for floor, text in MATCH_TIERS if score >= floor)
            other = _display(conn, room, target)
            if other == target:
                # 对方还没在群里说过话 → `player` 里没昵称，用指令里写的那个人名兜底
                other = _first_name(args) or target
            rows = [
                render.field("💞 ", "对象", other),
                render.field("📊 ", "缘分指数", f"{score}/{MATCH_MAX}"),
                render.row("💬 ", f"评语：{tier}"),
            ]
            return Reply(
                render.result(self._nick, "💞 配对结果", rows,
                              tail="⏳ 同一对今天不变，明天重新摇"),
                "social.match",
            )

    # -- 礼物 ---------------------------------------------------------------
    def gift(self, cmd, msg=None, ctx=None, game=None) -> Reply | None:
        room, uid = msg_room(msg), msg_uid(msg)
        if not room or not uid:
            return None
        store = store_of(game)
        now = self._now()
        nickname = msg_nickname(msg, ctx) or None
        self._nick = nickname or ""
        args = getattr(cmd, "args", "") or ""
        target_name, item_text = _split_target_item(args)
        with store.tx() as conn:
            ensure_player(conn, room=room, uid=uid, nickname=nickname, now=now)
            if not target_name or not item_text:
                return self._no(
                    "礼物要写清楚送给谁、送什么。",
                    tip=f"发「礼物 @某人 玫瑰」；能送的有：{items.gift_price_text()}",
                    action="social.gift",
                )
            target, err = self._resolve_target(cmd, ctx, room, uid, target_name)
            if target is None:
                return self._no(err or "用法：礼物 @某人 玫瑰", action="social.gift")
            items.seed(conn)
            gift = items.find_by_name(conn, item_text)
            if gift is None:
                return self._no(
                    f"没有叫「{item_text}」的礼物。",
                    tip=f"能送的有：{items.gift_price_text()}",
                    action="social.gift",
                )
            paid = 0
            balance_after: int | None = None
            if items.take(conn, room, uid, gift.item_id):
                source = "背包取 1 件"
            elif gift.price > 0:
                try:
                    entry = debit(
                        conn, room=room, uid=uid, currency="coin", amount=gift.price,
                        reason=COIN_GIFT,
                        operation_key=f"gift:{uid}:{target}:{gift.item_id}:{now}",
                        posting_no=0, now=now, ref=gift.item_id,
                    )
                except Insufficient as exc:
                    return self._no(
                        f"金币不够，{gift.name}要 {gift.price} 金币。",
                        tip=f"你现在有 {exc.balance} 金币，发「签到」攒一点",
                        action="social.gift",
                    )
                except DuplicatePosting:
                    self._log.warning("送礼重复流水被拦截：room=%s item=%s", room, gift.item_id)
                    return self._no("这次送礼已经记过账了。", action="social.gift")
                paid = gift.price
                balance_after = entry.balance
                source = f"代买，{gift.price} 金币"
            else:
                return self._no(f"{gift.name}是非卖品，得先抽到才能送。", action="social.gift")
            _bump_gift_count(conn, room, uid, target, now)
            _audit(conn, now=now, room=room, actor=uid, action="cmd:礼物",
                   target=target, detail={"item": gift.item_id, "paid": paid})
            target_label = _display(conn, room, target)
            if target_label == target:
                # 收礼人还没在群里说过话 → 用指令里写的人名兜底
                target_label = target_name.lstrip("@") or target
            rows = [
                render.field("🎁 ", "礼物", f"{gift.icon} {gift.name} ×1"),
                render.field("📦 ", "来源", source),
                render.row("💌 ", f"收礼人：{target_label}"),
            ]
            if balance_after is not None:
                rows.append(render.current("💰 ", "金币", balance_after))
            return Reply(render.result(self._nick, "🎁 礼物已送到", rows), "social.gift")

    # -- 宝宝（v1 基础版）---------------------------------------------------
    def baby(self, cmd, msg=None, ctx=None, game=None) -> Reply | None:
        room, uid = msg_room(msg), msg_uid(msg)
        if not room or not uid:
            return None
        store = store_of(game)
        now = self._now()
        nickname = msg_nickname(msg, ctx) or None
        self._nick = nickname or ""
        args = getattr(cmd, "args", "") or ""
        with store.tx() as conn:
            ensure_player(conn, room=room, uid=uid, nickname=nickname, now=now)
            sub, rest = _split_sub(args, BABY_SUBS)
            if sub is None:
                return self._no(
                    "宝宝要带子命令。",
                    tip="宝宝 看 / 宝宝 喂养 / 宝宝 改名 新名字 / 宝宝 超极",
                    action="baby.view",
                )
            if sub == SUB_SUPER:
                return self._no(
                    "超极宝宝 v2 才开放。",
                    tip="v1 只有 看 / 喂养 / 改名，超极不升级也不收费",
                    action="baby.view",
                )
            row = self._baby_of(conn, room, uid)
            if row is None:
                if sub == SUB_RENAME:
                    return self._no("你还没有宝宝。", tip="发「宝宝 看」先领一个",
                                    action="baby.view")
                row = self._bear(conn, room, uid, now)
            if sub == SUB_LOOK:
                return self._baby_card(conn, room, row, now)
            if sub == SUB_RENAME:
                return self._baby_rename(conn, room, uid, row, rest, now)
            return self._baby_feed(conn, room, uid, row, now)

    @staticmethod
    def _baby_of(conn, room: str, uid: str):
        return conn.execute(
            "SELECT * FROM baby WHERE room = ? AND (owner_uid = ? OR parent_low = ? OR parent_high = ?) "
            "ORDER BY id LIMIT 1",
            (room, uid, uid, uid),
        ).fetchone()

    def _bear(self, conn, room: str, uid: str, now: int):
        """领养一个宝宝（v1 没有独立的出生命令，`宝宝 看` / `宝宝 喂养` 时自动领）。"""
        marriage = _active_pair(conn, room, uid, KIND_MARRIAGE)
        if marriage is not None:
            low, high = str(marriage["low_uid"]), str(marriage["high_uid"])
        else:
            low, high = uid, None
        base = f"{_display(conn, room, uid)}的宝宝"
        name, n = base, 2
        while conn.execute("SELECT 1 FROM baby WHERE room = ? AND name = ?", (room, name)).fetchone():
            name = f"{base}{n}"
            n += 1
        cursor = conn.execute(
            "INSERT INTO baby (room, name, owner_uid, parent_low, parent_high, born_at, "
            "stage, exp, care, is_super, last_care_at) VALUES (?, ?, ?, ?, ?, ?, 0, 0, 100, 0, ?)",
            (room, name, uid, low, high, now, now),
        )
        _audit(conn, now=now, room=room, actor=uid, action="cmd:宝宝",
               detail={"sub": "bear", "name": name, "baby_id": cursor.lastrowid})
        return conn.execute("SELECT * FROM baby WHERE id = ?", (cursor.lastrowid,)).fetchone()

    def _baby_card(self, conn, room: str, row, now: int) -> Reply:
        stage = int(row["stage"])
        exp = int(row["exp"])
        need = _stage_need(stage)
        rows = [
            render.field("🏷 ", "名字", str(row["name"])),
            render.field("🌱 ", "阶段", f"{BABY_STAGES[min(stage, 5)]}（{stage}/5）"),
            render.field("💗 ", "亲密度", f"{int(row['care'])}/100"),
            render.field("⭐ ", "经验", f"{exp}/{need}" if need else f"{exp}"),
            render.row("📅 ", f"出生：{clock.day_of(int(row['born_at']))}"),
        ]
        return Reply(render.result(self._nick, "🍼 我的宝宝", rows,
                                   tail=f"💡 发「宝宝 喂养」喂一顿，{BABY_FEED_COST} 金币"),
                     "baby.view")

    def _baby_feed(self, conn, room: str, uid: str, row, now: int) -> Reply:
        day = clock.day_of(now)
        used = _counter_used(conn, room=room, uid=uid, day=day, key=BABY_FEED_KEY, cap=BABY_FEED_CAP)
        if used >= BABY_FEED_CAP:
            return self._no(
                f"今天喂过 {BABY_FEED_CAP} 次了，宝宝吃不下了。",
                tip="明天再来，或者发「宝宝 看」陪它待会儿",
                action="baby.view",
            )
        try:
            entry = debit(
                conn, room=room, uid=uid, currency="coin", amount=BABY_FEED_COST,
                reason=COIN_BABY_FEED,
                operation_key=f"baby_feed:{uid}:{day}:{used}",
                posting_no=0, now=now, ref=str(row["id"]),
            )
        except Insufficient as exc:
            return self._no(
                f"金币不够，喂一次要 {BABY_FEED_COST} 金币。",
                tip=f"你现在有 {exc.balance} 金币，发「签到」攒一点",
                action="baby.view",
            )
        except DuplicatePosting:
            return self._no("这次喂养已经记过账了。", action="baby.view")
        _counter_consume(conn, room=room, uid=uid, day=day, key=BABY_FEED_KEY,
                         cap=BABY_FEED_CAP, now=now)
        care = min(100, int(row["care"]) + BABY_CARE_GAIN)
        exp = int(row["exp"]) + BABY_EXP_GAIN
        stage = _stage_for(exp, int(row["stage"]))
        conn.execute(
            "UPDATE baby SET care = ?, exp = ?, stage = ?, last_care_at = ? WHERE id = ?",
            (care, exp, stage, now, row["id"]),
        )
        _audit(conn, now=now, room=room, actor=uid, action="cmd:宝宝",
               detail={"sub": "feed", "baby_id": int(row["id"]), "stage": stage})
        rows = [
            render.field("💗 ", "亲密度", f"+{BABY_CARE_GAIN} → {care}/100"),
            render.field("⭐ ", "经验", f"+{BABY_EXP_GAIN} → {exp}/{_stage_need(stage)}"),
            render.field("💰 ", "金币", f"-{BABY_FEED_COST}"),
            render.current("💰 ", "金币", entry.balance),
            render.field("🔁 ", "今天还能喂", f"{BABY_FEED_CAP - used - 1} 次"),
        ]
        if stage > int(row["stage"]):
            rows.insert(0, render.row("🎉 ", f"长到「{BABY_STAGES[min(stage, 5)]}」了"))
        return Reply(render.result(self._nick, "🍼 喂饱了", rows), "baby.view")

    def _baby_rename(self, conn, room: str, uid: str, row, rest: str, now: int) -> Reply:
        name = (rest or "").strip()
        if not name:
            return self._no("改名要写新名字。", tip="发「宝宝 改名 小豆丁」", action="baby.view")
        if len(name) > BABY_NAME_MAX:
            return self._no(
                f"名字最多 {BABY_NAME_MAX} 个字。", tip=f"「{name}」有 {len(name)} 个字",
                action="baby.view",
            )
        taken = conn.execute(
            "SELECT 1 FROM baby WHERE room = ? AND name = ? AND id <> ?", (room, name, row["id"])
        ).fetchone()
        if taken:
            return self._no(f"群里已经有叫「{name}」的宝宝了。", action="baby.view")
        old = str(row["name"])
        conn.execute("UPDATE baby SET name = ? WHERE id = ?", (name, row["id"]))
        _audit(conn, now=now, room=room, actor=uid, action="cmd:宝宝",
               detail={"sub": "rename", "baby_id": int(row["id"]), "from": old, "to": name})
        return Reply(
            render.result(self._nick, "🏷 改名成功",
                          [render.field("🏷 ", "旧名字", old),
                           render.field("🏷 ", "新名字", name)]),
            "baby.view",
        )

    # -- 超时收尾（惰性，见模块 docstring）-----------------------------------
    def expire_room(self, conn, room: str, now: int) -> int:
        """后台定时收尾（`§4.2` 的 `expire` 动作）：返回本次真正收掉的单数。

        前台子命令还会惰性调一次，两边同路径、同 `operation_key`，谁先到谁生效，
        后到的只会撞 `DuplicatePosting`，不会重复退款（`§4.2.9`）。
        """
        return self._expire(conn, room, now)

    def _expire(self, conn, room: str, now: int) -> int:
        """收 pending 与到期离婚；返回收掉的数量（后台动作的计数用）。"""
        # 所有 kind 的 pending 统一 24h 失效：否则 `pair_active_pair` 会把两人永久锁在
        # 「有一条待确认请求」上，五类关系都会卡死。
        cursor = conn.execute(
            "UPDATE pair SET state = 'ended', ended_at = ? "
            "WHERE room = ? AND state = 'pending' AND created_at <= ?",
            (now, room, now - PROPOSE_TTL),
        )
        expired = int(cursor.rowcount or 0)
        rows = conn.execute(
            "SELECT key, value FROM kv WHERE room = ? AND key LIKE 'divorce:%'", (room,)
        ).fetchall()
        for divorce_key, raw in rows:
            try:
                data = json.loads(raw)
                by = str(data["by"])
                deadline = int(data["deadline"])
            except (TypeError, ValueError, KeyError):
                conn.execute("DELETE FROM kv WHERE room = ? AND key = ?", (room, divorce_key))
                continue
            if deadline > now:
                continue
            pair_id = _pair_id_of(divorce_key)
            _drop_divorce(conn, room, pair_id)
            conn.execute(
                "UPDATE pair SET state = 'ended', ended_at = ? WHERE id = ? AND state = 'active'",
                (now, pair_id),
            )
            credit(
                conn, room=room, uid=by, currency="coin", amount=DIVORCE_COST,
                reason=COIN_DIVORCE_REFUND, operation_key=f"divorce_timeout:{pair_id}",
                posting_no=0, now=now, ref=str(pair_id),
            )
            _audit(conn, now=now, room=room, actor=by, action="cmd:结婚",
                   detail={"sub": "divorce_timeout", "pair_id": pair_id})
            expired += 1
        return expired
