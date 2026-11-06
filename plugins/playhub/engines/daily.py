"""签到 + 抽签引擎（`docs/design/playhub.md:1291-1310`、`:1727`、`§7 第 1 批` 验收标准 1）。

数值一字不改地照 §4.4 / §1727 落地：

- 签到金币 = `30 + 5 × min(连签天数-1, 10)`，即 30 → 80 封顶，每日 1 次。
- 连签天数 `% 7 == 0` 时额外 **+3 钻石**（第 7、14、21… 天都给，见 `:1727`）。
- 断签（隔天以上）连签归 1。
- 抽签：每日 1 次，五档共 12 份；上上签 1/12 → 30 金币 + 3 钻石。

跨批边界：**本批签到只发金币与钻石，不发金箱**。金箱（`core/items.py`）属第 3 批，
按 §7「不追溯补发」——第 3 批上线前签过的日子不会再补一个金箱。

写路径：余额只经 `core/wallet.py` 的 `credit()`，引擎里不出现任何
`INSERT INTO wallet` / `UPDATE wallet`；每日次数落 `daily_counter`、连签落 `kv`，
两张表都是 `schema.sql` 给签到指定的表（`:1165`）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from ..core import balance, clock, credit, ensure_player, items, kv, render
from ..core.errors import DuplicatePosting
from . import Reply, msg_nickname, msg_room, msg_uid, store_of

# ---------------------------------------------------------------------------
# 签到数值（§4.4 / §1727）
# ---------------------------------------------------------------------------
SIGNIN_BASE = 30                 # 首日（连签 1 天）
SIGNIN_STEP = 5                  # 每多一天 +5
SIGNIN_STREAK_CAP = 10           # min(连签-1, 10) 的 10
SIGNIN_COIN_MAX = SIGNIN_BASE + SIGNIN_STEP * SIGNIN_STREAK_CAP   # 80
SIGNIN_DIAMOND_EVERY = 7         # 连签天数 % 7 == 0 时给钻石
SIGNIN_DIAMOND_BONUS = 3         # 每次 +3 钻石
SIGNIN_KEY = "signin"            # daily_counter.key
SIGNIN_CAP = 1                   # 每日 1 次
SIGNIN_REASON = "signin"                 # 金币流水 reason
SIGNIN_DIAMOND_REASON = "signin_day7"    # 钻石流水 reason（§1093 正向枚举之一）

# ---------------------------------------------------------------------------
# 抽签数值（§4.4 签等表，12 份概率）
# ---------------------------------------------------------------------------
FORTUNE_KEY = "fortune"
FORTUNE_CAP = 1                  # 每日 1 次
FORTUNE_REASON = "fortune"               # 金币流水 reason
FORTUNE_DIAMOND_REASON = "fortune_best"  # 钻石流水 reason（§1093 正向枚举之一）


@dataclass(frozen=True)
class FortuneTier:
    """一个签档：`weight` 是 12 份里的份数，不是百分比。"""

    name: str
    weight: int
    coin: int
    diamond: int = 0


# §4.4：上上签 1/12 → 30 金币 +3 钻；上吉 2/12 → 20；中吉 4/12 → 12；
# 小吉 3/12 → 8；末吉 2/12 → 5。顺序即抽签区间顺序（1+2+4+3+2 = 12）。
FORTUNE_TIERS: tuple[FortuneTier, ...] = (
    FortuneTier("上上签", 1, 30, 3),
    FortuneTier("上吉", 2, 20),
    FortuneTier("中吉", 4, 12),
    FortuneTier("小吉", 3, 8),
    FortuneTier("末吉", 2, 5),
)
FORTUNE_TOTAL_WEIGHT = sum(t.weight for t in FORTUNE_TIERS)


@dataclass(frozen=True)
class FortuneEntry:
    """一条签：`id` 供跨天/解签定位，`no` 是签号，`jie` 是给「解签」看的白话释义。"""

    id: str
    no: int
    tier: str
    text: str
    jie: str = ""


if FORTUNE_TOTAL_WEIGHT != 12:      # 加载期硬校验，不许靠「差不多」
    raise ValueError(f"§4.4 抽签总权重必须是 12 份，实际 {FORTUNE_TOTAL_WEIGHT}")

FORTUNE_NOT_READY = "签文库未就绪"
#: 当天的签记在 kv（按群 + 人，每天覆盖），解签只认「今天」这一条。
FORTUNE_PICK_PREFIX = "fortune.pick"
FORTUNE_NO_JIE = "这一签的释义还没写好"
FORTUNE_EXPLAIN_MISSING = "🌙 今天还没抽签，先发「抽签」再来解签吧"
#: 小V 版式的虚线分隔（19 个半角连字符，`docs/design/xiaov-cmd/images.md`「## 抽签」）
FORTUNE_DASH = "-" * 19
SIGNIN_ALREADY = "今天已经签过了，明天再来吧"
FORTUNE_ALREADY = "今天已经抽过签了，明天再来吧"

# 签文库文件名（§5.2 的 <cat>.jsonl）
FORTUNE_BANK = "fortune.jsonl"

_REPO_ROOT = Path(__file__).resolve().parents[3]   # plugins/playhub/engines/daily.py → 仓库根


# ---------------------------------------------------------------------------
# 纯函数：数值
# ---------------------------------------------------------------------------
def signin_coin(streak: int) -> int:
    """`30 + 5 × min(连签天数-1, 10)`；连签 <=1 时按第 1 天算。"""
    days = max(int(streak), 1)
    return SIGNIN_BASE + SIGNIN_STEP * min(days - 1, SIGNIN_STREAK_CAP)


def signin_diamond(streak: int) -> int:
    """连签天数 `% 7 == 0` 给 3 钻（第 7、14、21… 天），否则 0。"""
    days = int(streak)
    return SIGNIN_DIAMOND_BONUS if days > 0 and days % SIGNIN_DIAMOND_EVERY == 0 else 0


# ---------------------------------------------------------------------------
# 纯函数：日界与连签
# ---------------------------------------------------------------------------
def _streak_key(uid: str) -> str:
    return f"signin.streak.{uid}"


def _pick_key(uid: str) -> str:
    """当天签的 kv 键（按群 + 人；值 `{"day": ..., "id": ...}`，每天覆盖）。"""
    return f"{FORTUNE_PICK_PREFIX}.{uid}"


def _yesterday(day: str) -> str:
    return (date.fromisoformat(day) - timedelta(days=1)).isoformat()


def next_streak(last_day: str, last_streak: int, today: str) -> int:
    """按上一次签到日推今天的连签：正好昨天 → +1；其余（含隔天以上、跨月）→ 1。"""
    if last_day and last_streak > 0 and last_day == _yesterday(today):
        return last_streak + 1
    return 1


def _read_streak(conn, room: str, uid: str) -> tuple[str, int]:
    """读 kv 里的 (上次签到日, 连签天数)；坏数据一律当「没签过」，绝不让它卡住签到。"""
    row = conn.execute(
        "SELECT value FROM kv WHERE room = ? AND key = ?", (room, _streak_key(uid))
    ).fetchone()
    if row is None:
        return "", 0
    try:
        data = json.loads(row[0])
        day = data.get("day")
        streak = int(data.get("streak", 0))
    except (TypeError, ValueError, AttributeError):
        return "", 0
    if not isinstance(day, str) or streak < 0:
        return "", 0
    return day, streak


def _write_streak(conn, room: str, uid: str, day: str, streak: int, now: int) -> None:
    payload = json.dumps({"day": day, "streak": streak}, ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO kv (room, key, value, updated_at) VALUES (?, ?, ?, ?)
        ON CONFLICT(room, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (room, _streak_key(uid), payload, now),
    )


# ---------------------------------------------------------------------------
# 纯函数：每日次数
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# 签文库（§5.2 路径优先级：① 账号工作区 override → ② 仓库默认；不做跨层合并）
# ---------------------------------------------------------------------------
def fortune_bank_paths(store, *, repo_root: Path | None = None) -> tuple[Path, Path, ...]:
    """两条候选路径，按优先级排列。

    ① `<账号工作区>/.runtime/playhub/content/fortune.jsonl`：Store 的 db 就在
       `.runtime/playhub/playhub.sqlite3`（§6.1），所以 `db_path.parent/content/`。
    ② `<仓库根>/content/playhub/fortune.jsonl`：随包发布、进 git 的默认题库。
    §5.2 的 ③「内置兜底示例」属 `content/loader.py`（后续批次），本批不实现——
    宁可回「签文库未就绪」，也不自己编签文。
    """
    local = Path(store.db_path).parent / "content" / FORTUNE_BANK
    root = Path(repo_root) if repo_root is not None else _REPO_ROOT
    return (local, root / "content" / "playhub" / FORTUNE_BANK)


def _line_text(row: dict) -> str | None:
    """从一行 JSON 里取签文。

    §5.3 只规定了题库的通用字段（`id`/`q`/`a`/`hint`/`d`/`tag`/`src`），
    没给 `fortune.jsonl` 的专用字段名，所以按 `text` → `q` → `a[0]` 依次取，
    都是为了不把一行合法数据判成坏行；一个都取不到才算坏行。
    """
    for field in ("text", "q"):
        value = row.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    answers = row.get("a")
    if isinstance(answers, list):
        for value in answers:
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _line_tier(row: dict) -> str | None:
    """签等：`tier` 优先，其次 `tag[0]`（同上，兼容两种写法）。"""
    value = row.get("tier")
    if isinstance(value, str) and value.strip():
        return value.strip()
    tags = row.get("tag")
    if isinstance(tags, list):
        for value in tags:
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _line_jie(row: dict) -> str:
    """释义：`jie` 优先；没有就空串，解签时回「这一签的释义还没写好」。"""
    value = row.get("jie")
    return value.strip() if isinstance(value, str) else ""


def _line_no(row: dict, lineno: int) -> int:
    """签号：`no` 是正整数；缺号/坏号用文件行号兜底（漏写一个号不该废掉这条签）。"""
    value = row.get("no")
    if isinstance(value, bool):
        return lineno
    if isinstance(value, int):
        return value if value > 0 else lineno
    if isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
        return number if number > 0 else lineno
    return lineno


def _line_id(row: dict, lineno: int) -> str:
    """签 id：行里给了就用，没给用 `line-<行号>`（同一文件内唯一即可）。"""
    value = row.get("id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return f"line-{lineno}"


def load_fortune_bank(path: Path) -> tuple[FortuneEntry, ...] | None:
    """读一个签文库，返回 `(FortuneEntry, ...)`；读不动返回 None。

    坏行（非 JSON、缺签等、缺签文）逐行跳过，不抛异常——题库是运维手改的，
    一行手滑不该让整个玩法崩掉（§2.5：异常不上抛）。`id`/`no`/`jie` 缺了各自兜底：
    行号当签号、`line-<行号>` 当 id、空释义留给「解签」提示。
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    rows: list[FortuneEntry] = []
    for lineno, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        tier = _line_tier(row)
        text = _line_text(row)
        if tier is None or text is None:
            continue
        rows.append(
            FortuneEntry(
                id=_line_id(row, lineno),
                no=_line_no(row, lineno),
                tier=tier,
                text=text,
                jie=_line_jie(row),
            )
        )
    return tuple(rows)


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 黄历 / 星座 / 求财神（第 3 批，`docs/design/playhub.md:1178-1184`）
#
# 三条都是**纯本地、日确定性**：同一群同一天同一人结果不变，跨天自动换。
# 黄历/星座/财神签文都放在 `content/playhub/<bank>.jsonl`，运维可自行替换；
# 文件缺失时回「未就绪」，不现编内容（§5.2 只允许「不回答」，不允许瞎编）。
# ---------------------------------------------------------------------------
ALMANAC_BANK = "almanac.jsonl"
HOROSCOPE_BANK = "horoscope.jsonl"
WEALTH_BANK = "wealth.jsonl"

#: 求财神固定 +20 金币（§4.4 明写「金币产出全部为固定值，无随机金币」）。
WEALTH_COIN = 20
WEALTH_KEY = "wealth"
WEALTH_CAP = 1
WEALTH_REASON = "wealth"
WEALTH_ALREADY = "今天已经求过财神了，明天再来吧"
WEALTH_NOT_READY = "财神签文库未就绪"

WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

#: 12 星座（名字里带「座」也能认）
SIGNS = (
    "白羊", "金牛", "双子", "巨蟹", "狮子", "处女",
    "天秤", "天蝎", "射手", "摩羯", "水瓶", "双鱼",
)

LUCKY_COLORS = (
    "墨绿", "藏青", "暖橙", "米白", "酒红", "雾灰",
    "浅金", "湖蓝", "薰衣草紫", "青竹绿", "砂棕", "奶咖",
)

#: `(mtime, size, rows)`，避免每条消息都重读小文件；文件改了立刻失效。
_BANK_CACHE: dict[Path, tuple[float, int, tuple[dict, ...]]] = {}


def content_paths(store, bank: str, *, repo_root: Path | None = None) -> tuple[Path, Path, ...]:
    """与 `fortune_bank_paths` 同一套覆盖规则：账号工作区优先，仓库层兜底。"""
    local = Path(store.db_path).parent / "content" / bank
    root = Path(repo_root) if repo_root is not None else _REPO_ROOT
    return (local, root / "content" / "playhub" / bank)


def load_jsonl_bank(path: Path) -> tuple[dict, ...] | None:
    """读一个 `{...}` 每行的 JSONL，按 `(mtime, size)` 缓存；读不动返回 None。"""
    try:
        stat = path.stat()
    except OSError:
        return None
    cached = _BANK_CACHE.get(path)
    if cached is not None and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    rows: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue          # 手改坏行只跳过，不整类拒载
        if isinstance(row, dict):
            rows.append(row)
    result = tuple(rows)
    _BANK_CACHE[path] = (stat.st_mtime, stat.st_size, result)
    return result


def bank_for(store, bank: str, *, repo_root: Path | None = None) -> tuple[dict, ...] | None:
    for path in content_paths(store, bank, repo_root=repo_root):
        rows = load_jsonl_bank(path)
        if rows:
            return rows
    return None


def _hash_int(*parts: str) -> int:
    raw = "|".join(parts).encode("utf-8")
    return int(hashlib.sha256(raw).hexdigest()[:8], 16)


def _stars(value: int, low: int = 2, high: int = 5) -> str:
    n = low + value % (high - low + 1)
    return "★" * n + "☆" * (5 - n)


def _sign_of(text: str) -> str | None:
    """从参数里认星座名，`狮子`/`狮子座`/`我是狮子座` 都算。"""
    for sign in SIGNS:
        if sign in text:
            return sign
    return None


class DailyEngine:
    """`daily` 引擎：签到 + 抽签。

    随机源必须可注入（§2.5 的确定性要求）：默认 `random.Random()` 实例，
    **不碰模块级 `random` 函数**，测试传 `random.Random(种子)` 或假 rng 即可复现。
    """

    name = "daily"

    def __init__(self, *, rng=None, now_fn=None, repo_root=None, logger=None):
        self._rng = rng if rng is not None else random.Random()
        self._now = now_fn if now_fn is not None else clock.now_ts
        self._repo_root = repo_root
        self._log = logger if logger is not None else logging.getLogger("playhub.engines.daily")

    # -- 分发 ---------------------------------------------------------------
    def on_command(self, cmd, msg=None, ctx=None, game=None) -> Reply | None:
        action = getattr(cmd, "action", "")
        if action == "daily.signin":
            return self.signin(msg, ctx, game)
        if action == "daily.fortune":
            return self.fortune(msg, ctx, game)
        if action == "daily.fortune_explain":
            return self.fortune_explain(msg, ctx, game)
        if action == "daily.almanac":
            return self.almanac(cmd, msg, ctx, game)
        if action == "daily.horoscope":
            return self.horoscope(cmd, msg, ctx, game)
        if action == "daily.wealth":
            return self.wealth(msg, ctx, game)
        return None

    # -- 签到 ---------------------------------------------------------------
    def signin(self, msg, ctx=None, game=None) -> Reply | None:
        room, uid = msg_room(msg), msg_uid(msg)
        if not room or not uid:          # 拿不到群号/身份就不动账
            return None
        store = store_of(game)
        now = self._now()
        day = clock.day_of(now)          # 日界统一走 core/clock（Asia/Shanghai）
        nickname = msg_nickname(msg, ctx)
        operation_key = f"signin:{day}:{uid}"

        already = False
        streak = coin = diamond = coin_after = 0
        chests: tuple[str, ...] = ()
        try:
            with store.tx() as conn:      # BEGIN IMMEDIATE → 校验 → 发币 → 流水 → COMMIT
                if _counter_used(conn, room=room, uid=uid, day=day, key=SIGNIN_KEY, cap=SIGNIN_CAP) >= SIGNIN_CAP:
                    already = True
                else:
                    ensure_player(conn, room=room, uid=uid, nickname=nickname or None, now=now)
                    last_day, last_streak = _read_streak(conn, room, uid)
                    streak = next_streak(last_day, last_streak, day)
                    coin = signin_coin(streak)
                    diamond = signin_diamond(streak)

                    _counter_consume(conn, room=room, uid=uid, day=day, key=SIGNIN_KEY, cap=SIGNIN_CAP, now=now)
                    entry = credit(
                        conn,
                        room=room,
                        uid=uid,
                        currency="coin",
                        amount=coin,
                        reason=SIGNIN_REASON,
                        operation_key=operation_key,
                        posting_no=0,
                        now=now,
                    )
                    coin_after = entry.balance
                    if diamond:
                        # §4.4「钻石总账」：连签第 7 天 +3 钻；金箱也在这天发，但箱子
                        # 只是「抽奖容器」，开启后的金币/钻石记在 `open_chest` 里。
                        credit(
                            conn,
                            room=room,
                            uid=uid,
                            currency="diamond",
                            amount=diamond,
                            reason=SIGNIN_DIAMOND_REASON,
                            operation_key=operation_key,
                            posting_no=1,
                            now=now,
                        )
                    # §4.4「宝箱三型」取得方式：铜箱每日签到赠 1；金箱连签第 7 天赠 1。
                    # 银箱来自活跃榜周榜前 10（`ranking` 的定时动作属第 4 批），这里不发。
                    chests = [items.CHEST_BRONZE] + ([items.CHEST_GOLD] if diamond else [])
                    items.seed(conn)
                    for chest_id in chests:
                        items.add(conn, room, uid, chest_id)
                    _write_streak(conn, room, uid, day, streak, now)
        except DuplicatePosting:
            # 同一 operation_key 已有流水：并发/重放，按「今天签过了」处理，不重复发币。
            self._log.warning("签到重复流水被拦截：room=%s uid=%s key=%s", room, uid, operation_key)
            return Reply(SIGNIN_ALREADY, "daily.signin")

        if already:                      # Reply 一律在 COMMIT 之后构造，事务内不发送
            return Reply(SIGNIN_ALREADY, "daily.signin")
        return Reply(_signin_text(nickname, streak, coin, diamond, coin_after, chests), "daily.signin")

    # -- 抽签 ---------------------------------------------------------------
    def draw_tier(self) -> FortuneTier:
        """按 §4.4 的 12 份权重抽一档；用注入的 rng，不用全局 random。"""
        roll = self._rng.randrange(FORTUNE_TOTAL_WEIGHT)
        for tier in FORTUNE_TIERS:
            if roll < tier.weight:
                return tier
            roll -= tier.weight
        return FORTUNE_TIERS[-1]          # 理论上到不了；权重和已校验为 12

    def fortune(self, msg, ctx=None, game=None) -> Reply | None:
        room, uid = msg_room(msg), msg_uid(msg)
        if not room or not uid:
            return None
        store = store_of(game)
        now = self._now()
        day = clock.day_of(now)
        nickname = msg_nickname(msg, ctx)
        operation_key = f"fortune:{day}:{uid}"

        # 签文库先判：缺失/本档为空 → 回「签文库未就绪」，不崩、不发币、不消耗次数。
        bank = self._bank(store)
        if bank is None:
            return Reply(FORTUNE_NOT_READY, "daily.fortune")
        tier = self.draw_tier()
        entry = _pick_entry(bank, tier.name, self._rng)
        if entry is None:
            return Reply(FORTUNE_NOT_READY, "daily.fortune")

        already = False
        coin_after = 0
        try:
            with store.tx() as conn:
                if _counter_used(conn, room=room, uid=uid, day=day, key=FORTUNE_KEY, cap=FORTUNE_CAP) >= FORTUNE_CAP:
                    already = True
                else:
                    ensure_player(conn, room=room, uid=uid, nickname=nickname or None, now=now)
                    _counter_consume(conn, room=room, uid=uid, day=day, key=FORTUNE_KEY, cap=FORTUNE_CAP, now=now)
                    # 当天抽到哪一签落在 kv（按群 + 人，每天覆盖），解签只认今天这条。
                    kv.set_json(
                        conn, room, _pick_key(uid), {"day": day, "id": entry.id}, now
                    )
                    coin_entry = credit(
                        conn,
                        room=room,
                        uid=uid,
                        currency="coin",
                        amount=tier.coin,
                        reason=FORTUNE_REASON,
                        operation_key=operation_key,
                        posting_no=0,
                        now=now,
                    )
                    coin_after = coin_entry.balance
                    if tier.diamond:
                        credit(
                            conn,
                            room=room,
                            uid=uid,
                            currency="diamond",
                            amount=tier.diamond,
                            reason=FORTUNE_DIAMOND_REASON,
                            operation_key=operation_key,
                            posting_no=1,
                            now=now,
                        )
        except DuplicatePosting:
            self._log.warning("抽签重复流水被拦截：room=%s uid=%s key=%s", room, uid, operation_key)
            return Reply(FORTUNE_ALREADY, "daily.fortune")

        if already:
            return Reply(FORTUNE_ALREADY, "daily.fortune")
        return Reply(_fortune_text(nickname, tier, entry, coin_after), "daily.fortune")

    # -- 解签 ---------------------------------------------------------------
    def fortune_explain(self, msg, ctx=None, game=None) -> Reply | None:
        """回今天的签诗释义；不发币、不限次数、不消耗抽签次数。

        「今天」按抽签记录的 `day` 判断：跨天就只提示先抽签，昨天的签不再解。
        """
        room, uid = msg_room(msg), msg_uid(msg)
        if not room or not uid:
            return None
        store = store_of(game)
        now = self._now()
        day = clock.day_of(now)
        nickname = msg_nickname(msg, ctx)

        with store.tx(immediate=False) as conn:
            record = kv.get_json(conn, room, _pick_key(uid))
        if not isinstance(record, dict) or record.get("day") != day:
            return Reply(FORTUNE_EXPLAIN_MISSING, "daily.fortune_explain")

        bank = self._bank(store)
        if bank is None:
            return Reply(FORTUNE_NOT_READY, "daily.fortune_explain")
        wanted = record.get("id")
        entry = next((row for row in bank if row.id == wanted), None)
        jie = (entry.jie if entry is not None else "") or FORTUNE_NO_JIE
        rows = (FORTUNE_DASH, f"🔮解签：{jie}")
        return Reply(
            render.result(nickname, "", rows), "daily.fortune_explain"
        )

    # -- 内部 ---------------------------------------------------------------
    # -- 黄历 ---------------------------------------------------------------
    def almanac(self, cmd, msg=None, ctx=None, game=None) -> Reply | None:
        room, uid = msg_room(msg), msg_uid(msg)
        store = store_of(game)
        if store is None:
            return None
        now = self._now()
        args = getattr(cmd, "args", "") or ""
        offset = 1 if "明天" in args else (-1 if "昨天" in args else 0)
        try:
            day = date.fromisoformat(clock.day_of(now)) + timedelta(days=offset)
        except ValueError:
            return None
        rows = bank_for(store, ALMANAC_BANK, repo_root=self._repo_root)
        if not rows:
            return Reply("📅 黄历还没准备好：缺 content/playhub/almanac.jsonl", "daily.almanac")
        row = rows[_hash_int(str(day)) % len(rows)]

        def _join(key: str) -> str:
            value = row.get(key)
            if isinstance(value, list):
                return "、".join(str(x) for x in value) or "—"
            return str(value) if value else "—"

        when = f"{day:%Y-%m-%d} {WEEKDAYS[day.weekday()]}"
        if offset:
            when += " · 明天" if offset > 0 else " · 昨天"
        body = [
            render.field("🗓", "干支", f"{row.get('gz') or '—'}日"),
            render.field("🌿", "五行", _join("wx")),
            render.field("🐎", "冲煞", _join("chong")),
            render.field("✨", "吉神", _join("god")),
            render.field("✅", "宜", _join("yi")),
            render.field("⛔", "忌", _join("ji")),
        ]
        return Reply(
            render.menu(
                "📅", f"黄历 {when}", body,
                note_text="内容出自本地文案库（content/playhub/almanac.jsonl），按日轮转，不接真实历法",
            ),
            "daily.almanac",
        )

    # -- 星座 ---------------------------------------------------------------
    def horoscope(self, cmd, msg=None, ctx=None, game=None) -> Reply | None:
        room, uid = msg_room(msg), msg_uid(msg)
        store = store_of(game)
        if store is None:
            return None
        day = clock.day_of(self._now())
        args = getattr(cmd, "args", "") or ""
        sign = _sign_of(args)
        derived = sign is None
        if derived:
            # 不给星座就按「这天 + 这群 + 你」定一个：同一天反复发结果一致，
            # 免得同一个人一会儿白羊一会儿天蝎。
            sign = SIGNS[_hash_int(day, room or "", uid or "") % len(SIGNS)]
        seed = f"{day}|{sign}"
        rows = [
            render.field("💗", "爱情", _stars(_hash_int(seed, "love"))),
            render.field("💼", "工作", _stars(_hash_int(seed, "work"))),
            render.field("💰", "财运", _stars(_hash_int(seed, "money"))),
            render.field("🍀", "健康", _stars(_hash_int(seed, "health"))),
            render.field("🔢", "幸运数字", str(_hash_int(seed, "num") % 9 + 1)),
            render.field("🎨", "幸运颜色", LUCKY_COLORS[_hash_int(seed, "color") % len(LUCKY_COLORS)]),
        ]
        bank = bank_for(store, HOROSCOPE_BANK, repo_root=self._repo_root)
        if bank:
            text = str(bank[_hash_int(seed, "text") % len(bank)].get("text") or "").strip()
            if text:
                rows.append(render.quote("📜", text))
        note_text = (
            "没给星座就按你名字算了一个；想指定发「星座 狮子」"
            if derived
            else "同一星座同一天结果不变，跨天自动换"
        )
        return Reply(render.menu("🔮", f"今日运势 · {sign}座", rows, note_text=note_text), "daily.horoscope")

    # -- 求财神 -------------------------------------------------------------
    def wealth(self, msg, ctx=None, game=None) -> Reply | None:
        room, uid = msg_room(msg), msg_uid(msg)
        if not room or not uid:
            return None
        store = store_of(game)
        now = self._now()
        day = clock.day_of(now)
        nickname = msg_nickname(msg, ctx)
        already = False
        coin_after = 0
        row: dict = {}
        try:
            with store.tx() as conn:
                if _counter_used(conn, room=room, uid=uid, day=day, key=WEALTH_KEY, cap=WEALTH_CAP) >= WEALTH_CAP:
                    already = True
                else:
                    bank = bank_for(store, WEALTH_BANK, repo_root=self._repo_root)
                    if not bank:
                        return Reply(f"🧧 {WEALTH_NOT_READY}", "daily.wealth")
                    row = bank[_hash_int(day, uid) % len(bank)]
                    ensure_player(conn, room=room, uid=uid, nickname=nickname or None, now=now)
                    _counter_consume(conn, room=room, uid=uid, day=day, key=WEALTH_KEY, cap=WEALTH_CAP, now=now)
                    credit(
                        conn, room=room, uid=uid, currency="coin", amount=WEALTH_COIN,
                        reason=WEALTH_REASON, operation_key=f"wealth:{day}:{uid}",
                        posting_no=0, now=now,
                    )
                    coin_after = balance(conn, room=room, uid=uid, currency="coin")
        except DuplicatePosting:
            return Reply("这个玩法暂时出错了", "daily.wealth")

        if already:
            return Reply(
                render.result(nickname, f"🧧 {WEALTH_ALREADY}", (), tail="⏳ 每天 0 点刷新"),
                "daily.wealth",
            )
        body = [
            render.field("💰", "金币", f"+{WEALTH_COIN}"),
            render.current("💰", "金币", coin_after),
        ]
        text = str(row.get("text") or "").strip()
        if text:
            body.append(render.quote("📜", text))
        return Reply(
            render.result(nickname, "🧧 财神到", body, tail="💡 一天只问一次，问的是今天这口气"),
            "daily.wealth",
        )

    def _bank(self, store) -> tuple[FortuneEntry, ...] | None:
        """按 §5.2 取签文库：① 存在即**完全覆盖** ②（不做跨层合并）。"""
        for path in fortune_bank_paths(store, repo_root=self._repo_root):
            if not path.is_file():
                continue
            rows = load_fortune_bank(path)
            return rows if rows is not None else None
        return None


def _pick_entry(bank: tuple[FortuneEntry, ...], tier: str, rng) -> FortuneEntry | None:
    """在同档的签里随机挑一条（rng 由调用方注入，§2.5 确定性要求）。

    该档只有一条时不碰 rng——少一次调用，测试里给「只认 12 份」的假 rng 也不会被绊到。
    """
    candidates = [entry for entry in bank if entry.tier == tier]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    return candidates[rng.randrange(len(candidates))]


def _signin_text(
    nickname: str,
    streak: int,
    coin: int,
    diamond: int,
    coin_after: int,
    chests: tuple[str, ...] = (),
) -> str:
    """签到结果卡（小V 格式：点人 → 明细 → 当前余额 → 一句彩蛋，见 `core/render.py`）。"""
    rows = [
        render.row("├ ", f"连签：{streak} 天"),
        render.row("├ ", f"金币：+{coin}"),
    ]
    if diamond:
        rows.append(render.row("├ ", f"钻石：+{diamond}"))
    for chest_id in chests:
        chest = items.chest_of(chest_id)
        if chest is not None:
            rows.append(render.row("├ ", f"{chest.name}：+1"))
    rows.append(render.current("💰", "金币", coin_after))
    tail = "🎋 明天接着来，断一天连签就得重新数"
    if chests:
        tail = "📦 箱子在背包里，发「开宝箱 铜」就能开\n" + tail
    return render.result(nickname, "🎁 签到成功", rows, tail=tail)


def _fortune_text(
    nickname: str, tier: FortuneTier, entry: FortuneEntry, coin_after: int
) -> str:
    """抽签结果卡（小V 版式，`docs/design/xiaov-cmd/images.md`「## 抽签」）。

    `@昵称` → 签号/签等 → 虚线 → 签诗 + 奖励 + 余额 → 双线 → 解签提示。
    签文原样保留，不改写、不截断。
    """
    rows = [
        FORTUNE_DASH,
        f"签诗：{entry.text}",
        render.row("├ ", f"金币：+{tier.coin}"),
    ]
    if tier.diamond:
        rows.append(render.row("├ ", f"钻石：+{tier.diamond}"))
    rows.append(render.current("💰", "金币", coin_after))
    tail = f"{render.rule(10)}\n💡欲知详情可发送【解签】"
    return render.result(
        nickname, f"🙏您抽到了第{entry.no}签 · {tier.name}！", rows, tail=tail
    )
