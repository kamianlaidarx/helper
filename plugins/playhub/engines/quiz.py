"""`quiz` 引擎：7 类竞猜 + 文字找茬，共用一张 `quiz_round`
（`docs/design/playhub.md:438-446` 引擎协议、`:1548-1559` 第 2 批验收、§5.4 题库管线）。

设计上的四个要点，改代码前先读：

1. **答案不存明文**：`quiz_round.answer_norm` 存 JSON 数组形式的 sha256
   （一条题可以有多个等价答案）。公布答案时才按 `question_id` 回题库取明文，
   所以 `quiz_round` 泄露也看不到答案。
2. **子命令复用框架词**：`提示`/`换一题`/`公布答案`/`结束` 是框架
   `GROUP_CONTROL_COMMANDS` 里的词（`wxbot_framework/text.py:8-10`），playhub
   **不注册**它们；只有本群存在未结束对局时才消费，否则立刻返回、绝不回包。
3. **关键词优先于纯文本**：`app.handle_message` 先 `route()`，命中命令就不再进
   `on_plain_text`（`:577`）。所以 `签到` 不会被当成答案，抢答也不会吃掉 61 个关键词。
4. **违规与错答一律静默**：错答不回复（群聊里"不对"刷屏比冷场更烦），
   只落 `quiz_attempt` 一行；`Reply` 只在答对 / 用子命令时构造，且一律在 COMMIT 之后。
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
from dataclasses import dataclass
from typing import Any

from wxbot_framework.game_slot import claim as claim_slot
from wxbot_framework.game_slot import release as release_slot

from ..content.loader import Bank, BankError, load_bank
from ..content.validate import normalize
from ..core import balance, clock, credit, ensure_player, render
from ..core.errors import DuplicatePosting
from . import Reply, msg_nickname, msg_room, msg_uid, store_of

# action → 题库类目。键必须与 keywords.py 的 action 逐字一致。
CATEGORY_BY_ACTION: dict[str, str] = {
    "quiz.song": "song",
    "quiz.riddle": "riddle",
    "quiz.movie": "movie",
    "quiz.food": "food",
    "quiz.honor": "honor",
    "quiz.anime": "anime",
    "quiz.guoman": "guoman",
    "quiz.diff": "diff",
}

#: 类目 → 卡面图标（小V 每个玩法一个固定图标，群里一眼分辨）
CATEGORY_ICON: dict[str, str] = {
    "song": "🎵",
    "riddle": "🧩",
    "movie": "🎬",
    "food": "🍜",
    "honor": "⚔️",
    "anime": "🎌",
    "guoman": "🐉",
    "diff": "🔍",
}

CATEGORY_LABEL: dict[str, str] = {
    "song": "猜歌名",
    "riddle": "猜谜语",
    "movie": "猜电影",
    "food": "猜美食",
    "honor": "猜王者",
    "anime": "猜动漫",
    "guoman": "猜国漫",
    "diff": "文字找茬",
}

# 框架控制词：只在有未结束对局时消费（见文件头第 2 条）。
SUBCOMMANDS = frozenset({"提示", "换一题", "公布答案"})

# 结束词只有一个：`结束` 就是「结束本群当前这一局」，不按类目分。
# `退出猜成语` 保留兼容（它是框架 GROUP_CONTROL_COMMANDS 里的词）。
EXIT_WORDS = frozenset({"结束", "退出猜成语"})

ROUND_SECONDS = 300              # 单题时限；quiz_gc 的默认值与此对齐
SLOT_OWNER = "playhub"           # 共享游戏槽（wxbot_framework/game_slot.py）里的署名
RECENT_ROUNDS = 50               # 同群近 N 题不重复（:1442）
RECENT_DAYS = 7                  # 7 天内出过的题不再出
MAX_ATTEMPTS_PER_ROUND = 200     # 单局作答上限，防刷屏
ATTEMPT_TEXT_MAX = 64            # 与 quiz_attempt.text 的列宽约定一致

# 奖励：§4.4 数值表原先没有竞猜项（已在文档补记）。取值口径是「一天答对
# 10 题 ≈ 一次签到」，有提示时减半，避免提示沦为免费答案。
COIN_CORRECT = 10
COIN_CORRECT_HINTED = 5
WIN_CAP_PER_DAY = 10
WIN_KEY = "quiz_win"
COIN_REASON = "quiz_win"

_IDIOM_HINT = "\n（本群开着成语接龙，抢答可能被接龙判定先吃掉；建议先发「退出接龙」。）"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hashes_for(question: Any) -> str:
    """把可接受答案编成 JSON 数组（每项一个 sha256），落 `answer_norm`。"""
    hashes = sorted({_sha(n) for n in question.accepted})
    return json.dumps(hashes)


def _load_hashes(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, str) or not raw:
        return ()
    try:
        data = json.loads(raw)
    except ValueError:
        return ()
    if not isinstance(data, list):
        return ()
    return tuple(h for h in data if isinstance(h, str))


@dataclass(frozen=True)
class OpenRound:
    id: int
    category: str
    question_id: str
    answer_norm: str
    hint_level: int
    expires_at: int
    attempts: int


_OPEN_SQL = """
SELECT id, category, question_id, answer_norm, hint_level, expires_at, attempts
FROM quiz_round WHERE room = ? AND closed_at IS NULL
"""


def _row_to_round(row) -> OpenRound | None:
    if row is None:
        return None
    return OpenRound(
        id=int(row[0]),
        category=row[1],
        question_id=row[2],
        answer_norm=row[3],
        hint_level=int(row[4]),
        expires_at=int(row[5]),
        attempts=int(row[6]),
    )


def _counter_used(conn, *, room: str, uid: str, day: str, key: str) -> int:
    row = conn.execute(
        "SELECT used FROM daily_counter WHERE room = ? AND uid = ? AND day = ? AND key = ?",
        (room, uid, day, key),
    ).fetchone()
    return int(row[0]) if row is not None else 0


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


class QuizEngine:
    """`quiz` 引擎。随机源与时钟都必须可注入，测试才不会随机翻车。"""

    name = "quiz"

    def __init__(self, *, rng=None, now_fn=None, repo_root=None, logger=None):
        self._rng = rng if rng is not None else random.Random()
        self._now = now_fn if now_fn is not None else clock.now_ts
        self._repo_root = repo_root
        self._log = logger if logger is not None else logging.getLogger("playhub.engines.quiz")

    # -- 分发 ---------------------------------------------------------------
    def on_command(self, cmd, msg=None, ctx=None, game=None) -> Reply | None:
        category = CATEGORY_BY_ACTION.get(getattr(cmd, "action", ""))
        if category is None:
            return None
        return self.start(msg, ctx, game, category)

    def on_plain_text(self, msg, ctx=None, game=None) -> Reply | None:
        """非关键词文本入口：只在本群有未结束对局时消费（见文件头第 2/3 条）。"""
        room, uid = msg_room(msg), msg_uid(msg)
        if not room or not uid:
            return None
        text = getattr(msg, "text", "") or ""
        text = text.strip()
        if not text:
            return None

        store = store_of(game)
        now = int(self._now())
        current = self._read_open(store, room)
        if current is None or current.expires_at <= now:
            return None                      # 没有对局 / 已超时：什么都不做

        if text in SUBCOMMANDS or text in EXIT_WORDS:
            return self._subcommand(store, msg, ctx, room, uid, text, now)
        return self._judge(store, msg, room, uid, text, now)

    # -- 开局 ---------------------------------------------------------------
    def start(self, msg, ctx=None, game=None, category: str | None = None) -> Reply | None:
        if category is None:
            return None
        room, uid = msg_room(msg), msg_uid(msg)
        if not room or not uid:
            return None
        store = store_of(game)
        now = int(self._now())
        label = CATEGORY_LABEL.get(category, category)

        try:
            bank = self._bank(store, category)
        except BankError as exc:
            self._log.warning("题库不可用：category=%s reason=%s", category, exc.reason)
            return Reply(f"「{label}」的题库还没准备好，回头再试试。", f"quiz.{category}")

        # 一个群同时只玩一局（不管那是哪个插件的玩法）：先抢槽，被占就不开局。
        busy = claim_slot(room, SLOT_OWNER, label=label, ttl=ROUND_SECONDS)
        if busy is not None:
            return Reply(f"本群还有一局{busy}没结束，先发「结束」。", f"quiz.{category}")

        # 同一群同时只留一局（`quiz_one_open` 部分唯一索引）：新局开起来，
        # 上一局不论哪个类目都就地结束，并在回包里说清关了哪一局。
        with store.tx() as conn:
            self._sweep(conn, now, room=room)
            previous = _row_to_round(conn.execute(_OPEN_SQL, (room,)).fetchone())
            if previous is not None:
                conn.execute(
                    "UPDATE quiz_round SET closed_at = ? WHERE id = ? AND closed_at IS NULL",
                    (now, previous.id),
                )
            question = self._pick(conn, bank, room, now)
            text = self._open_round(conn, room, category, bank, question, now)
        if previous is not None:
            closed_label = CATEGORY_LABEL.get(previous.category, previous.category)
            text = f"上一局（{closed_label}）没答完，已结束。\n{text}"
        return Reply(self._decorate(text, ctx, msg), f"quiz.{category}")

    # -- 抢答 ---------------------------------------------------------------
    def _judge(self, store, msg, room: str, uid: str, text: str, now: int) -> Reply | None:
        with store.tx() as conn:
            current = _row_to_round(conn.execute(_OPEN_SQL, (room,)).fetchone())
            if current is None or current.expires_at <= now:
                return None
            if current.attempts >= MAX_ATTEMPTS_PER_ROUND:
                return None
            hashes = _load_hashes(current.answer_norm)
            norm = normalize(text)           # 与题库同一套归一化（全角/标点/大小写）
            hit = bool(norm) and bool(hashes) and _sha(norm) in hashes

            conn.execute(
                "INSERT INTO quiz_attempt (round_id, uid, text, hit, at) VALUES (?, ?, ?, ?, ?)",
                (current.id, uid, text[:ATTEMPT_TEXT_MAX], 1 if hit else 0, now),
            )
            conn.execute(
                "UPDATE quiz_round SET attempts = attempts + 1 WHERE id = ?", (current.id,)
            )
            if not hit:
                return None                  # 错答静默：只留流水

            closed = conn.execute(
                "UPDATE quiz_round SET closed_at = ?, winner_uid = ? WHERE id = ? AND closed_at IS NULL",
                (now, uid, current.id),
            ).rowcount
            if not closed:                   # 并发下已被别人收题
                return None
            conn.execute(
                "UPDATE quiz_stat SET correct = correct + 1 WHERE question_id = ?",
                (current.question_id,),
            )

            question = self._question(store, current.category, current.question_id)
            answer = question.display if question is not None else "（题库缺失）"
            coin = self._payout(conn, room=room, uid=uid, msg=msg, now=now,
                                round_id=current.id, hinted=current.hint_level > 0)

        release_slot(room, SLOT_OWNER)      # 答对即收题，槽位立刻让出来
        who = msg_nickname(msg) or "有人"
        rows = [render.row("├ ", f"答案：{answer}")]
        if coin:
            rows.append(render.row("├ ", f"金币：+{coin}"))
        else:
            rows.append(render.row("├ ", f"金币：今天的答题奖励已领满（{WIN_CAP_PER_DAY} 次）"))
        bal = self._coin_balance(store, room, uid)
        if bal is not None:
            rows.append(render.current("💰", "金币", bal))
        return Reply(render.result(who, "🎉 答对了，这题是你的", rows), f"quiz.{current.category}")

    # -- 子命令 -------------------------------------------------------------
    def _subcommand(self, store, msg, ctx, room: str, uid: str, text: str, now: int) -> Reply | None:
        with store.tx() as conn:
            current = _row_to_round(conn.execute(_OPEN_SQL, (room,)).fetchone())
            if current is None or current.expires_at <= now:
                return None
            question = self._question(store, current.category, current.question_id)

            if text == "提示":
                hints = question.hints if question is not None else ()
                if current.hint_level >= len(hints):
                    return Reply(render.tip("💡", "这题的提示已经给完了，可以发「公布答案」"), "quiz.hint")
                level = current.hint_level + 1
                conn.execute("UPDATE quiz_round SET hint_level = ? WHERE id = ?", (level, current.id))
                return Reply(
                    render.tip("💡", f"提示 {level}/{len(hints)}：{hints[level - 1]}"), "quiz.hint"
                )

            answer = question.display if question is not None else "（题库缺失）"
            conn.execute(
                "UPDATE quiz_round SET closed_at = ? WHERE id = ? AND closed_at IS NULL",
                (now, current.id),
            )
            if text == "公布答案":
                release_slot(room, SLOT_OWNER)
                return Reply(
                    render.result(
                        "",
                        f"🎬 答案是「{answer}」",
                        [render.row("📊 ", f"本题共 {current.attempts} 次作答")],
                        at_sender=False,
                    ),
                    "quiz.reveal",
                )
            if text in EXIT_WORDS:
                release_slot(room, SLOT_OWNER)
                return Reply(
                    render.result(
                        "",
                        f"🛑 本局（{CATEGORY_LABEL.get(current.category, current.category)}）已结束",
                        [render.row("🎬 ", f"答案是「{answer}」")],
                        at_sender=False,
                    ),
                    "quiz.quit",
                )

            # 换一题：同一类目再开一局
            try:
                bank = self._bank(store, current.category)
            except BankError:
                release_slot(room, SLOT_OWNER)
                return Reply(f"本局已结束，答案是「{answer}」。", "quiz.next")
            question = self._pick(conn, bank, room, now)
            text_new = self._open_round(conn, room, current.category, bank, question, now)
        # 新局仍是我们的，续期即可；这里若被别的玩法抢走就不该续（claim 返回对方标签）
        claim_slot(
            room, SLOT_OWNER,
            label=CATEGORY_LABEL.get(current.category, current.category),
            ttl=ROUND_SECONDS,
        )
        return Reply(self._decorate(render.row("🔄 ", "换一题") + "\n" + text_new, ctx, msg), "quiz.next")

    # -- 出题 / 选题 ---------------------------------------------------------
    def _pick(self, conn, bank: Bank, room: str, now: int):
        recent = {
            row[0]
            for row in conn.execute(
                "SELECT question_id FROM quiz_round WHERE room = ? ORDER BY id DESC LIMIT ?",
                (room, RECENT_ROUNDS),
            )
        }
        stale = {
            row[0]
            for row in conn.execute(
                "SELECT question_id FROM quiz_stat WHERE last_at IS NOT NULL AND last_at > ?",
                (now - RECENT_DAYS * 86400,),
            )
        }
        candidates = [q for q in bank.questions if q.accepted]
        pool = [q for q in candidates if q.id not in recent and q.id not in stale]
        if not pool:
            pool = [q for q in candidates if q.id not in recent] or candidates
        return self._rng.choice(pool)

    def _open_round(self, conn, room: str, category: str, bank: Bank, question: Any, now: int) -> str:
        conn.execute(
            """
            INSERT INTO quiz_round
                (room, category, question_id, answer_norm, hint_level, started_at, expires_at, attempts)
            VALUES (?, ?, ?, ?, 0, ?, ?, 0)
            """,
            (room, category, question.id, _hashes_for(question), now, now + ROUND_SECONDS),
        )
        conn.execute(
            """
            INSERT INTO quiz_stat (question_id, category, shown, correct, last_at)
            VALUES (?, ?, 1, 0, ?)
            ON CONFLICT(question_id)
            DO UPDATE SET shown = shown + 1, last_at = excluded.last_at
            """,
            (question.id, category, now),
        )
        label = CATEGORY_LABEL.get(category, category)
        icon = CATEGORY_ICON.get(category, "🎯")
        rows = [render.head("❓", question.q)]
        if category == "diff":
            rows.append(render.tip("🔍", "直接发不一样的那个词"))
        else:
            rows.append(render.tip("💡", "直接发答案抢答；「提示」看提示、「公布答案」看答案"))
        rows.append(render.row("⏱ ", f"{ROUND_SECONDS // 60} 分钟内抢答，第一个答对才算"))
        rows.append(render.row("🎁 ", f"答对 +{COIN_CORRECT} 金币，用过提示 +{COIN_CORRECT_HINTED}"))
        return render.menu(icon, label, rows)

    # -- 奖励 ---------------------------------------------------------------
    def _payout(self, conn, *, room: str, uid: str, msg, now: int, round_id: int, hinted: bool) -> int:
        """答题奖励；达日上限返回 0（答对仍成立，只是不发币）。"""
        day = clock.day_of(now)
        if _counter_used(conn, room=room, uid=uid, day=day, key=WIN_KEY) >= WIN_CAP_PER_DAY:
            return 0
        coin = COIN_CORRECT_HINTED if hinted else COIN_CORRECT
        ensure_player(conn, room=room, uid=uid, nickname=msg_nickname(msg) or None, now=now)
        try:
            credit(
                conn,
                room=room,
                uid=uid,
                currency="coin",
                amount=coin,
                reason=COIN_REASON,
                operation_key=f"quiz:{round_id}",
                posting_no=0,
                now=now,
            )
        except DuplicatePosting:             # 同一局重放：钱已发过，不再计次
            self._log.warning("答题奖励重复流水被拦截：room=%s round=%s", room, round_id)
            return 0
        _counter_consume(conn, room=room, uid=uid, day=day, key=WIN_KEY, cap=WIN_CAP_PER_DAY, now=now)
        return coin

    # -- 超时收题 -----------------------------------------------------------
    def _sweep(self, conn, now: int, *, room: str | None = None, release_slots: bool = True) -> int:
        """把超时未结束的对局标记为结束；返回收掉的条数。

        前台懒清理（开局时）与后台定时动作共用：`release_slots=False` 供后台使用
        （§4.2 `gc`）——槽位属于前台运行期状态，后台线程不该替它释放。
        """
        if room is None:
            rooms = [
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT room FROM quiz_round "
                    "WHERE closed_at IS NULL AND expires_at <= ?",
                    (now,),
                )
            ]
            cur = conn.execute(
                "UPDATE quiz_round SET closed_at = expires_at WHERE closed_at IS NULL AND expires_at <= ?",
                (now,),
            )
        else:
            rooms = [room]
            cur = conn.execute(
                "UPDATE quiz_round SET closed_at = expires_at "
                "WHERE room = ? AND closed_at IS NULL AND expires_at <= ?",
                (room, now),
            )
        closed = int(cur.rowcount or 0)
        if closed and release_slots:          # 超时也算结束，槽位必须跟着让出来
            for key in rooms:
                release_slot(key, SLOT_OWNER)
        return closed

    def sweep(self, conn, now: int | None = None, *, release_slots: bool = True) -> int:
        """供定时动作调用（§4.2 `quiz_gc`）。"""
        return self._sweep(
            conn, int(self._now()) if now is None else now, release_slots=release_slots
        )

    # -- 工具 ---------------------------------------------------------------
    def _read_open(self, store, room: str) -> OpenRound | None:
        """读一条未结束对局；读失败按「没有对局」处理（不能因坏库把群聊卡住）。"""
        try:
            with store.tx(immediate=False) as conn:
                return _row_to_round(conn.execute(_OPEN_SQL, (room,)).fetchone())
        except Exception:
            self._log.exception("读取竞猜对局失败：room=%s", room)
            return None

    def _bank(self, store, category: str) -> Bank:
        return load_bank(store, category, repo_root=self._repo_root)

    def _question(self, store, category: str, question_id: str):
        try:
            bank = self._bank(store, category)
        except BankError:
            return None
        return bank.by_id(question_id)

    def _coin_balance(self, store, room: str, uid: str) -> int | None:
        """结果卡上的「当前金币」：读不到就省掉这一行，不让回包因为余额查询失败而消失。"""
        try:
            with store.tx(immediate=False) as conn:
                return balance(conn, room=room, uid=uid, currency="coin")
        except Exception:
            self._log.exception("读取金币余额失败：room=%s uid=%s", room, uid)
            return None

    def _decorate(self, text: str, ctx, msg) -> str:
        """接龙冲突提醒：只探测插件是否激活，不读对方内部状态（§2.7）。"""
        active = getattr(ctx, "plugin_is_active", None) if ctx is not None else None
        if not callable(active):
            return text
        try:
            if active("idiom_chain"):
                return text + _IDIOM_HINT
        except Exception:
            return text
        return text
