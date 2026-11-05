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

import json
import logging
import random
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from ..core import clock, credit, ensure_player, render
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

if FORTUNE_TOTAL_WEIGHT != 12:      # 加载期硬校验，不许靠「差不多」
    raise ValueError(f"§4.4 抽签总权重必须是 12 份，实际 {FORTUNE_TOTAL_WEIGHT}")

FORTUNE_NOT_READY = "签文库未就绪"
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


def load_fortune_bank(path: Path) -> tuple[tuple[str, str], ...] | None:
    """读一个签文库，返回 ((签等, 签文), ...)；读不动返回 None。

    坏行（非 JSON、缺签等、缺签文）逐行跳过，不抛异常——题库是运维手改的，
    一行手滑不该让整个玩法崩掉（§2.5：异常不上抛）。
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    rows: list[tuple[str, str]] = []
    for line in raw.splitlines():
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
        rows.append((tier, text))
    return tuple(rows)


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------
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
                        # 跨批边界：金箱（core/items.py）属第 3 批，本批只发钻石；
                        # 已签过的日子按 §7「不追溯补发」，第 3 批不会回头再补金箱。
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
                    _write_streak(conn, room, uid, day, streak, now)
        except DuplicatePosting:
            # 同一 operation_key 已有流水：并发/重放，按「今天签过了」处理，不重复发币。
            self._log.warning("签到重复流水被拦截：room=%s uid=%s key=%s", room, uid, operation_key)
            return Reply(SIGNIN_ALREADY, "daily.signin")

        if already:                      # Reply 一律在 COMMIT 之后构造，事务内不发送
            return Reply(SIGNIN_ALREADY, "daily.signin")
        return Reply(_signin_text(nickname, streak, coin, diamond, coin_after), "daily.signin")

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
        text = _pick_text(bank, tier.name)
        if text is None:
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
                    entry = credit(
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
                    coin_after = entry.balance
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
        return Reply(_fortune_text(nickname, tier, text, coin_after), "daily.fortune")

    # -- 内部 ---------------------------------------------------------------
    def _bank(self, store) -> tuple[tuple[str, str], ...] | None:
        """按 §5.2 取签文库：① 存在即**完全覆盖** ②（不做跨层合并）。"""
        for path in fortune_bank_paths(store, repo_root=self._repo_root):
            if not path.is_file():
                continue
            rows = load_fortune_bank(path)
            return rows if rows is not None else None
        return None


def _pick_text(bank: tuple[tuple[str, str], ...], tier: str) -> str | None:
    """取该档第一条签文。

    §5.3 没有规定「同档多签」怎么选，本批按文件顺序取第一条（可预期、可测）；
    要随机挑选属于题库加载层的事，等 `content/loader.py` 落地再改。
    """
    for name, text in bank:
        if name == tier:
            return text
    return None


def _signin_text(nickname: str, streak: int, coin: int, diamond: int, coin_after: int) -> str:
    """签到结果卡（小V 格式：点人 → 明细 → 当前余额 → 一句彩蛋，见 `core/render.py`）。"""
    rows = [
        render.row("├ ", f"连签：{streak} 天"),
        render.row("├ ", f"金币：+{coin}"),
    ]
    if diamond:
        rows.append(render.row("├ ", f"钻石：+{diamond}"))
    rows.append(render.current("💰", "金币", coin_after))
    return render.result(nickname, "🎁 签到成功", rows, tail="🎋 明天接着来，断一天连签就得重新数")


def _fortune_text(nickname: str, tier: FortuneTier, text: str, coin_after: int) -> str:
    """抽签结果卡：签档 + 奖励 + 当前余额 + 签文（签文原样保留，不改写、不截断）。"""
    rows = [render.row("├ ", f"金币：+{tier.coin}")]
    if tier.diamond:
        rows.append(render.row("├ ", f"钻石：+{tier.diamond}"))
    rows.append(render.current("💰", "金币", coin_after))
    rows.append(render.quote("📜", text))
    return render.result(nickname, f"🎋 抽到「{tier.name}」", rows)
