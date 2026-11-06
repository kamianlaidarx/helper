"""后台定时动作（`docs/design/playhub.md` §4.2、验收要点 §4.2.8）。

五个动作（改名巡检 / 离婚超时 / 收题与清理 / 工资 / 周榜银箱）全部在这里
离线跑：临时目录里的真 `Store` + 注入 `now_fn` / `rng` / 假 ctx，
所以 `tick()` 可以同步直调，不 sleep、不联网、不起微信。

线程只测 shell：`start()` 后 `stop()` 必须能立刻收手；把一个群的执行弄炸
不能带走别的群；账号还没登录时 tick 什么都不做。
"""
from __future__ import annotations

import json
import random
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from plugins.playhub import db_path_for
from plugins.playhub.core import Store, balance, credit, ensure_player
from plugins.playhub.core import activity, clock, items, kv
from plugins.playhub.core.clock import CN
from plugins.playhub.engines.social import SocialEngine
from plugins.playhub.scheduler import (
    IDEM_RETENTION_DAYS,
    SCHED_LAST_CHEST,
    SCHED_LAST_PURGE,
    SCHED_LAST_RENAME,
    Scheduler,
    TickReport,
)
from wxbot_framework import game_slot

ACCOUNT = "wxid_bot"
ROOM = "12345678@chatroom"
ROOM_B = "87654321@chatroom"
UID_A = "wxid_a"
UID_B = "wxid_b"
UID_C = "wxid_c"

# 2026-11-04 是周三；改名的 10:00–22:00 窗口、工资的 00:05、银箱的 00:10 都用它推。
NOW = int(datetime.fromisoformat("2026-11-04 10:30:00").replace(tzinfo=CN).timestamp())
LAST_MONDAY = "2026-10-26"
LAST_SUNDAY = "2026-11-01"


def ts(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=CN).timestamp())


class FakeMembers:
    """`ctx.members`：只暴露 account。"""

    def __init__(self, account=ACCOUNT):
        self.account = account


def members(*pairs) -> list[dict]:
    """`ctx.group_members()` 的返回形状（框架给的是 dict 列表）。"""
    return [{"wxid": uid, "nickname": name} for uid, name in pairs]


class FakeCtx:
    """最小 ctx：群成员、白名单、发消息三个能力。"""

    def __init__(self, *, account=ACCOUNT, rows=None, allowed=True, send_error=None):
        self.members = FakeMembers(account)
        self.rows = rows if rows is not None else []
        self.allowed = allowed
        self.send_error = send_error
        self.sent: list[tuple[str, str]] = []

    def group_members(self, room):
        if isinstance(self.rows, Exception):
            raise self.rows
        return self.rows

    def chat_allowed(self, room):
        return self.allowed

    def send_text(self, room, text):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append((room, text))


class CaptureLogger:
    def __init__(self):
        self.records: list[tuple[str, str]] = []

    def exception(self, msg, *args, **kwargs):
        self.records.append(("exception", msg % args if args else msg))

    def warning(self, msg, *args, **kwargs):
        self.records.append(("warning", msg % args if args else msg))

    def debug(self, msg, *args, **kwargs):
        self.records.append(("debug", msg % args if args else msg))

    def info(self, msg, *args, **kwargs):
        self.records.append(("info", msg % args if args else msg))

    def levels(self) -> list[str]:
        return [level for level, _msg in self.records]


class SchedulerBase(unittest.TestCase):
    """一个临时目录 + 已迁移的库（路径与调度器算出来的一致）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.now = NOW
        self.log = CaptureLogger()
        self.ctx = FakeCtx()
        self.path = db_path_for(self.root, ACCOUNT)
        self.path.parent.mkdir(parents=True)
        self.store = self.open_store()
        self.sched = self.make_scheduler()

    # -- 夹具 ---------------------------------------------------------------
    def open_store(self, *, timeout: float = 3.0, name: str | None = None) -> Store:
        store = Store(Path(name) if name else self.path, account=ACCOUNT, timeout=timeout)
        store.migrate()
        self.addCleanup(store.close)
        return store

    def make_scheduler(self, *, ctx=None, **kwargs) -> Scheduler:
        kwargs.setdefault("now_fn", lambda: self.now)
        kwargs.setdefault("rng", random.Random(0))
        kwargs.setdefault("logger", self.log)
        scheduler = Scheduler(ctx=ctx if ctx is not None else self.ctx, root=self.root, **kwargs)
        self.addCleanup(scheduler.stop)
        return scheduler

    def tick(self, now: int | None = None, *, sched: Scheduler | None = None) -> TickReport:
        if now is not None:
            self.now = now
        return (sched or self.sched).tick(self.now)

    # -- 数据 ---------------------------------------------------------------
    def kv(self, key, room=ROOM):
        with self.store.tx(immediate=False) as conn:
            return kv.get(conn, room, key)

    def kv_put(self, key, value, room=ROOM, now=None):
        with self.store.tx() as conn:
            kv.put(conn, room, key, value, self.now if now is None else now)

    def snapshot(self, room=ROOM):
        with self.store.tx(immediate=False) as conn:
            return kv.get_json(conn, room, "groupstat.rename_snapshot")

    def say_activity(self, uid, day, n=1, room=ROOM, nickname=""):
        with self.store.tx() as conn:
            for _ in range(n):
                activity.record(conn, room=room, uid=uid, day=day, now=self.now, nickname=nickname)

    def ensure(self, uid, *, coins=0, room=ROOM):
        with self.store.tx() as conn:
            ensure_player(conn, room=room, uid=uid, nickname="", now=self.now)
            if coins:
                credit(conn, room=room, uid=uid, currency="coin", amount=coins,
                       reason="seed", operation_key=f"seed:{uid}", posting_no=0, now=self.now)

    def coins(self, uid, room=ROOM) -> int:
        with self.store.tx(immediate=False) as conn:
            return balance(conn, room=room, uid=uid, currency="coin")

    def add_pair(self, *, kind="employ", low=UID_A, high=UID_B, by=None, since=None,
                 state="active", meta=None, room=ROOM, created_at=None) -> int:
        with self.store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO pair (room, kind, low_uid, high_uid, state, since, by_uid, meta, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (room, kind, low, high, state, self.now if since is None else since, by,
                 json.dumps(meta or {}, ensure_ascii=False) if meta is not None else None,
                 self.now if created_at is None else created_at),
            )
            return int(cur.lastrowid)

    def nickname_log(self) -> list[tuple[str, str, str]]:
        with self.store.tx(immediate=False) as conn:
            return [
                (str(uid), str(old), str(new))
                for uid, old, new in conn.execute(
                    "SELECT uid, old_name, new_name FROM nickname_log ORDER BY id"
                ).fetchall()
            ]

    def audits(self, action: str | None = None) -> list[dict]:
        sql = "SELECT action, result, detail, room FROM audit"
        params: tuple = ()
        if action is not None:
            sql += " WHERE action = ?"
            params = (action,)
        with self.store.tx(immediate=False) as conn:
            rows = conn.execute(sql + " ORDER BY id", params).fetchall()
        return [
            {"action": a, "result": r, "detail": json.loads(d or "{}"), "room": room}
            for a, r, d, room in rows
        ]

    def idem_keys(self) -> list[str]:
        with self.store.tx(immediate=False) as conn:
            return [str(key) for (key,) in conn.execute("SELECT key FROM idem ORDER BY key").fetchall()]

    def set_switch(self, name, value, room=ROOM):
        self.kv_put(f"feature.{name}", value, room)


# ---------------------------------------------------------------------------
# 动作一：改名巡检
# ---------------------------------------------------------------------------
class RenameTest(SchedulerBase):
    def setUp(self):
        super().setUp()
        self.ensure(UID_A)
        self.say_activity(UID_A, "2026-11-04")
        self.ctx.rows = members((UID_A, "旧名字"), (UID_B, "小B"))

    def test_first_tick_only_builds_the_baseline(self):
        report = self.tick()
        self.assertEqual(report.renamed, 0)
        self.assertEqual(self.ctx.sent, [])                       # 首次不播报
        self.assertEqual(self.snapshot(), {UID_A: "旧名字", UID_B: "小B"})
        self.assertEqual(self.kv(SCHED_LAST_RENAME), "2026-11-04")
        self.assertEqual(self.nickname_log(), [])

    def test_next_day_announces_the_change(self):
        self.tick()
        self.ctx.rows = members((UID_A, "新名字"), (UID_B, "小B"))
        report = self.tick(now=ts("2026-11-05 10:30"))
        self.assertEqual(report.renamed, 1)
        self.assertEqual(len(self.ctx.sent), 1)
        room, text = self.ctx.sent[0]
        self.assertEqual(room, ROOM)
        self.assertIn("旧名字 → 新名字", text)
        self.assertEqual(self.nickname_log(), [(UID_A, "旧名字", "新名字")])
        self.assertEqual(self.snapshot()[UID_A], "新名字")

    def test_same_day_second_tick_does_not_report_again(self):
        self.tick()
        self.ctx.rows = members((UID_A, "新名字"), (UID_B, "小B"))
        report = self.tick(now=ts("2026-11-04 18:00"))            # 同日、仍在窗口内
        self.assertEqual(report.renamed, 0)
        self.assertEqual(self.ctx.sent, [])
        self.assertEqual(self.nickname_log(), [])                  # 留到明天再说

    def test_watch_off_skips_the_room_entirely(self):
        self.kv_put("groupstat.rename_watch", "0")
        report = self.tick()
        self.assertEqual(report.renamed, 0)
        self.assertEqual(self.snapshot(), None)
        self.assertEqual(self.kv(SCHED_LAST_RENAME), None)

    def test_not_in_allowlist_writes_state_but_does_not_send(self):
        self.ctx.allowed = False
        self.tick()
        self.ctx.rows = members((UID_A, "新名字"), (UID_B, "小B"))
        report = self.tick(now=ts("2026-11-05 10:30"))
        self.assertEqual(report.renamed, 1)
        self.assertEqual(self.ctx.sent, [])
        self.assertEqual(len(self.nickname_log()), 1)             # 照常落库
        skipped = [
            a for a in self.audits("sched:rename")
            if a["result"] == "skipped" and a["detail"].get("changes") == 1
        ]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["detail"], {"why": "not_allowed", "changes": 1})

    def test_ctx_without_chat_allowed_does_not_send(self):
        bare = SimpleNamespace(
            members=FakeMembers(),
            rows=members((UID_A, "旧名字")),
            group_members=lambda room: members((UID_A, "旧名字")),
            send_text=lambda room, text: self.ctx.sent.append((room, text)),
        )
        sched = self.make_scheduler(ctx=bare)
        self.tick(sched=sched)
        bare.group_members = lambda room: members((UID_A, "新名字"))
        self.tick(now=ts("2026-11-05 10:30"), sched=sched)
        self.assertEqual(self.ctx.sent, [])                       # 老 ctx 一律不播报
        self.assertEqual(len(self.nickname_log()), 1)

    def test_members_none_skips_without_writing_last_run(self):
        self.ctx.rows = None
        report = self.tick()
        self.assertEqual(report.renamed, 0)
        self.assertEqual(self.kv(SCHED_LAST_RENAME), None)        # 下次还能补上
        self.assertEqual(self.snapshot(), None)

    def test_members_error_is_treated_as_cache_not_ready(self):
        # 读成员缓存失败与「缓存没就绪」同处理：不发、不写 last_run、快照不动。
        # 这里不该记 error——下一次 tick 会自己重试，不需要人工介入。
        self.ctx.rows = RuntimeError("微信尚未连接")
        report = self.tick()
        self.assertEqual(report.errors, 0)
        self.assertEqual(self.kv(SCHED_LAST_RENAME), None)
        self.assertEqual(self.snapshot(), None)

    def test_after_22_waits_for_next_morning(self):
        self.tick()                                               # 当天 10:30 建基线
        self.ctx.rows = members((UID_A, "新名字"), (UID_B, "小B"))
        self.assertEqual(self.tick(now=ts("2026-11-04 23:00")).renamed, 0)
        self.assertEqual(self.ctx.sent, [])                       # 22:00 后不播报
        report = self.tick(now=ts("2026-11-05 10:00"))
        self.assertEqual(report.renamed, 1)                       # 第二天上午补报
        self.assertIn("旧名字 → 新名字", self.ctx.sent[0][1])

    def test_send_failure_marks_audit_failed_and_is_not_retried(self):
        self.tick()
        self.ctx.rows = members((UID_A, "新名字"), (UID_B, "小B"))
        self.ctx.send_error = RuntimeError("发送炸了")
        report = self.tick(now=ts("2026-11-05 10:30"))
        self.assertEqual(report.renamed, 1)
        failed = [a for a in self.audits("sched:rename") if a["result"] == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["detail"], {"error": "RuntimeError"})
        self.assertEqual(self.snapshot()[UID_A], "新名字")         # 快照已更新

        self.ctx.send_error = None
        self.assertEqual(self.tick(now=ts("2026-11-06 10:30")).renamed, 0)
        self.assertEqual(self.ctx.sent, [])                       # 不重试，也不补报

    def test_member_seen_nickname_does_not_cause_false_positive(self):
        # 成员发言会把 `member_seen.nickname` 写成微信名；基线用的是独立快照，
        # 所以这里不该冒出一条「改名」。
        self.tick()
        with self.store.tx() as conn:
            activity.record(conn, room=ROOM, uid=UID_A, day="2026-11-05", now=self.now,
                            nickname="微信名")
        self.ctx.rows = members((UID_A, "旧名字"), (UID_B, "小B"))
        self.assertEqual(self.tick(now=ts("2026-11-05 10:30")).renamed, 0)
        self.assertEqual(self.nickname_log(), [])

    def test_bot_itself_is_not_reported(self):
        self.ctx.rows = members((UID_A, "旧名字"), (ACCOUNT, "机器人"))
        self.tick()
        self.ctx.rows = members((UID_A, "旧名字"), (ACCOUNT, "机器人改名了"))
        self.assertEqual(self.tick(now=ts("2026-11-05 10:30")).renamed, 0)
        self.assertNotIn(ACCOUNT, self.snapshot())


# ---------------------------------------------------------------------------
# 动作二：离婚超时退款
# ---------------------------------------------------------------------------
class ExpireTest(SchedulerBase):
    def divorce(self, *, deadline: int, by=UID_A) -> int:
        pair_id = self.add_pair(kind="marriage", low=UID_A, high=UID_B, by=by, since=NOW - 86400)
        self.kv_put(
            f"divorce:{pair_id}",
            json.dumps({"by": by, "at": NOW - 60, "deadline": deadline,
                        "low": UID_A, "high": UID_B}),
        )
        return pair_id

    def pair_state(self, pair_id: int) -> str:
        with self.store.tx(immediate=False) as conn:
            return str(conn.execute("SELECT state FROM pair WHERE id = ?", (pair_id,)).fetchone()[0])

    def test_timed_out_divorce_ends_and_refunds_once(self):
        self.ensure(UID_A, coins=0)
        pair_id = self.divorce(deadline=NOW - 10)
        report = self.tick()
        self.assertEqual(report.expired, 1)
        self.assertEqual(self.pair_state(pair_id), "ended")
        self.assertEqual(self.coins(UID_A), 200)                  # DIVORCE_COST 退回
        self.assertEqual(self.kv(f"divorce:{pair_id}"), None)

        self.assertEqual(self.tick().expired, 0)                  # 再 tick 不重复退
        self.assertEqual(self.coins(UID_A), 200)

    def test_foreground_expire_first_still_pays_only_once(self):
        self.ensure(UID_A)
        pair_id = self.divorce(deadline=NOW - 10)
        social = SocialEngine(now_fn=lambda: NOW)
        with self.store.tx() as conn:
            social.expire_room(conn, ROOM, NOW)                   # 前台先收尾
        self.assertEqual(self.coins(UID_A), 200)
        self.assertEqual(self.tick().expired, 0)
        self.assertEqual(self.coins(UID_A), 200)
        self.assertEqual(self.pair_state(pair_id), "ended")

    def test_pending_proposal_expires_without_refund(self):
        self.ensure(UID_A)
        pair_id = self.add_pair(kind="marriage", low=UID_A, high=UID_B, by=UID_A,
                                state="pending", created_at=NOW - 90000)
        report = self.tick()
        self.assertEqual(report.expired, 1)
        self.assertEqual(self.pair_state(pair_id), "ended")
        self.assertEqual(self.coins(UID_A), 0)                    # pending 单没有手续费

    def test_not_yet_due_is_left_alone(self):
        pair_id = self.divorce(deadline=NOW + 3600)
        self.assertEqual(self.tick().expired, 0)
        self.assertEqual(self.pair_state(pair_id), "active")
        self.assertIsNotNone(self.kv(f"divorce:{pair_id}"))


# ---------------------------------------------------------------------------
# 动作三：收题 + 过期数据清理
# ---------------------------------------------------------------------------
class GarbageCollectTest(SchedulerBase):
    def test_sweep_closes_round_but_keeps_the_slot(self):
        self.add_pair(kind="marriage", low=UID_A, high=UID_B, since=NOW - 86400)
        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO quiz_round (room, category, question_id, answer_norm, started_at, expires_at)"
                " VALUES (?, 'trivia', 'q1', 'x', ?, ?)",
                (ROOM, NOW - 600, NOW - 60),
            )
        game_slot.claim(ROOM, "playhub", label="竞猜")
        self.addCleanup(game_slot.release, ROOM)
        report = self.tick()
        self.assertEqual(report.quiz_closed, 1)
        with self.store.tx(immediate=False) as conn:
            row = conn.execute("SELECT closed_at, expires_at FROM quiz_round").fetchone()
        self.assertEqual(row[0], row[1])                          # closed_at = expires_at
        self.assertIsNotNone(game_slot.holder(ROOM))              # 后台不替前台释放槽位

    def test_purge_removes_31_day_old_idem_and_181_day_old_audit(self):
        with self.store.tx() as conn:
            conn.execute("INSERT INTO idem (key, scope, at) VALUES ('old', 'x', ?)",
                         (NOW - 31 * 86400,))
            conn.execute("INSERT INTO idem (key, scope, at) VALUES ('fresh', 'x', ?)",
                         (NOW - 29 * 86400,))
            conn.execute("INSERT INTO audit (at, room, actor, action, result) VALUES (?, ?, 's', 'old', 'ok')",
                         (NOW - 181 * 86400, ROOM))
            conn.execute("INSERT INTO audit (at, room, actor, action, result) VALUES (?, ?, 's', 'fresh', 'ok')",
                         (NOW - 179 * 86400, ROOM))
        report = self.tick(now=ts("2026-11-04 04:30"))
        self.assertEqual((report.idem_purged, report.audit_purged), (1, 1))
        self.assertEqual(self.idem_keys(), ["fresh"])
        with self.store.tx(immediate=False) as conn:
            actions = [row[0] for row in conn.execute("SELECT action FROM audit ORDER BY id")]
        self.assertNotIn("old", actions)
        self.assertIn("fresh", actions)
        self.assertEqual(self.kv(SCHED_LAST_PURGE, room="*"), "2026-11-04")

        # 同一天第二次不重跑
        report = self.tick(now=ts("2026-11-04 05:30"))
        self.assertEqual((report.idem_purged, report.audit_purged), (0, 0))

    def test_purge_before_four_am_waits(self):
        with self.store.tx() as conn:
            conn.execute("INSERT INTO idem (key, scope, at) VALUES ('old', 'x', ?)",
                         (NOW - 31 * 86400,))
        report = self.tick(now=ts("2026-11-04 03:59"))
        self.assertEqual(report.idem_purged, 0)
        self.assertEqual(self.idem_keys(), ["old"])

    def test_purge_batches_and_never_touches_the_ledger(self):
        with self.store.tx() as conn:
            conn.executemany(
                "INSERT INTO idem (key, scope, at) VALUES (?, 'x', ?)",
                [(f"k{i}", NOW - 40 * 86400) for i in range(12_000)],
            )
        self.ensure(UID_A, coins=10)
        with self.store.tx() as conn:
            conn.execute("UPDATE wallet_ledger SET at = ?", (NOW - 400 * 86400,))
            ledger_before = int(conn.execute("SELECT COUNT(*) FROM wallet_ledger").fetchone()[0])
        report = self.tick(now=ts("2026-11-04 04:30"))
        self.assertEqual(report.idem_purged, 12_000)
        self.assertEqual(self.idem_keys(), [])
        with self.store.tx(immediate=False) as conn:
            self.assertEqual(
                int(conn.execute("SELECT COUNT(*) FROM wallet_ledger").fetchone()[0]), ledger_before
            )


# ---------------------------------------------------------------------------
# 动作四：发工资
# ---------------------------------------------------------------------------
class SalaryTest(SchedulerBase):
    def setUp(self):
        super().setUp()
        self.set_switch("employ", "on")

    def employ(self, *, employer=UID_A, wage=None, since="2026-11-03", **kwargs) -> int:
        meta = {"wage": wage} if wage is not None else None
        return self.add_pair(kind="employ", low=UID_A, high=UID_B, by=employer,
                             since=ts(f"{since} 12:00"), meta=meta, **kwargs)

    def test_salary_is_not_paid_on_the_deal_day(self):
        self.ensure(UID_A, coins=100)
        self.ensure(UID_B)
        pair_id = self.employ(since="2026-11-04")
        report = self.tick()                                      # 成交当天 10:30
        self.assertEqual(report.salary_paid, 0)
        self.assertEqual(self.coins(UID_A), 100)

    def test_salary_paid_next_day_with_two_postings(self):
        self.ensure(UID_A, coins=100)
        self.ensure(UID_B)
        pair_id = self.employ(since="2026-11-03")
        report = self.tick()
        self.assertEqual(report.salary_paid, 1)
        self.assertEqual(self.coins(UID_A), 80)
        self.assertEqual(self.coins(UID_B), 20)
        with self.store.tx(immediate=False) as conn:
            rows = conn.execute(
                "SELECT uid, operation_key, posting_no, reason, delta FROM wallet_ledger"
                " WHERE operation_key LIKE 'sched:salary:%' ORDER BY posting_no"
            ).fetchall()
        self.assertEqual([row[2] for row in rows], [0, 1])        # 同 key、posting 0/1
        self.assertEqual({row[1] for row in rows}, {f"sched:salary:{pair_id}:2026-11-04"})
        self.assertEqual([row[3] for row in rows], ["salary_pay", "salary_income"])

    def test_same_day_ticks_pay_once(self):
        self.ensure(UID_A, coins=100)
        self.ensure(UID_B)
        self.employ(since="2026-11-03")
        self.tick()
        self.tick(now=ts("2026-11-04 18:00"))
        self.assertEqual(self.coins(UID_B), 20)

    def test_by_uid_can_be_either_side(self):
        self.ensure(UID_A)
        self.ensure(UID_B, coins=100)
        self.employ(employer=UID_B, since="2026-11-03")           # 雇主是高 uid
        self.assertEqual(self.tick().salary_paid, 1)
        self.assertEqual(self.coins(UID_B), 80)
        self.assertEqual(self.coins(UID_A), 20)

    def test_custom_wage_from_meta(self):
        self.ensure(UID_A, coins=100)
        self.ensure(UID_B)
        self.employ(since="2026-11-03", wage=50)
        self.tick()
        self.assertEqual(self.coins(UID_A), 50)
        self.assertEqual(self.coins(UID_B), 50)

    def test_insufficient_leaves_both_sides_untouched(self):
        self.ensure(UID_A, coins=10)
        self.ensure(UID_B)
        pair_id = self.employ(since="2026-11-03")
        report = self.tick()
        self.assertEqual(report.salary_skipped, 1)
        self.assertEqual(self.coins(UID_A), 10)
        self.assertEqual(self.coins(UID_B), 0)
        failed = [a for a in self.audits("sched:salary") if a["result"] == "failed"]
        self.assertEqual(failed[0]["detail"]["why"], "insufficient")
        with self.store.tx(immediate=False) as conn:
            state = conn.execute("SELECT state FROM pair WHERE id = ?", (pair_id,)).fetchone()[0]
        self.assertEqual(state, "active")                         # 雇佣关系不受影响

    def test_catches_up_missed_days_but_only_three(self):
        # 停机 5 天：只补最近 3 天，不无限补发（§4.2.4）。
        self.ensure(UID_A, coins=1000)
        self.ensure(UID_B)
        self.employ(since="2026-10-28")
        report = self.tick(now=ts("2026-11-04 00:10"))
        self.assertEqual(report.salary_paid, 3)
        self.assertEqual(self.coins(UID_B), 60)

    def test_switch_off_pays_nothing_and_does_not_touch_last_run(self):
        self.ensure(UID_A, coins=100)
        self.ensure(UID_B)
        self.employ(since="2026-11-03")
        self.set_switch("employ", "0")
        self.assertEqual(self.tick().salary_paid, 0)
        self.assertEqual(self.coins(UID_B), 0)
        self.assertEqual(self.kv("sched.last.salary"), None)

    def test_missing_switch_means_off(self):
        self.ensure(UID_A, coins=100)
        self.ensure(UID_B)
        self.employ(since="2026-11-03")
        with self.store.tx() as conn:                             # 清掉开关 = 读不到
            conn.execute("DELETE FROM kv WHERE key = 'feature.employ'")
        self.assertEqual(self.tick().salary_paid, 0)
        self.assertEqual(self.coins(UID_B), 0)

    def test_bad_by_uid_is_skipped_and_audited(self):
        self.ensure(UID_A, coins=100)
        self.ensure(UID_B)
        self.add_pair(kind="employ", low=UID_A, high=UID_B, by="", since=ts("2026-11-03 12:00"))
        report = self.tick()
        self.assertEqual(report.salary_paid, 0)
        failed = [a for a in self.audits("sched:salary") if a["result"] == "failed"]
        self.assertEqual(failed[0]["detail"]["why"], "bad_by_uid")


# ---------------------------------------------------------------------------
# 动作五：周榜银箱
# ---------------------------------------------------------------------------
class ChestTest(SchedulerBase):
    def setUp(self):
        super().setUp()
        for index in range(12):
            uid = f"wxid_top{index:02d}"
            self.ensure(uid)
            self.say_activity(uid, LAST_SUNDAY, n=index + 1)
        self.ensure(UID_C)

    def silver(self, uid) -> int:
        with self.store.tx(immediate=False) as conn:
            return items.qty(conn, ROOM, uid, items.CHEST_SILVER)

    def test_wednesday_backfills_last_week(self):
        report = self.tick()
        self.assertEqual(report.chests, 10)
        self.assertEqual(self.ctx.sent, [])                       # 不播报
        # top11 是 wxid_top11（12 条），第 11 名往后没箱子
        self.assertEqual(self.silver("wxid_top11"), 1)
        self.assertEqual(self.silver("wxid_top02"), 1)
        self.assertEqual(self.silver("wxid_top01"), 0)
        self.assertEqual(self.kv(SCHED_LAST_CHEST), "2026-W44")
        self.assertIn(f"sched:chest:{ROOM}:2026-W44", self.idem_keys())

    def test_this_week_activity_is_not_counted(self):
        self.say_activity(UID_C, "2026-11-03", n=50)              # 本周二
        self.ensure(UID_C)
        self.tick()
        self.assertEqual(self.silver(UID_C), 0)

    def test_monday_before_ten_past_waits(self):
        self.assertEqual(self.tick(now=ts("2026-11-02 00:05")).chests, 0)
        self.assertEqual(self.tick(now=ts("2026-11-02 00:10")).chests, 10)

    def test_blocked_member_is_excluded(self):
        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO blocklist (room, uid, scope, reason, by_uid, at) VALUES (?, ?, 'play', 'x', 's', ?)",
                (ROOM, "wxid_top11", NOW),
            )
        self.tick()
        self.assertEqual(self.silver("wxid_top11"), 0)
        self.assertEqual(self.silver("wxid_top10"), 1)            # 顺位补上来

    def test_same_week_never_pays_twice_even_without_last_run(self):
        self.tick()
        with self.store.tx() as conn:
            kv.clear(conn, ROOM, SCHED_LAST_CHEST)
        self.assertEqual(self.tick().chests, 0)
        self.assertEqual(self.silver("wxid_top11"), 1)            # idem 挡住重发

    def test_two_weeks_downtime_only_pays_the_latest_week(self):
        for index in range(12):                                   # 最近这一周的活跃
            self.say_activity(f"wxid_top{index:02d}", "2026-11-15", n=index + 1)
        report = self.tick(now=ts("2026-11-18 10:30"))            # 两周后的周三
        self.assertEqual(report.chests, 10)                       # 只发最近一周
        self.assertEqual(self.kv(SCHED_LAST_CHEST), "2026-W46")
        self.assertEqual(len([k for k in self.idem_keys() if k.startswith("sched:chest")]), 1)


# ---------------------------------------------------------------------------
# 线程壳：start/stop、异常隔离、账号未就绪
# ---------------------------------------------------------------------------
class ThreadTest(SchedulerBase):
    def test_stop_right_after_start_returns_quickly(self):
        scheduler = self.make_scheduler()
        scheduler.start()
        began = time.monotonic()
        self.assertTrue(scheduler.stop())
        self.assertLess(time.monotonic() - began, 1.0)
        self.assertEqual(scheduler._thread.name, "playhub.scheduler")

    def test_one_broken_room_does_not_stop_the_others(self):
        self.add_pair(kind="marriage", low=UID_A, high=UID_B, state="pending",
                      created_at=NOW - 90000, room=ROOM)
        self.add_pair(kind="marriage", low=UID_A, high=UID_B, state="pending",
                      created_at=NOW - 90000, room=ROOM_B)

        def flaky(store, social, room, now):
            if room == ROOM:
                raise RuntimeError("这个群炸了")
            return 1

        with mock.patch("plugins.playhub.scheduler.run_expire", side_effect=flaky):
            report = self.tick()
        self.assertEqual(report.errors, 1)
        self.assertGreaterEqual(report.expired, 1)                # 另一个群照常处理

    def test_no_account_does_nothing_and_does_not_open_the_db(self):
        self.ctx.members = FakeMembers("")
        report = self.tick()
        self.assertFalse(report.any())
        self.assertIsNone(self.sched._store)                      # 没去开库
        self.assertIn("warning", self.log.levels())               # 只提示一次


class LockedTest(SchedulerBase):
    def test_locked_room_is_skipped_and_retried_next_tick(self):
        self.ensure(UID_A)
        self.ctx.rows = members((UID_A, "旧名字"))
        self.tick()                                               # 先把连接/迁移走完
        self.say_activity(UID_A, "2026-11-04")
        with self.store.tx() as conn:                             # 另一条连接持写锁
            conn.execute("SELECT 1").fetchall()
            report = self.tick(now=ts("2026-11-05 10:30"))
        self.assertEqual(report.errors, 0)                        # 写锁不是「出错」
        self.assertEqual(self.kv(SCHED_LAST_RENAME), None)        # 不写 last_run
        self.assertEqual(self.snapshot(), None)
        report = self.tick(now=ts("2026-11-05 10:31"))            # 锁释放后补上
        self.assertEqual(report.errors, 0)
        self.assertEqual(self.snapshot(), {UID_A: "旧名字"})


class PluginLifecycleTest(unittest.TestCase):
    """`on_load` 起线程、`on_unload` 停线程（§4.2.7）。"""

    def test_on_unload_stops_the_scheduler_thread(self):
        import logging

        from plugins.playhub import Plugin

        with tempfile.TemporaryDirectory() as tmp:
            ctx = SimpleNamespace(
                project_root=Path(tmp),
                members=FakeMembers(),
                logger=logging.getLogger("test.playhub"),
            )
            plugin = Plugin()
            plugin.on_load(ctx)
            scheduler = plugin._scheduler
            self.assertIsNotNone(scheduler)
            thread = scheduler._thread
            self.assertTrue(thread.is_alive())
            self.assertEqual(thread.name, "playhub.scheduler")

            plugin.on_unload(ctx)
            self.assertFalse(thread.is_alive())                  # 10 秒内收手（立刻叫醒）
            self.assertIsNone(plugin._scheduler)


if __name__ == "__main__":
    unittest.main()
