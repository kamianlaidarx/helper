"""拟人层：让发出去的东西像一个真人在打字，而不是一条接口响应。

七类破绽与对策都在这里：
1. 节奏恒定 → typing_delay：按字数给时间，再乘一个对数正态抖动
2. 有问必答 → should_reply：被艾特也不一定每次都回
3. 一条说完 → split_bubbles：长回复按句切成一到三条，中间有停顿
4. 排版洁癖 → plain_speech：去掉列表符号和加粗标记
5. 永不犯错 → maybe_typo：只在熟人群闲聊里，低频打错一个字再自己改口
6. 永远在线 → should_reply 的深夜抑制
7. 对所有人一个语气 → reply_probability 按身份分档

全部是纯函数，随机源由调用方注入（random.Random），测试用固定种子。
"""

from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass
from datetime import datetime

TYPING_PER_CHAR_S = 0.18
TYPING_MIN_S = 1.2
TYPING_MAX_S = 12.0
TYPING_SIGMA = 0.6

OWNER_REPLY = 0.95
PEER_REPLY = 0.75
QUIET_HOURS = (0, 7)
QUIET_FACTOR = 0.2

BUBBLE_MAX_CHARS = 42
BUBBLE_LIMIT = 3
BUBBLE_GAP_S = 0.8
# 第一条按真实打字时间等；后面的气泡只做"还在打"的停顿，不能把消息队列堵死。
BUBBLE_MAX_DELAY_S = 2.5

TYPO_RATE = 0.03
TYPO_MIN_CHARS = 6
TYPO_POOL = "的了是在和就不都也很到说要去会着看好自己这那么什么怎样"

_WORKISH = ("报错", "日志", "代码", "函数", "接口", "编译", "部署", "修一下", "怎么改",
            "为什么", "原理", "配置", "报错信息", "栈", "异常", "帮我看看", "排查")
_LIST_MARK = re.compile(r"^\s*(?:[-*•·]|\d+[.、)]|[（(]\d+[)）])\s*", re.M)
_EMPHASIS = ("**", "__", "~~")
_SENTENCE_END = re.compile(r"(?<=[。！？!?…；;~])\s*")


@dataclass(frozen=True)
class Bubble:
    """一条要发出去的气泡：内容，以及发它之前该等多久。"""
    text: str
    delay: float


def reply_probability(*, is_owner, is_at_self):
    if is_at_self:
        return OWNER_REPLY if is_owner else PEER_REPLY
    # 没被艾特时只是跟着聊两句，绝大多数时候不插话。
    return 0.08 if is_owner else 0.03


def _quiet(hour):
    return 0 <= hour < 24 and QUIET_HOURS[0] <= hour < QUIET_HOURS[1]


def should_reply(*, is_owner, is_at_self, hour=None, rng=None):
    rng = rng or random
    hour = datetime.now().hour if hour is None else hour
    chance = reply_probability(is_owner=is_owner, is_at_self=is_at_self)
    if _quiet(hour):
        chance *= QUIET_FACTOR
    return rng.random() < chance


def typing_delay(text, rng=None):
    """按字数估一个打字时间，再乘对数正态抖动——真人不会每条都同样快。"""
    rng = rng or random
    base = TYPING_PER_CHAR_S * max(1, len(text or ""))
    return max(TYPING_MIN_S, min(TYPING_MAX_S, base * math.exp(rng.gauss(0.0, TYPING_SIGMA))))


def plain_speech(text):
    """去掉列表符号和加粗标记：微信里没人这么说话，机器人味就是这么来的。"""
    body = _LIST_MARK.sub("", text or "")
    for mark in _EMPHASIS:
        body = body.replace(mark, "")
    return body.strip()


def is_workish(text):
    """是不是在找她干活——干活的时候不打错字，也不玩欲言又止。"""
    body = text or ""
    return any(word in body for word in _WORKISH) or "```" in body


def split_bubbles(text):
    """把一段话按句子聚成最多三条气泡；句子太碎时合并，整段太长就整条保留。"""
    body = (text or "").strip()
    if not body:
        return []
    if len(body) <= BUBBLE_MAX_CHARS:
        return [body]
    pieces = [piece.strip() for piece in _SENTENCE_END.split(body) if piece.strip()]
    if len(pieces) <= 1:
        return [body]
    bubbles, current = [], ""
    for piece in pieces:
        if current and len(current) + len(piece) > BUBBLE_MAX_CHARS:
            bubbles.append(current)
            current = piece
        else:
            current += piece
    if current:
        bubbles.append(current)
    if len(bubbles) > BUBBLE_LIMIT:
        # 超过三条就把后面的并进最后一条，别把一句话讲成一串气泡。
        bubbles = bubbles[:BUBBLE_LIMIT - 1] + ["".join(bubbles[BUBBLE_LIMIT - 1:])]
    return bubbles


def maybe_typo(text, *, is_owner, rng=None):
    """低频打错一个字，返回 (发出去的错句, 用来圆回来的那句)；不满足条件返回 None。"""
    rng = rng or random
    body = (text or "").strip()
    if is_owner or len(body) < TYPO_MIN_CHARS or is_workish(body):
        return None
    if rng.random() >= TYPO_RATE:
        return None
    index = rng.randrange(len(body))
    wrong = body[index]
    if wrong not in TYPO_POOL:
        return None
    replacement = rng.choice([char for char in TYPO_POOL if char != wrong])
    return body[:index] + replacement + body[index + 1:], "…刚打错了，是「%s」那句。" % wrong


def plan(text, *, is_owner, is_at_self, hour=None, rng=None):
    """把最终回复排成要发的气泡序列；返回空列表表示这次不回。"""
    rng = rng or random
    if not should_reply(is_owner=is_owner, is_at_self=is_at_self, hour=hour, rng=rng):
        return []
    body = plain_speech(text)
    if not body:
        return []
    typo = maybe_typo(body, is_owner=is_owner, rng=rng)
    if typo is not None:
        wrong, correction = typo
        return [Bubble(wrong, _delay_for(wrong, 0, rng)),
                Bubble(correction, _delay_for(correction, 1, rng))]
    bubbles = split_bubbles(body)
    return [Bubble(item, _delay_for(item, index, rng)) for index, item in enumerate(bubbles)]


def _delay_for(text, index, rng):
    delay = typing_delay(text, rng)
    if index == 0:
        return delay
    return min(delay, BUBBLE_MAX_DELAY_S) + BUBBLE_GAP_S
