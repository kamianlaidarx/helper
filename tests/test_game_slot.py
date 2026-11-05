"""`wxbot_framework/game_slot.py` 单测：一个群同一时刻只允许一局游戏。

槽位是进程内状态，测试之间由 `tests/conftest.py` 的自动夹具清空；这里也在 setUp
里显式清一次，便于单独运行本文件。
"""
from __future__ import annotations

import unittest

from wxbot_framework import game_slot

ROOM = "12345678@chatroom"
OTHER = "87654321@chatroom"


class ClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        game_slot.clear()

    def tearDown(self) -> None:
        game_slot.clear()

    def test_first_claim_wins_and_reports_holder(self) -> None:
        self.assertIsNone(game_slot.claim(ROOM, "idiom_chain", label="成语接龙"))
        self.assertIsNone(game_slot.claim(OTHER, "playhub", label="猜成语"))
        self.assertEqual(game_slot.holder(ROOM), "成语接龙")
        self.assertEqual(game_slot.holder(OTHER), "猜成语")

    def test_second_owner_is_refused_with_holder_label(self) -> None:
        game_slot.claim(ROOM, "idiom_chain", label="成语接龙")
        self.assertEqual(game_slot.claim(ROOM, "playhub", label="猜成语"), "成语接龙")
        self.assertEqual(game_slot.holder_owner(ROOM), "idiom_chain")

    def test_same_owner_reclaims_and_refreshes_ttl(self) -> None:
        self.assertIsNone(game_slot.claim(ROOM, "playhub", label="猜成语", ttl=10, now=0.0))
        self.assertIsNone(game_slot.claim(ROOM, "playhub", label="文字找茬", ttl=10, now=5.0))
        self.assertEqual(game_slot.holder(ROOM, now=14.0), "文字找茬")
        self.assertIsNone(game_slot.holder(ROOM, now=16.0))   # 从 5.0 起算 10 秒

    def test_release_only_by_owner(self) -> None:
        game_slot.claim(ROOM, "idiom_chain", label="成语接龙")
        self.assertFalse(game_slot.release(ROOM, "playhub"))
        self.assertEqual(game_slot.holder(ROOM), "成语接龙")
        self.assertTrue(game_slot.release(ROOM, "idiom_chain"))
        self.assertIsNone(game_slot.holder(ROOM))
        self.assertFalse(game_slot.release(ROOM, "idiom_chain"))   # 已经是空槽

    def test_release_without_owner_forces(self) -> None:
        game_slot.claim(ROOM, "picture_idiom", label="看图猜成语")
        self.assertTrue(game_slot.release(ROOM))
        self.assertIsNone(game_slot.holder_owner(ROOM))

    def test_expired_slot_is_free_again(self) -> None:
        game_slot.claim(ROOM, "playhub", label="猜成语", ttl=300, now=100.0)
        self.assertEqual(game_slot.holder(ROOM, now=399.0), "猜成语")
        self.assertIsNone(game_slot.holder(ROOM, now=400.0))
        self.assertIsNone(game_slot.holder_owner(ROOM, now=401.0))
        self.assertIsNone(game_slot.claim(ROOM, "idiom_chain", label="成语接龙", now=400.0))

    def test_rooms_are_independent(self) -> None:
        game_slot.claim(ROOM, "idiom_chain", label="成语接龙")
        self.assertIsNone(game_slot.claim(OTHER, "playhub", label="猜成语"))
        self.assertEqual(game_slot.holder(ROOM), "成语接龙")
        self.assertEqual(game_slot.holder(OTHER), "猜成语")

    def test_empty_arguments_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            game_slot.claim("", "playhub")
        with self.assertRaises(ValueError):
            game_slot.claim(ROOM, "")

    def test_label_defaults_to_owner(self) -> None:
        game_slot.claim(ROOM, "playhub")
        self.assertEqual(game_slot.holder(ROOM), "playhub")


if __name__ == "__main__":
    unittest.main()
