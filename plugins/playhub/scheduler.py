"""playhub 后台定时动作（`§4.2`）：改名巡检、离婚超时、收题清理、发工资、周榜银箱。

为什么单开一条线程、单开一条连接（`§4.2.2`）：

- 前台每条消息一个事务，后台一次 tick 可能扫十几个群。共用一条连接会让「等锁」
  出现在用户能感知的路径上；所以后台在**自己的线程里**建 `Store`、在自己的线程里
  `close()`，与 `App` 的连接互不共享。
- **事务里绝不调微信接口**。改名播报要读群成员快照、还要发消息，顺序固定为
  「事务外读快照 → 事务内比对落库 → 事务外发消息」；事务里做 IO 会把写锁持到
  网络超时，前台整群卡住。

幂等靠三样东西（`§4.2.5`）：`sched.last.*`（每天/每周一次）、钱包 `operation_key`
（工资两条分录）、`idem` 表（银箱按周一次性）。任何一样丢了，另外两样仍能把
「重复执行」挡在「余额不变」这一层。

测试要点见 `§4.2.8`：`now_fn`/`rng`/`sleep_event` 都可注入，`tick()` 可以同步直调。
"""
from __future__ import annotations

import logging
import random
import sqlite3
import threading
from dataclasses import dataclass, fields
from datetime import date, datetime, timedelta
from pathlib import Path

from .core import activity, clock, items, kv, render, switches
from .core.errors import DuplicatePosting, Insufficient
from .core.store import Store
from .core.wallet import credit, debit, ensure_player
from .engines import moderation, ranking
from .engines.groupstat import (
    RENAME_SNAPSHOT_KEY,
    diff_names,
    member_rows,
    snapshot_of,
    watch_on,
)
from .engines.quiz import QuizEngine
# 关系/审计的读写只在 `engines/social.py` 里实现过一次，后台复用同一份，
# 免得两张地方各写一套 `pair` 解析，迟早分叉。
from .engines.social import KIND_EMPLOY, WAGE_PER_DAY, SocialEngine, _audit, _meta, _partner_of

_LOG = logging.getLogger("playhub.scheduler")

#: 线程名：`§4.2.7` 要求日志里能一眼认出是后台线程
THREAD_NAME = "playhub.scheduler"

#: 首次 tick 的延迟与抖动（`§4.2.6`）：错开进程启动时的一堆初始化
FIRST_DELAY_SECONDS = 300
FIRST_DELAY_JITTER = 60
#: 之后每 10 分钟一次
TICK_SECONDS = 600
#: `stop()` 等线程收尾的上限（`§4.2.1`）
JOIN_TIMEOUT_SECONDS = 10

#: 改名只在 10:00–22:00 播报（`§4.2.4`）；窗口外连巡检都不做
RENAME_WINDOW = (10, 22)
RENAME_AFTER = (RENAME_WINDOW[0], 0)
RENAME_LIST_LIMIT = 10

#: 工资在 00:05 之后结算，最多补 3 天（成交当天不发）
SALARY_AFTER = (0, 5)
SALARY_CATCHUP_DAYS = 3

#: 银箱在周一 00:10 之后发上周榜
CHEST_AFTER = (0, 10)
CHEST_TOP_N = 10
CHEST_SCAN_LIMIT = 20

#: 过期清理在 04:00 之后跑一次
GC_AFTER = (4, 0)
IDEM_RETENTION_DAYS = 30
AUDIT_RETENTION_DAYS = 180
PURGE_BATCH = 5000

#: 全局（不分群）的 kv 作用域
GLOBAL_ROOM = "*"
#: 改名巡检只看最近 7 天活跃过的群
ACTIVE_ROOM_DAYS = 7

#: 三个「已经做过」的标记键（`§4.2.5`）
SCHED_LAST_RENAME = "sched.last.rename"
SCHED_LAST_SALARY = "sched.last.salary"
SCHED_LAST_CHEST = "sched.last.chest"
SCHED_LAST_PURGE = "sched.last.purge"

#: 群集合的发现 SQL（`§4.2.3`）。都不带参数以外的东西，换库也不用改。
RENAME_ROOMS_SQL = "SELECT DISTINCT room FROM activity_daily WHERE day >= ?"
EXPIRE_ROOMS_SQL = (
    "SELECT DISTINCT room FROM kv WHERE key LIKE 'divorce:%' "
    "UNION SELECT DISTINCT room FROM pair WHERE state = 'pending'"
)
SALARY_ROOMS_SQL = "SELECT DISTINCT room FROM pair WHERE kind = ? AND state = 'active'"
CHEST_ROOMS_SQL = "SELECT DISTINCT room FROM activity_daily WHERE day BETWEEN ? AND ?"

#: `_guard()` 的两种「没做成」：拿不到写锁与真异常。前者不计 errors、不写 last_run。
_LOCKED = object()
_FAILED = object()


@dataclass(frozen=True)
class TickReport:
    """一次 tick 的计数（`§4.2.1`）；全零就不打日志。"""

    renamed: int = 0
    expired: int = 0
    quiz_closed: int = 0
    idem_purged: int = 0
    audit_purged: int = 0
    salary_paid: int = 0
    salary_skipped: int = 0
    chests: int = 0
    errors: int = 0

    def any(self) -> bool:
        """有没有任何一件事发生（决定要不要记一条 info）。"""
        return any(getattr(self, item.name) for item in fields(self))

    def describe(self) -> str:
        """一行摘要，日志用。"""
        return (
            f"改名 {self.renamed} / 超时 {self.expired} / 收题 {self.quiz_closed} / "
            f"工资 {self.salary_paid}（跳过 {self.salary_skipped}）/ 银箱 {self.chests} / "
            f"清理 idem {self.idem_purged} audit {self.audit_purged} / 错误 {self.errors}"
        )


# ---------------------------------------------------------------------------
# 时间判断
# ---------------------------------------------------------------------------
def _local(now: int) -> datetime:
    """unix 秒 → 北京时间（`core/clock.py` 的时区口径）。

    日切/周切一律走 `clock`，这里只为了拿「几点几分」「周几」。
    """
    return datetime.fromtimestamp(int(now), clock.CN)


def is_due_daily(last, today: str, local_hm: tuple[int, int], after: tuple[int, int]) -> bool:
    """每天一次的动作是否到期：今天还没做过，且已过 `after` 这个时分。"""
    if last == today:
        return False
    return tuple(local_hm) >= tuple(after)


def is_due_weekly(
    last, this_week: str, local_wd_hm: tuple[int, int, int], after: tuple[int, int]
) -> bool:
    """每周一次的动作是否到期。

    `local_wd_hm` 是 `(周几, 时, 分)`，周几按 ISO（周一=1）。非周一也算到期：
    停机几天后再启动要补上周那一次，否则要等到下周一。
    """
    if last == this_week:
        return False
    weekday, hour, minute = local_wd_hm
    if weekday != 1:
        return True
    return (hour, minute) >= tuple(after)


# ---------------------------------------------------------------------------
# 审计与账号
# ---------------------------------------------------------------------------
def _audit_tx(store, *, now: int, room: str | None, action: str, result: str = "ok",
              detail: dict | None = None, target: str | None = None) -> None:
    """单独开一个事务写一行 audit（失败路径用：业务事务已经回滚了）。"""
    with store.tx() as conn:
        _audit(conn, now=now, room=room, actor="scheduler", action=action,
               target=target, result=result, detail=detail)


def _account_of(ctx) -> str:
    """`ctx.members.account`；拿不到（空/非字符串/能力异常）返回空串。"""
    members = getattr(ctx, "members", None)
    try:
        account = getattr(members, "account", "") or ""
    except Exception:                       # CapabilityError 等：失败关闭
        return ""
    return account if isinstance(account, str) else ""


def _chat_allowed(ctx, room: str) -> bool:
    """这个群允不允许主动播报（`§4.2.4`）。

    框架没提供 `chat_allowed`（老版本 ctx / 测试的假 ctx）时**不发**：主动播报
    比漏播更危险，宁可只落库。
    """
    getter = getattr(ctx, "chat_allowed", None)
    if not callable(getter):
        return False
    try:
        return bool(getter(room))
    except Exception:
        return False


def _rooms(store, sql: str, params=()) -> list[str]:
    """按 SQL 取群集合；只读事务，去空串。"""
    with store.tx(immediate=False) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [str(row[0]) for row in rows if row[0]]


# ---------------------------------------------------------------------------
# 动作一：改名巡检（唯一会主动发群的定时动作）
# ---------------------------------------------------------------------------
def _rename_text(changes: list[tuple[str, str, str]]) -> str:
    """播报正文：前 10 条 `旧 → 新`，剩下的折成一行（`render.limit` 兜长度）。"""
    rows = [render.row("✏️ ", f"{old} → {new}") for _uid, old, new in changes[:RENAME_LIST_LIMIT]]
    rest = len(changes) - len(rows)
    if rest > 0:
        rows.append(render.row("… ", f"还有 {rest} 处没列出来"))
    return render.limit(render.result("", "🔍 改名播报", rows, at_sender=False))


def run_rename(store, ctx, room: str, now: int, *, send, logger=None) -> int:
    """一次改名巡检；返回发现的变化数（播报成功与否都算「发现」）。"""
    log = logger if logger is not None else _LOG
    local = _local(now)
    if local.hour >= RENAME_WINDOW[1]:
        return 0                            # 22:00 之后不巡检，把变化留给明天 10:00
    today = clock.day_of(now)

    with store.tx(immediate=False) as conn:
        if not watch_on(conn, room):        # `改名 关`：整群跳过
            return 0
        last = kv.get(conn, room, SCHED_LAST_RENAME)
    if not is_due_daily(last, today, (local.hour, local.minute), RENAME_AFTER):
        return 0

    # 事务外读快照：`ctx.group_members()` 是框架缓存，慢/失败都不该占着写锁。
    rows = member_rows(ctx, room)
    if rows is None:
        return 0                            # 缓存没就绪：不写 last_run，下一次再试
    account = _account_of(ctx)
    rows = [(uid, name) for uid, name in rows if uid != account]
    allowed = _chat_allowed(ctx, room)

    changes: list[tuple[str, str, str]] = []
    with store.tx() as conn:
        prev = kv.get_json(conn, room, RENAME_SNAPSHOT_KEY) or {}
        changes = diff_names(prev, rows)
        # 首次巡检（没有基线）diff 必为空：只建基线、不播报（`§4.2.4`）
        for uid, old, new in changes:
            activity.record_nickname_change(conn, room=room, uid=uid, old=old, new=new, now=now)
        kv.set_json(conn, room, RENAME_SNAPSHOT_KEY, snapshot_of(rows), now)
        kv.put(conn, room, SCHED_LAST_RENAME, today, now)
        if allowed:
            _audit(conn, now=now, room=room, actor="scheduler", action="sched:rename",
                   result="ok", detail={"changes": len(changes)})
        else:
            _audit(conn, now=now, room=room, actor="scheduler", action="sched:rename",
                   result="skipped", detail={"why": "not_allowed", "changes": len(changes)})

    if changes and allowed:
        try:
            send(room, _rename_text(changes))
        except Exception as error:
            # 发送失败不重试：快照已更新，重试只会把同一处改名报两遍（`§4.2.4`）
            log.warning("playhub 改名播报发送失败：room=%s error=%s", room, type(error).__name__)
            _audit_tx(store, now=now, room=room, action="sched:rename",
                      result="failed", detail={"error": type(error).__name__})
    return len(changes)


# ---------------------------------------------------------------------------
# 动作二：离婚超时退款
# ---------------------------------------------------------------------------
def run_expire(store, social, room: str, now: int) -> int:
    """收掉这个群到期的待确认/离婚单；返回收掉的条数。

    与前台惰性收尾同一条路径、同一个 `operation_key`，谁先到谁生效（`§4.2.9`）。
    """
    with store.tx() as conn:
        return social.expire_room(conn, room, now)


# ---------------------------------------------------------------------------
# 动作三：收题清理 + 过期 idem/audit
# ---------------------------------------------------------------------------
def run_quiz_gc(store, quiz, now: int) -> int:
    """把超时对局标记结束；**不释放 game_slot**（槽位属于前台运行期，`§4.2.4`）。"""
    with store.tx() as conn:
        return quiz.sweep(conn, now, release_slots=False)


def _purge_idem(store, cutoff: int) -> int:
    """分批删过期 idem；每批一个事务，避免一次锁太久。

    `idem` 是 `WITHOUT ROWID`，没有 `rowid` 列，只能按主键 `key` 删。
    """
    total = 0
    while True:
        with store.tx() as conn:
            cursor = conn.execute(
                "DELETE FROM idem WHERE key IN (SELECT key FROM idem WHERE at < ? LIMIT ?)",
                (cutoff, PURGE_BATCH),
            )
            count = int(cursor.rowcount or 0)
        total += count
        if count < PURGE_BATCH:
            return total


def _purge_audit(store, cutoff: int) -> int:
    """分批删过期 audit（`audit` 有自增 id，按 id 删）。`wallet_ledger` 永不删。"""
    total = 0
    while True:
        with store.tx() as conn:
            cursor = conn.execute(
                "DELETE FROM audit WHERE id IN (SELECT id FROM audit WHERE at < ? LIMIT ?)",
                (cutoff, PURGE_BATCH),
            )
            count = int(cursor.rowcount or 0)
        total += count
        if count < PURGE_BATCH:
            return total


def run_purge(store, now: int) -> tuple[int, int]:
    """每天 04:00 后清一次过期 idem/audit；返回 `(idem, audit)` 条数。"""
    today = clock.day_of(now)
    local = _local(now)
    with store.tx(immediate=False) as conn:
        last = kv.get(conn, GLOBAL_ROOM, SCHED_LAST_PURGE)
    if not is_due_daily(last, today, (local.hour, local.minute), GC_AFTER):
        return 0, 0

    idem_count = _purge_idem(store, now - IDEM_RETENTION_DAYS * 86400)
    audit_count = _purge_audit(store, now - AUDIT_RETENTION_DAYS * 86400)
    with store.tx() as conn:
        kv.put(conn, GLOBAL_ROOM, SCHED_LAST_PURGE, today, now)
        _audit(conn, now=now, room=None, actor="scheduler", action="sched:gc",
               detail={"idem": idem_count, "audit": audit_count})
    return idem_count, audit_count


# ---------------------------------------------------------------------------
# 动作四：发工资
# ---------------------------------------------------------------------------
def _wage_of(row) -> int | None:
    """`meta.wage`，读不到用 `WAGE_PER_DAY`；解析失败/非正数返回 None。"""
    raw = _meta(row).get("wage", WAGE_PER_DAY)
    try:
        wage = int(raw)
    except (TypeError, ValueError):
        return None
    return wage if wage > 0 else None


def _salary_days(since_day: str, today: str) -> list[str]:
    """该补的日期：成交次日 起、最多往前 3 天、到今天为止（`§4.2.4`）。"""
    try:
        since = date.fromisoformat(since_day)
        end = date.fromisoformat(today)
    except (TypeError, ValueError):
        return []
    start = max(since + timedelta(days=1), end - timedelta(days=SALARY_CATCHUP_DAYS - 1))
    if start > end:
        return []
    return [(start + timedelta(days=offset)).isoformat() for offset in range((end - start).days + 1)]


def run_salary(store, room: str, day: str, now: int) -> tuple[int, int]:
    """给这个群的每份雇佣合同发工资；返回 `(发出天数, 因余额不足跳过天数)`。"""
    with store.tx(immediate=False) as conn:
        if not switches.is_on(conn, room, switches.EMPLOY):
            return 0, 0                     # 开关关：整群不动，也不写 last_run
        pairs = conn.execute(
            "SELECT * FROM pair WHERE room = ? AND kind = ? AND state = 'active'",
            (room, KIND_EMPLOY),
        ).fetchall()

    paid = skipped = 0
    for row in pairs:
        pair_id = int(row["id"])
        employer = str(row["by_uid"] or "")
        employee = _partner_of(row, employer) if employer else ""
        if not employer or not employee or employer == employee:
            _audit_tx(store, now=now, room=room, action="sched:salary", result="failed",
                      target=str(pair_id), detail={"why": "bad_by_uid", "pair_id": pair_id})
            continue
        wage = _wage_of(row)
        if wage is None:
            _audit_tx(store, now=now, room=room, action="sched:salary", result="failed",
                      target=str(pair_id), detail={"why": "bad_wage", "pair_id": pair_id})
            continue
        keys = f"sched:salary:{pair_id}"
        for due_day in _salary_days(clock.day_of(int(row["since"] or 0)), day):
            operation_key = f"{keys}:{due_day}"
            try:
                with store.tx() as conn:
                    ensure_player(conn, room=room, uid=employer, nickname="", now=now)
                    ensure_player(conn, room=room, uid=employee, nickname="", now=now)
                    # 两笔分录同一个 operation_key、posting_no 0/1：任一笔重复，整对回滚
                    debit(conn, room=room, uid=employer, currency="coin", amount=wage,
                          reason="salary_pay", operation_key=operation_key, posting_no=0,
                          now=now, ref=str(pair_id))
                    credit(conn, room=room, uid=employee, currency="coin", amount=wage,
                           reason="salary_income", operation_key=operation_key, posting_no=1,
                           now=now, ref=str(pair_id))
                    _audit(conn, now=now, room=room, actor="scheduler", action="sched:salary",
                           target=str(pair_id),
                           detail={"day": due_day, "wage": wage,
                                   "employer": employer, "employee": employee})
                paid += 1
            except DuplicatePosting:
                continue                    # 这笔已经发过：不算失败，也不算补发
            except Insufficient:
                skipped += 1
                _audit_tx(store, now=now, room=room, action="sched:salary", result="failed",
                          target=str(pair_id), detail={"why": "insufficient", "day": due_day})
    return paid, skipped


# ---------------------------------------------------------------------------
# 动作五：周榜银箱
# ---------------------------------------------------------------------------
def _last_week(now: int) -> tuple[str, str, str]:
    """上一周：`(周键, 周一日期, 周日日期)`（`§4.2.4` 的活跃榜口径）。"""
    weekday = _local(now).weekday()          # 周一=0
    last_sunday = int(now) - (weekday + 1) * 86400
    return (
        clock.week_of(last_sunday),
        clock.day_of(last_sunday - 6 * 86400),
        clock.day_of(last_sunday),
    )


def run_chest(store, room: str, week: str, now: int, *, account: str = "") -> int:
    """给上周活跃榜前 10 各发一个银箱；返回发出去的个数（不播报）。

    `week` 是**要发的那个周**的键（上一周）。`idem` 里先占位再发货，同一周
    即使 `sched.last.chest` 被删也不会重发（`§4.2.5`）。
    """
    local = _local(now)
    with store.tx(immediate=False) as conn:
        last = kv.get(conn, room, SCHED_LAST_CHEST)
    if not is_due_weekly(
        last,
        clock.week_of(now),
        (local.isoweekday(), local.hour, local.minute),
        CHEST_AFTER,
    ):
        return 0

    weekday = local.weekday()
    last_sunday = int(now) - (weekday + 1) * 86400
    key = f"sched:chest:{room}:{week}"
    try:
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO idem (key, scope, at) VALUES (?, 'sched.chest', ?)", (key, now)
            )
            rows = ranking.top(conn, board=ranking.ACTIVITY_BOARD, room=room,
                               period="week", now=last_sunday, limit=CHEST_SCAN_LIMIT)
            winners = [
                row.uid for row in rows
                if row.uid != account and not moderation.is_blocked(conn, room, row.uid)
            ][:CHEST_TOP_N]
            for uid in winners:
                ensure_player(conn, room=room, uid=uid, nickname="", now=now)
                items.add(conn, room, uid, items.CHEST_SILVER, 1)
            kv.put(conn, room, SCHED_LAST_CHEST, week, now)
            _audit(conn, now=now, room=room, actor="scheduler", action="sched:chest",
                   detail={"week": week, "uids": winners})
        return len(winners)
    except sqlite3.IntegrityError:
        # 本周已发过（idem 主键撞车）：把 last_run 补上，别再每 tick 重查一遍
        with store.tx() as conn:
            kv.put(conn, room, SCHED_LAST_CHEST, week, now)
        return 0


# ---------------------------------------------------------------------------
# 调度器（`§4.2.1`）
# ---------------------------------------------------------------------------
def _is_locked(error: sqlite3.Error) -> bool:
    """是不是「拿不到写锁」这类临时错误（只跳过本群本动作，不当故障计数）。"""
    text = str(error).lower()
    return "locked" in text or "busy" in text


class Scheduler:
    """后台定时动作的线程外壳（`§4.2.1`）。

    可注入项一律从构造参数进来（`now_fn`/`rng`/`sleep_event`/`store_factory`），
    所以测试可以只调 `tick()`，不起线程、不睡 5 分钟（`§4.2.8`）。
    """

    def __init__(
        self,
        *,
        ctx,
        root: Path,
        now_fn=clock.now_ts,
        rng: random.Random | None = None,
        logger=None,
        store_factory=Store,
        sleep_event: threading.Event | None = None,
    ) -> None:
        self._ctx = ctx
        self._root = Path(root)
        self._now = now_fn
        self._rng = rng if rng is not None else random.Random()
        self._log = logger if logger is not None else _LOG
        self._store_factory = store_factory
        # `_wake` 既是「首次延迟」也是「两次 tick 之间」的等待对象：
        # `stop()` 只要 set 它，线程立刻醒来退出，不用等满 10 分钟。
        self._wake = sleep_event if sleep_event is not None else threading.Event()
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        # tick 串行：后台线程与测试里的同步直调共用同一条连接，不能并发进事务。
        self._lock = threading.Lock()
        self._store: Store | None = None
        self._account = ""
        self._warned_no_account = False
        # 引擎只用来调两个纯逻辑入口；它们不持有连接（`§2.5` 第 4 条）。
        self._quiz = QuizEngine(now_fn=self._now, repo_root=self._root, logger=self._log)
        self._social = SocialEngine(now_fn=self._now, logger=self._log)

    # -- 生命周期 -----------------------------------------------------------
    def start(self) -> threading.Thread:
        """起后台线程（daemon）；重复调用返回同一条线程。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self._thread
            self._stopped.clear()
            self._wake.clear()
            thread = threading.Thread(target=self._loop, name=THREAD_NAME, daemon=True)
            self._thread = thread
            thread.start()
            return thread

    def stop(self, timeout: float = JOIN_TIMEOUT_SECONDS) -> bool:
        """停线程并关连接；返回线程是否真的停了（`§4.2.7`）。"""
        self._stopped.set()
        self._wake.set()                    # 叫醒 wait()，别等满一个 tick 周期
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        alive = thread is not None and thread.is_alive()
        if not alive:
            # 线程已经退出，连接交回这边关；线程还活着就别抢它的连接。
            with self._lock:
                self._close_store()
        return not alive

    def tick(self, now: int | None = None) -> TickReport:
        """跑一轮五个动作；同步、可重入保护、异常全部收口（`§4.2.8`）。"""
        stamp = int(self._now() if now is None else now)
        counts = {item.name: 0 for item in fields(TickReport)}
        with self._lock:
            try:
                account = _account_of(self._ctx)
                if not account:
                    self._warn_no_account()
                    return TickReport()
                store = self._ensure_store(account)
                if store is None:           # 前台还没建库：后台不越俎代庖建空库
                    return TickReport()
                self._run(store, stamp, counts)
            except Exception:
                self._log.exception("playhub 后台 tick 失败")
                counts["errors"] += 1
            report = TickReport(**counts)
            if report.any():
                self._log.info("playhub 后台：%s", report.describe())
            return report

    # -- 线程主体 -----------------------------------------------------------
    def _loop(self) -> None:
        try:
            delay = FIRST_DELAY_SECONDS + self._rng.uniform(0, FIRST_DELAY_JITTER)
            if self._wake.wait(delay) or self._stopped.is_set():
                return
            while not self._stopped.is_set():
                try:
                    self.tick()
                except Exception:           # tick 自己收口过；这里只防线程被异常带走
                    self._log.exception("playhub 后台 tick 线程异常")
                if self._wake.wait(TICK_SECONDS) or self._stopped.is_set():
                    return
        finally:
            with self._lock:
                self._close_store()

    # -- 一轮里的五个动作（顺序固定，`§4.2.6`）-----------------------------
    def _run(self, store, now: int, counts: dict[str, int]) -> None:
        local = _local(now)
        day = clock.day_of(now)
        hm = (local.hour, local.minute)

        result = self._guard("gc", GLOBAL_ROOM, lambda: run_quiz_gc(store, self._quiz, now), counts)
        if isinstance(result, int):
            counts["quiz_closed"] += result

        for room in self._rooms(store, EXPIRE_ROOMS_SQL):
            result = self._guard(
                "expire", room, lambda room=room: run_expire(store, self._social, room, now), counts
            )
            if isinstance(result, int):
                counts["expired"] += result

        for room in self._rooms(store, SALARY_ROOMS_SQL, (KIND_EMPLOY,)):
            result = self._guard(
                "salary", room, lambda room=room: self._salary_room(store, room, day, hm, now), counts
            )
            if isinstance(result, tuple):
                counts["salary_paid"] += result[0]
                counts["salary_skipped"] += result[1]

        week, since_day, until_day = _last_week(now)
        for room in self._rooms(store, CHEST_ROOMS_SQL, (since_day, until_day)):
            result = self._guard(
                "chest",
                room,
                lambda room=room: run_chest(
                    store, room, week, now, account=self._account
                ),
                counts,
            )
            if isinstance(result, int):
                counts["chests"] += result

        result = self._guard("purge", GLOBAL_ROOM, lambda: run_purge(store, now), counts)
        if isinstance(result, tuple):
            counts["idem_purged"], counts["audit_purged"] = result

        since_day = clock.day_of(now - ACTIVE_ROOM_DAYS * 86400)
        for room in self._rooms(store, RENAME_ROOMS_SQL, (since_day,)):
            result = self._guard(
                "rename",
                room,
                lambda room=room: run_rename(
                    store, self._ctx, room, now, send=self._send, logger=self._log
                ),
                counts,
            )
            if isinstance(result, int):
                counts["renamed"] += result

    def _salary_room(self, store, room: str, day: str, hm: tuple[int, int], now: int):
        """一个群的工资：开关关就不动；到期才发；发完写 `sched.last.salary`。"""
        with store.tx(immediate=False) as conn:
            if not switches.is_on(conn, room, switches.EMPLOY):
                return 0, 0
            last = kv.get(conn, room, SCHED_LAST_SALARY)
        if not is_due_daily(last, day, hm, SALARY_AFTER):
            return 0, 0
        paid, skipped = run_salary(store, room, day, now)
        self._touch(store, room, SCHED_LAST_SALARY, day, now)
        return paid, skipped

    @staticmethod
    def _touch(store, room: str, key: str, value: str, now: int) -> None:
        with store.tx() as conn:
            kv.put(conn, room, key, value, now)

    # -- 连接 ---------------------------------------------------------------
    def _ensure_store(self, account: str):
        """按账号复用连接；账号变了就换连接（`§4.2.3`）。"""
        if self._store is not None and self._account == account:
            return self._store
        self._close_store()
        path = self._db_path(account)
        if not path.exists():
            return None
        store = self._store_factory(path, account=account)
        try:
            store.migrate()
        except Exception:
            store.close()
            raise
        self._store, self._account = store, account
        return store

    def _db_path(self, account: str) -> Path:
        from . import db_path_for          # 延迟导入：包入口要先定义它才轮到调度器

        return db_path_for(self._root, account)

    def _close_store(self) -> None:
        store, self._store = self._store, None
        self._account = ""
        if store is None:
            return
        try:
            store.close()
        except Exception:
            self._log.exception("playhub 后台关闭存档失败")

    # -- 收口 ---------------------------------------------------------------
    def _rooms(self, store, sql: str, params=()) -> list[str]:
        try:
            return _rooms(store, sql, params)
        except sqlite3.OperationalError as error:
            if _is_locked(error):
                self._log.warning("playhub 后台读群集合遇到写锁，本次跳过")
                return []
            self._log.exception("playhub 后台读群集合失败")
            return []

    def _guard(self, action: str, room: str, call, counts: dict[str, int]):
        """单个「群 × 动作」的护栏（`§4.2.7`）：失败只丢这一件事。"""
        try:
            return call()
        except sqlite3.OperationalError as error:
            if _is_locked(error):
                # 别的进程/连接持有写锁：跳过即可，下一次 tick 再来，**不写 last_run**
                self._log.warning("playhub 后台 %s 遇到写锁，跳过本次：room=%s", action, room)
                return _LOCKED
            self._log.exception("playhub 后台 %s 失败：room=%s", action, room)
            counts["errors"] += 1
            return _FAILED
        except Exception:
            self._log.exception("playhub 后台 %s 失败：room=%s", action, room)
            counts["errors"] += 1
            return _FAILED

    def _send(self, room: str, text: str) -> None:
        """唯一的主动发群出口；异常交给 `run_rename` 记 audit（`§4.2.4`）。"""
        self._ctx.send_text(room, text)

    def _warn_no_account(self) -> None:
        if self._warned_no_account:
            return
        self._warned_no_account = True
        self._log.warning(
            "playhub 后台：取不到登录账号（ctx.members.account 为空），本次 tick 不做任何事"
        )
