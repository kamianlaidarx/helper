"""`quiz` 引擎单测：出题、抢答、提示、公布答案、换一题、超时、日上限、
找茬判定、题库加载与覆盖优先级（`docs/design/playhub.md` §5.4、`:1548-1559`）。

不联网、不 sleep、不写仓库外路径：题库与数据库全部落在 `TemporaryDirectory` 里。
"""
from __future__ import annotations

import hashlib
import json
import random
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from plugins.playhub.app import App
from plugins.playhub.content import loader
from plugins.playhub.core import Store, balance
from plugins.playhub.core.clock import CN
from plugins.playhub.core.errors import WalletUninitialized
from plugins.playhub.engines.quiz import (
    COIN_CORRECT,
    COIN_CORRECT_HINTED,
    WIN_CAP_PER_DAY,
    QuizEngine,
)
from wxbot_framework import game_slot

ROOM = "12345678@chatroom"
UID = "wxid_player_a"
UID_B = "wxid_player_b"
NICK = "小明"


class FakeMsg:
    """最小 Message 替身：只带引擎真正读的字段。"""

    def __init__(self, text="", *, chat_id=ROOM, sender_id=UID, nickname=NICK, is_self=False):
        self.text = text
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.is_self = is_self
        self.data = {"sender_wechat_name": nickname}


class FakeClock:
    """秒级可控时钟（北京时区 2026-11-01 12:00 起）。"""

    def __init__(self) -> None:
        self.ts = int(
            datetime.fromisoformat("2026-11-01T12:00:00").replace(tzinfo=CN).timestamp()
        )

    def __call__(self) -> int:
        return self.ts

    def advance(self, seconds: int = 1) -> None:
        self.ts += seconds


def _quiz_rows(count: int, *, prefix: str = "题面", category: str = "song") -> list[dict]:
    return [
        {
            "id": f"{category}-{i:04d}",
            "v": 1,
            "q": f"{prefix}{i}：一个用来测试的题目描述",
            "a": [f"答案{i}", f"别名{i}"],
            "hint": [f"提示{i}之一", f"提示{i}之二"],
            "d": 1,
            "tag": ["测试"],
            "src": "test",
        }
        for i in range(1, count + 1)
    ]


def _diff_rows(count: int, *, prefix: str = "版本") -> list[dict]:
    return [
        {
            "id": f"diff-{i:04d}",
            "v": 1,
            "a": f"这是第{i}句原话，{prefix}甲",
            "b": f"这是第{i}句原话，{prefix}乙",
            "d": 1,
        }
        for i in range(1, count + 1)
    ]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
    )


class QuizTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.content = self.tmp / "content"          # = <db_path.parent>/content 覆盖层
        _write_jsonl(self.content / "song.jsonl", _quiz_rows(30))
        _write_jsonl(self.content / "movie.jsonl", _quiz_rows(30, prefix="电影题面", category="movie"))
        _write_jsonl(self.content / "diff.jsonl", _diff_rows(30))

        self.store = Store(self.tmp / "playhub.sqlite3", account="acct")
        self.store.migrate()
        self.clock = FakeClock()
        self.app = App(
            store=self.store,
            rng=random.Random(20261105),
            now_fn=self.clock,
            repo_root=self.tmp,                      # 仓库默认层指向临时根，避免读到真题库
        )
        loader.clear_cache()

    def tearDown(self) -> None:
        loader.clear_cache()
        self.store.close()
        self._tmp.cleanup()

    # -- 辅助 ---------------------------------------------------------------
    def say(self, text: str, *, uid: str = UID, nickname: str = NICK):
        msg = FakeMsg(text, sender_id=uid, nickname=nickname)
        return self.app.handle_message(None, msg)

    def open_row(self):
        with self.store.tx(immediate=False) as conn:
            return conn.execute(
                "SELECT id, category, question_id, answer_norm, hint_level, expires_at, attempts, closed_at "
                "FROM quiz_round WHERE room = ? AND closed_at IS NULL",
                (ROOM,),
            ).fetchone()

    def coins(self, uid: str = UID) -> int:
        """余额；钱包行还没建（没发过任何币）按 0 算。"""
        with self.store.tx(immediate=False) as conn:
            try:
                return balance(conn, room=ROOM, uid=uid, currency="coin")
            except WalletUninitialized:
                return 0

    def attempts(self) -> int:
        with self.store.tx(immediate=False) as conn:
            return conn.execute("SELECT COUNT(*) FROM quiz_attempt").fetchone()[0]

    def answer_of(self, question_id: str) -> str:
        """从临时题库里按 id 取首个答案（题目都是本测试生成的）。"""
        banks = _quiz_rows(30) + _quiz_rows(30, prefix="电影题面", category="movie")
        for row in banks:
            if row["id"] == question_id:
                return row["a"][0]
        raise AssertionError(f"未知题目 {question_id}")


class StartTests(QuizTestBase):
    def test_start_creates_round_and_hides_answer(self) -> None:
        reply = self.say("猜歌名")
        self.assertIsNotNone(reply)
        self.assertIn("=====🎵猜歌名🎵=====", reply.text)

        row = self.open_row()
        self.assertIsNotNone(row)
        self.assertEqual(row["category"], "song")

        # answer_norm 只存 sha256 数组，不含任何答案明文
        stored = json.loads(row["answer_norm"])
        self.assertTrue(all(len(h) == 64 for h in stored))
        answer = self.answer_of(row["question_id"])
        self.assertNotIn(answer, row["answer_norm"])
        self.assertIn(hashlib.sha256(answer.encode()).hexdigest(), stored)

    def test_second_start_closes_previous_round(self) -> None:
        """开局不阻塞：新局开起来，上一局就地结束并在回包说明。"""
        self.say("猜歌名")
        first = self.open_row()
        reply = self.say("猜电影")
        self.assertIsNotNone(reply)
        self.assertIn("上一局（猜歌名）没答完，已结束", reply.text)
        self.assertIn("=====🎬猜电影🎬=====", reply.text)

        with self.store.tx(immediate=False) as conn:
            closed = conn.execute(
                "SELECT closed_at FROM quiz_round WHERE id = ?", (first["id"],)
            ).fetchone()[0]
        self.assertIsNotNone(closed)
        self.assertEqual(self.open_row()["category"], "movie")

    def test_missing_bank_keeps_current_round(self) -> None:
        """题库没了不该顺手把正在进行的题关掉。"""
        self.say("猜歌名")
        first = self.open_row()
        reply = self.say("猜国漫")                 # song/diff 之外的类目没题库
        self.assertIn("题库还没准备好", reply.text)
        self.assertEqual(self.open_row()["id"], first["id"])

    def test_missing_bank_is_reported(self) -> None:
        reply = self.say("猜国漫")          # 只有 song/diff 两个题库
        self.assertIsNotNone(reply)
        self.assertIn("题库还没准备好", reply.text)

    def test_expired_round_is_swept_on_next_start(self) -> None:
        first = self.say("猜歌名")
        self.assertIsNotNone(first)
        self.clock.advance(301)
        second = self.say("猜歌名")
        self.assertIsNotNone(second)
        self.assertIn("=====🎵猜歌名🎵=====", second.text)

        with self.store.tx(immediate=False) as conn:
            closed = conn.execute(
                "SELECT COUNT(*) FROM quiz_round WHERE room = ? AND closed_at IS NOT NULL",
                (ROOM,),
            ).fetchone()[0]
        self.assertEqual(closed, 1)
        self.assertIsNotNone(self.open_row())


class AnswerTests(QuizTestBase):
    def test_correct_answer_pays_coins_and_closes(self) -> None:
        self.say("猜歌名")
        row = self.open_row()
        self.clock.advance()
        reply = self.say(self.answer_of(row["question_id"]))

        self.assertIsNotNone(reply)
        self.assertIn("答对了", reply.text)
        self.assertIn(f"├ 金币：+{COIN_CORRECT}", reply.text)
        self.assertIsNone(self.open_row())
        self.assertEqual(self.coins(), COIN_CORRECT)

        with self.store.tx(immediate=False) as conn:
            winner = conn.execute("SELECT winner_uid FROM quiz_round WHERE id = ?", (row["id"],)).fetchone()[0]
        self.assertEqual(winner, UID)

    def test_answer_is_normalized(self) -> None:
        self.say("猜歌名")
        row = self.open_row()
        answer = self.answer_of(row["question_id"])
        # 全角空格 + 句号 + 首尾空白都不该影响判定
        reply = self.say(f"  {answer}。 ")
        self.assertIsNotNone(reply)
        self.assertIn("答对了", reply.text)

    def test_wrong_answer_is_silent(self) -> None:
        self.say("猜歌名")
        reply = self.say("这肯定不是答案")
        self.assertIsNone(reply)
        self.assertEqual(self.attempts(), 1)
        self.assertEqual(self.coins(), 0)
        row = self.open_row()
        self.assertEqual(row["attempts"], 1)

    def test_keyword_wins_over_answer(self) -> None:
        """对局进行中发关键词，必须走签到、不能被当成错误答案（§2.6 第 6 条）。"""
        self.say("猜歌名")
        reply = self.say("签到")
        self.assertIsNotNone(reply)
        self.assertIn("签到", reply.text)
        self.assertEqual(self.attempts(), 0)
        self.assertEqual(self.open_row()["attempts"], 0)

    def test_hint_reduces_reward(self) -> None:
        self.say("猜歌名")
        row = self.open_row()
        self.say("提示")
        self.clock.advance()
        reply = self.say(self.answer_of(row["question_id"]))
        self.assertIn(f"├ 金币：+{COIN_CORRECT_HINTED}", reply.text)
        self.assertEqual(self.coins(), COIN_CORRECT_HINTED)

    def test_daily_win_cap(self) -> None:
        for _ in range(WIN_CAP_PER_DAY):
            self.assertIsNotNone(self.say("猜歌名"))
            row = self.open_row()
            self.clock.advance()
            self.assertIsNotNone(self.say(self.answer_of(row["question_id"])))
        self.assertEqual(self.coins(), COIN_CORRECT * WIN_CAP_PER_DAY)

        self.assertIsNotNone(self.say("猜歌名"))
        row = self.open_row()
        self.clock.advance()
        reply = self.say(self.answer_of(row["question_id"]))
        self.assertIn("已领满", reply.text)
        self.assertEqual(self.coins(), COIN_CORRECT * WIN_CAP_PER_DAY)


class SubcommandTests(QuizTestBase):
    def test_subcommands_silent_without_round(self) -> None:
        for word in ("提示", "换一题", "公布答案", "结束", "退出猜成语"):
            self.assertIsNone(self.say(word), word)

    def test_end_word_closes_whatever_is_running(self) -> None:
        """只有一个结束词「结束」，不按类目；回包要说清结束的是哪一局。"""
        self.say("猜电影")
        reply = self.say("结束")
        self.assertIn("本局（猜电影）已结束", reply.text)
        self.assertIsNone(self.open_row())

        self.say("猜歌名")
        reply = self.say("结束")
        self.assertIn("本局（猜歌名）已结束", reply.text)
        self.assertIsNone(self.open_row())

    def test_other_exit_words_do_nothing(self) -> None:
        """历史写法 `退出猜电影` 之类不再生效，避免「退出甲却退了乙」。"""
        self.say("猜歌名")
        self.assertIsNone(self.say("退出猜电影"))
        self.assertIsNotNone(self.open_row())

    def test_end_word_works_for_diff(self) -> None:
        self.say("找茬")
        reply = self.say("结束")
        self.assertIn("本局（文字找茬）已结束", reply.text)
        self.assertIsNone(self.open_row())

    def test_hint_levels_then_reveal(self) -> None:
        self.say("猜歌名")
        first = self.say("提示")
        self.assertIn("提示 1/2", first.text)
        second = self.say("提示")
        self.assertIn("提示 2/2", second.text)
        third = self.say("提示")
        self.assertIn("提示已经给完", third.text)

        reveal = self.say("公布答案")
        self.assertIn("答案是", reveal.text)
        self.assertIsNone(self.open_row())
        self.assertEqual(self.coins(), 0)

    def test_next_question_replaces_round(self) -> None:
        self.say("猜歌名")
        before = self.open_row()["question_id"]
        reply = self.say("换一题")
        self.assertIn("换一题", reply.text)
        after = self.open_row()
        self.assertIsNotNone(after)
        self.assertNotEqual(after["question_id"], before)

    def test_quit_closes_round(self) -> None:
        self.say("猜歌名")
        reply = self.say("结束")
        self.assertIn("本局（猜歌名）已结束", reply.text)
        self.assertIsNone(self.open_row())
        self.assertEqual(self.coins(), 0)


class DiffTests(QuizTestBase):
    def test_diff_accepts_changed_word(self) -> None:
        reply = self.say("找茬")
        self.assertIn("=====🔍文字找茬🔍=====", reply.text)
        row = self.open_row()
        self.clock.advance()
        hit = self.say("乙")
        self.assertIsNotNone(hit)
        self.assertIn("答对了", hit.text)
        self.assertIsNone(self.open_row())

    def test_diff_rejects_unrelated_word(self) -> None:
        self.say("找茬")
        self.assertIsNone(self.say("丙"))


class LoaderTests(QuizTestBase):
    def test_bank_below_threshold_is_unusable(self) -> None:
        _write_jsonl(self.content / "song.jsonl", _quiz_rows(29))
        loader.clear_cache()
        reply = self.say("猜歌名")
        self.assertIn("题库还没准备好", reply.text)

    def test_broken_lines_are_dropped(self) -> None:
        rows = _quiz_rows(30)
        raw = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
        (self.content / "song.jsonl").write_text(raw + "\n{坏行\n", encoding="utf-8")
        loader.clear_cache()
        bank = loader.load_bank(self.store, "song", repo_root=self.tmp)
        self.assertEqual(len(bank.questions), 30)
        self.assertEqual(bank.bad_lines, 1)

    def test_account_override_wins_over_repo_default(self) -> None:
        repo = self.tmp / "repo"
        _write_jsonl(repo / "content" / "playhub" / "song.jsonl", _quiz_rows(30, prefix="仓库"))

        # 没有账号覆盖层（db 在另一个空目录里）→ 落到仓库默认层
        bare = Store(self.tmp / "bare" / "playhub.sqlite3", account="acct2")
        bare.migrate()
        try:
            loader.clear_cache()
            bank = loader.load_bank(bare, "song", repo_root=repo)
            self.assertTrue(all("仓库" in q.q for q in bank.questions), bank.questions[0].q)
        finally:
            bare.close()

        # 有账号覆盖层（setUp 已写到 <db.parent>/content）→ 覆盖层优先
        loader.clear_cache()
        bank = loader.load_bank(self.store, "song", repo_root=repo)
        self.assertTrue(all("题面" in q.q for q in bank.questions), bank.questions[0].q)

    def test_cache_reuses_and_rechecks(self) -> None:
        clock = [0.0]
        first = loader.load_bank(self.store, "song", repo_root=self.tmp, now_fn=lambda: clock[0])
        clock[0] += 1
        second = loader.load_bank(self.store, "song", repo_root=self.tmp, now_fn=lambda: clock[0])
        self.assertIs(first, second)              # 60 s 内直接命中缓存

        clock[0] += loader.RECHECK_SECONDS + 1
        third = loader.load_bank(self.store, "song", repo_root=self.tmp, now_fn=lambda: clock[0])
        self.assertIsNot(third, first)            # 过期后重建（文件未变也要复核）
        self.assertEqual(len(third.questions), len(first.questions))


class EngineUnitTests(unittest.TestCase):
    def test_category_map_covers_all_quiz_actions(self) -> None:
        from plugins.playhub.keywords import TRIGGERS
        from plugins.playhub.engines.quiz import CATEGORY_BY_ACTION

        quiz_actions = {t.action for t in TRIGGERS if t.action.startswith("quiz.")}
        self.assertEqual(quiz_actions, set(CATEGORY_BY_ACTION))
        self.assertEqual(len(quiz_actions), 8)

    def test_engine_name_matches_bindings(self) -> None:
        from plugins.playhub.registry import BINDINGS

        self.assertEqual(QuizEngine.name, "quiz")
        for action, binding in BINDINGS.items():
            if action.startswith("quiz."):
                self.assertEqual(binding.engine, "quiz")


class SlotTests(QuizTestBase):
    """共享游戏槽：一个群同一时刻只允许一局，跨插件也一样（game_slot.py）。"""

    def test_other_game_holding_the_slot_blocks_start(self) -> None:
        game_slot.claim(ROOM, "idiom_chain", label="成语接龙")
        reply = self.say("猜歌名")
        self.assertIn("本群还有一局成语接龙没结束", reply.text)
        self.assertIsNone(self.open_row())

    def test_start_takes_the_slot_with_its_label(self) -> None:
        self.say("猜电影")
        self.assertEqual(game_slot.holder(ROOM), "猜电影")
        self.assertEqual(game_slot.holder_owner(ROOM), "playhub")

    def test_end_releases_the_slot(self) -> None:
        self.say("猜歌名")
        self.say("结束")
        self.assertIsNone(game_slot.holder(ROOM))

    def test_reveal_and_correct_answer_release_the_slot(self) -> None:
        self.say("猜歌名")
        self.say("公布答案")
        self.assertIsNone(game_slot.holder(ROOM))

        self.say("猜歌名")
        row = self.open_row()
        self.clock.advance()
        self.say(self.answer_of(row["question_id"]))
        self.assertIsNone(game_slot.holder(ROOM))

    def test_next_question_keeps_the_slot(self) -> None:
        self.say("猜歌名")
        self.say("换一题")
        self.assertEqual(game_slot.holder(ROOM), "猜歌名")

    def test_sweep_releases_the_slot(self) -> None:
        from plugins.playhub.engines.quiz import ROUND_SECONDS

        self.say("猜歌名")
        self.clock.advance(ROUND_SECONDS + 1)
        with self.store.tx() as conn:
            self.assertEqual(self.app._engines["quiz"].sweep(conn, now=self.clock()), 1)
        self.assertIsNone(game_slot.holder(ROOM))


if __name__ == "__main__":
    unittest.main()
