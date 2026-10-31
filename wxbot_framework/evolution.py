"""成长层：把这个聊天里学到的东西，压成一小块可覆盖的自我提醒。

三层隔离的最外层——只有这里会被自动改写：
- GENOME 在 personality.SPECTRUM / PROMPTS，是代码常量，本模块只读不写；
- SOUL 在 mask.MASK_DATA，同样是常量；
- GROWTH 住在 memory.load(chat_id) 的 "growth" 分区，覆盖写而不是追加。

覆盖写是刻意的：追加式成长迟早把 system 撑爆。每轮都重写一遍，
硬上限 GROWTH_MAX_CHARS，超了按整句截断；上一版留在 prev 里可回滚。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from . import memory

GROWTH_MAX_CHARS = 600
GROWTH_INTERVAL_HOURS = 24.0
GROWTH_MIN_MATERIAL = 5
SAMPLE_FACTS = 12
SAMPLE_PEOPLE = 8

SYSTEM = """你在替一个长期待在同一个聊天里的角色写「日结」：她这段时间在那儿是什么样子。

只根据给定的事实写，不要编造没有出现过的经历。写成她自己的口吻的自我提醒，
不写台词、不写例子对话、不称呼自己为机器人。

输出 JSON：{"growth":"不超过 300 字的连续中文"}。内容包括且只包括：
1. 在这个聊天里说话该长还是短、要不要留白；
2. 哪类话题上更放松，哪类话题上得更收着；
3. 这群人之间独有的说法、称呼或梗（没有就不写）。
不要重复给出的事实的原文，不要写「我是某某」这类介绍。"""


def empty():
    return {"text": "", "v": 0, "updated_at": "", "prev": ""}


def _state(state):
    growth = state.get("growth") if isinstance(state, dict) else None
    if not isinstance(growth, dict) or not isinstance(growth.get("text"), str):
        growth = empty()
        if isinstance(state, dict):
            state["growth"] = growth
    return growth


def _age_hours(growth, now):
    try:
        moment = datetime.fromisoformat(growth.get("updated_at") or "")
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (now - moment).total_seconds() / 3600.0


def due(state, *, now=None):
    """够不够时间、够不够料。没料就不跑，免得空转烧调用。"""
    now = now or datetime.now(timezone.utc)
    growth = _state(state)
    if len(state.get("facts") or []) < GROWTH_MIN_MATERIAL:
        return False
    age = _age_hours(growth, now)
    return age is None or age >= GROWTH_INTERVAL_HOURS


def _people_lines(state):
    people = state.get("people") or {}
    rows = sorted(people.values(), key=lambda item: float(item.get("affinity") or 0.0), reverse=True)
    lines = []
    for person in rows[:SAMPLE_PEOPLE]:
        count = int(person.get("interactions") or 0)
        if not count:
            continue
        value = float(person.get("affinity") or 0.0)
        level = "很熟" if value >= 0.6 else ("有点熟" if value >= 0.35 else "刚认识")
        lines.append("- %s（%s，聊过 %d 次）" % (person.get("name") or "?", level, count))
    return lines


def _fact_lines(state):
    facts = list(state.get("facts") or [])
    facts.sort(key=lambda fact: (int(fact.get("importance") or 0), fact.get("last_seen") or ""), reverse=True)
    lines = []
    for fact in facts[:SAMPLE_FACTS]:
        text = (fact.get("text") or "").strip()
        if text:
            lines.append("- " + text)
    return lines


def build_user(state):
    """把事实和人物关系整理成给模型看的一段材料。"""
    people = _people_lines(state)
    facts = _fact_lines(state)
    blocks = []
    if people:
        blocks.append("常出现的人：\n" + "\n".join(people))
    if facts:
        blocks.append("已经记住的事：\n" + "\n".join(facts))
    growth = _state(state)
    if growth.get("text"):
        blocks.append("上一版日结（可以修，不要整段照抄）：\n" + growth["text"])
    return "\n\n".join(blocks)


def apply(state, text, *, now=None):
    """覆盖写入新的一版；旧的一版留在 prev，随时能回滚。"""
    now = now or datetime.now(timezone.utc)
    growth = _state(state)
    body = " ".join((text or "").split()).strip()
    if not body:
        return growth
    if len(body) > GROWTH_MAX_CHARS:
        cut = body.rfind("。", 0, GROWTH_MAX_CHARS)
        body = body[:cut + 1] if cut > 0 else body[:GROWTH_MAX_CHARS]
    growth["prev"] = growth.get("text") or ""
    growth["text"] = body
    growth["v"] = int(growth.get("v") or 0) + 1
    growth["updated_at"] = now.isoformat(timespec="seconds")
    return growth


def rollback(state):
    growth = _state(state)
    if not growth.get("prev"):
        return False
    growth["text"], growth["prev"] = growth["prev"], growth.get("text") or ""
    growth["v"] = max(0, int(growth.get("v") or 0) - 1)
    return True


def note(state):
    """拼进 system 的成长块；没有就返回空串，不占额度。"""
    growth = _state(state)
    body = (growth.get("text") or "").strip()
    if not body:
        return ""
    return ("\n【你在这个聊天里慢慢长成的样子】这些是你自己琢磨出来的，不是别人给的设定，"
            "按它说话，但不要念出来，也不要说「我记得我说过」。\n" + body)


def consolidate(client, chat_id, *, now=None):
    """给一个聊天跑一次日结；返回是否真的更新了。失败一律吞掉。"""
    now = now or datetime.now(timezone.utc)
    try:
        state = memory.load(chat_id)
        if not due(state, now=now):
            return False
        user = build_user(state)
        if not user.strip():
            return False
        result = client.complete_json(SYSTEM, user)
        body = result.get("growth") if isinstance(result, dict) else None
        if not isinstance(body, str) or not body.strip():
            return False
        apply(state, body, now=now)
        memory.save(chat_id, state)
        return True
    except Exception:
        return False


def _chats():
    """枚举记忆目录里的真实 chat_id；目录名是 _safe 过的，不能直接当 ID 用。"""
    root = memory.STATE_ROOT
    if not root.is_dir():
        return []
    found = []
    for directory in sorted(root.iterdir()):
        path = directory / "memory.json"
        if not path.is_file():
            continue
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        chat_id = state.get("chat_id") if isinstance(state, dict) else None
        if isinstance(chat_id, str) and chat_id:
            found.append(chat_id)
    return found


def sweep(client, *, now=None):
    """扫一遍所有聊天，把到期的跑掉；返回真正更新的个数。"""
    now = now or datetime.now(timezone.utc)
    return sum(1 for chat_id in _chats() if consolidate(client, chat_id, now=now))
