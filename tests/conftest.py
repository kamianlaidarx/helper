"""全局测试夹具。

`wxbot_framework/game_slot.py` 是进程级状态：一个群同一时刻只允许一局游戏。
测试用例会复用同一个群号，若不在用例之间清空，前一个用例占着的槽位会让后一个
用例的「开局」被拒绝（表现为莫名其妙的「本群还有一局…没结束」）。
"""
import pytest

from wxbot_framework import game_slot


@pytest.fixture(autouse=True)
def _reset_game_slot():
    game_slot.clear()
    yield
    game_slot.clear()
