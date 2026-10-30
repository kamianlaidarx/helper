"""群聊长期记忆：结构化事实的存储、合并、检索与注入文本渲染。

设计取舍：
- 只用标准库，单群一个 JSON，坏了直接删，不影响机器人启动。
- 检索不依赖向量：查询词与事实文本做字符/词集合重合，叠加时间衰减和重要性。
- 合并按 (subject, predicate, object) 归一化后覆盖，同一件事不会反复追加。
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

STATE_ROOT = Path(__file__).resolve().parent.parent / "state"

RECENCY_HALF_LIFE_HOURS = 72.0
WEIGHT_RECENCY = 0.5
WEIGHT_IMPORTANCE = 3.0
WEIGHT_RELEVANCE = 2.0
RECALL_LIMIT = 8
MAX_FACTS = 400

TEMP_BURNING = "burning"
TEMP_WARM = "warm"
TEMP_COLD = "cold"
TEMP_WEIGHT = {TEMP_BURNING: 0.4, TEMP_WARM: 0.0, TEMP_COLD: -0.6}
TEMP_ORDER = (TEMP_COLD, TEMP_WARM, TEMP_BURNING)
BURNING_TO_WARM_DAYS = 14
WARM_TO_COLD_DAYS = 30
HOT_IMPORTANCE = 8

AFFINITY_START = 0.1
AFFINITY_STEP = 0.02
AFFINITY_DIMINISH = 0.95
AFFINITY_MAX = 1.0
AFFINITY_MIN = 0.0
AFFINITY_DECAY_AFTER_DAYS = 7
AFFINITY_DECAY_PER_DAY = 0.01
AFFINITY_NOTE_MIN_INTERACTIONS = 3

EMOTION_STEP = 0.25
EMOTION_FADE = 0.1
EMOTION_NOTE_MIN = 0.3
EMOTION_KEYWORDS = {
    "annoyed": ("烦", "滚", "闭嘴", "傻", "笨", "垃圾", "无语", "别说了"),
    "happy": ("哈哈", "笑死", "厉害", "好棒", "喜欢", "谢谢", "牛", "赞"),
}
EMOTION_LABELS = {"annoyed": "有点烦", "happy": "心情不错", "neutral": "平静"}

_TOKEN = re.compile(r"[0-9A-Za-z_]+|[\u4e00-\u9fff]")
_STOP = frozenset(
    "的 了 是 在 我 你 他 她 它 们 有 和 就 都 也 这 那 什么 怎么 吗 呢 吧 啊 呀 哦 嗯".split()
)


def _tokens(text):
    return {token for token in _TOKEN.findall(text or "") if token not in _STOP}


def _norm(value):
    return re.sub(r"\s+", "", str(value or "").lower())


def _safe(value):
    return re.sub(r"[^0-9A-Za-z_.-]", "_", str(value or "")) or "_"


def _key(fact):
    return (_norm(fact.get("subject")), _norm(fact.get("predicate")), _norm(fact.get("object")))


def _clamp_importance(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 5
    return max(1, min(10, number))


def state_path(chat_id):
    return STATE_ROOT / _safe(chat_id) / "memory.json"


def empty_state(chat_id):
    return {"chat_id": chat_id, "updated_at": "", "facts": []}


def load(chat_id):
    """读群记忆；文件缺失或损坏一律当作空记忆，不让记忆层阻断对话。"""
    path = state_path(chat_id)
    try:
        with path.open("r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return empty_state(chat_id)
    if not isinstance(state, dict) or not isinstance(state.get("facts"), list):
        return empty_state(chat_id)
    state["chat_id"] = chat_id
    state.setdefault("updated_at", "")
    return state


def save(chat_id, state):
    path = state_path(chat_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    state["chat_id"] = chat_id
    state["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    descriptor, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _find_existing(index, candidate):
    """精确命中优先；predicate 有一侧为空时宽松匹配，免得同一件事被存成两条。"""
    exact = index.get(_key(candidate))
    if exact is not None:
        return exact
    subject = _norm(candidate.get("subject"))
    obj = _norm(candidate.get("object"))
    for fact in index.values():
        if _norm(fact.get("subject")) != subject or _norm(fact.get("object")) != obj:
            continue
        if not fact.get("predicate") or not candidate.get("predicate"):
            return fact
    return None


def merge_facts(state, incoming):
    """把新抽取的事实并入 state，返回真正新增的条数。"""
    if not incoming:
        return 0
    index = {_key(fact): fact for fact in state["facts"]}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    added = 0
    for raw in incoming:
        if not isinstance(raw, dict):
            continue
        subject = str(raw.get("subject") or "").strip()
        text = str(raw.get("text") or "").strip()
        if not subject or not text:
            continue
        candidate = {
            "subject": subject,
            "predicate": str(raw.get("predicate") or "").strip(),
            "object": str(raw.get("object") or "").strip(),
            "text": text,
        }
        importance = _clamp_importance(raw.get("importance"))
        existing = _find_existing(index, candidate)
        if existing is None:
            fact = dict(candidate)
            fact.update(
                {
                    "id": "f%d" % (len(state["facts"]) + 1),
                    "importance": importance,
                    "speaker": str(raw.get("speaker") or ""),
                    "created_at": now,
                    "last_seen": now,
                    "hits": 1,
                    "temp": TEMP_BURNING if importance >= HOT_IMPORTANCE else TEMP_WARM,
                }
            )
            state["facts"].append(fact)
            index[_key(candidate)] = fact
            added += 1
            continue
        existing["last_seen"] = now
        existing["hits"] = int(existing.get("hits") or 1) + 1
        existing["importance"] = max(_clamp_importance(existing.get("importance")), importance)
        _heat(existing)
        if len(text) > len(existing.get("text") or ""):
            existing["text"] = text
    _trim(state)
    return added


def _trim(state):
    facts = state["facts"]
    if len(facts) <= MAX_FACTS:
        return
    facts.sort(key=lambda fact: (fact.get("last_seen") or "", int(fact.get("hits") or 0)))
    state["facts"] = facts[len(facts) - MAX_FACTS:]


def _temp_of(fact):
    value = fact.get("temp")
    return value if value in TEMP_WEIGHT else TEMP_WARM


def _heat(fact):
    """再次被提到时升一档：cold → warm → burning。"""
    position = TEMP_ORDER.index(_temp_of(fact))
    fact["temp"] = TEMP_ORDER[min(position + 1, len(TEMP_ORDER) - 1)]


def apply_decay(state):
    """按最后一次被提到的时间把温度降档，返回发生变化的条数。"""
    facts = state.get("facts") or []
    if not facts:
        return 0
    now = datetime.now(timezone.utc)
    changed = 0
    for fact in facts:
        temp = _temp_of(fact)
        if temp == TEMP_COLD:
            continue
        try:
            moment = datetime.fromisoformat(fact.get("last_seen") or "")
        except (TypeError, ValueError):
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        days = max(0.0, (now - moment).total_seconds() / 86400.0)
        if temp == TEMP_BURNING and days >= BURNING_TO_WARM_DAYS:
            fact["temp"] = TEMP_WARM
            changed += 1
        elif temp == TEMP_WARM and days >= WARM_TO_COLD_DAYS:
            fact["temp"] = TEMP_COLD
            changed += 1
    return changed


def _recency(fact):
    try:
        moment = datetime.fromisoformat(fact.get("last_seen") or "")
    except (TypeError, ValueError):
        return 0.1
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    hours = max(0.0, (datetime.now(timezone.utc) - moment).total_seconds() / 3600.0)
    return math.exp(-hours / RECENCY_HALF_LIFE_HOURS)


def _relevance(fact, query_tokens):
    hit = query_tokens & _tokens(fact.get("text") or "")
    if not hit:
        return 0.0
    score = len(hit) / len(query_tokens)
    if query_tokens & _tokens(fact.get("subject") or ""):
        score += 0.5
    return score


def _person(state, sender_id, name=""):
    people = state.setdefault("people", {})
    person = people.get(sender_id)
    if person is None:
        person = {
            "name": name or sender_id,
            "affinity": AFFINITY_START,
            "interactions": 0,
            "last_interaction": "",
        }
        people[sender_id] = person
    elif name:
        person["name"] = name
    return person


def bump_affinity(state, sender_id, name=""):
    """该成员每说一句话调用一次：增量随互动次数递减，刷屏涨不了好感。"""
    if not sender_id:
        return AFFINITY_START
    person = _person(state, sender_id, name)
    count = int(person.get("interactions") or 0)
    delta = AFFINITY_STEP * (AFFINITY_DIMINISH ** count)
    person["affinity"] = min(AFFINITY_MAX, float(person.get("affinity") or AFFINITY_START) + delta)
    person["interactions"] = count + 1
    person["last_interaction"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return person["affinity"]


def apply_affinity_decay(state):
    """超过 AFFINITY_DECAY_AFTER_DAYS 没说话的人，好感按超过的天数回落。"""
    people = state.get("people") or {}
    if not people:
        return 0
    now = datetime.now(timezone.utc)
    changed = 0
    for person in people.values():
        anchor = person.get("last_decay") or person.get("last_interaction") or ""
        try:
            moment = datetime.fromisoformat(anchor)
        except (TypeError, ValueError):
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        idle = max(0.0, (now - moment).total_seconds() / 86400.0)
        if idle <= AFFINITY_DECAY_AFTER_DAYS:
            continue
        current = float(person.get("affinity") or AFFINITY_START)
        lowered = max(AFFINITY_MIN, current - AFFINITY_DECAY_PER_DAY * (idle - AFFINITY_DECAY_AFTER_DAYS))
        if lowered != current:
            person["affinity"] = lowered
            person["last_decay"] = now.isoformat(timespec="seconds")
            changed += 1
    return changed


def _affinity_note(state, speaker):
    """给当前发言人加一行熟悉度提示；聊得太少就不提，免得像在记账。"""
    if not speaker:
        return ""
    for person in (state.get("people") or {}).values():
        if (person.get("name") or "") != speaker:
            continue
        count = int(person.get("interactions") or 0)
        if count < AFFINITY_NOTE_MIN_INTERACTIONS:
            return ""
        value = float(person.get("affinity") or 0.0)
        if value >= 0.6:
            level = "很熟"
        elif value >= 0.35:
            level = "有点熟"
        else:
            level = "刚认识"
        return "- 你和 %s 聊过 %d 次，%s" % (speaker, count, level)
    return ""


def update_emotion(state, text):
    """按群里正在说的话推机器人自己的情绪；纯关键词，不调模型。"""
    emotion = state.setdefault(
        "emotion", {"name": "neutral", "intensity": 0.0, "streak": 0, "updated_at": ""}
    )
    hits = {}
    body = text or ""
    for name, words in EMOTION_KEYWORDS.items():
        score = sum(1 for word in words if word in body)
        if score:
            hits[name] = score
    if not hits:
        emotion["intensity"] = max(0.0, float(emotion.get("intensity") or 0.0) - EMOTION_FADE)
        if emotion["intensity"] <= 0.0:
            emotion["name"] = "neutral"
            emotion["streak"] = 0
            emotion["intensity"] = 0.0
        return emotion
    name = max(hits, key=hits.get)
    if name == emotion.get("name"):
        emotion["streak"] = int(emotion.get("streak") or 0) + 1
        emotion["intensity"] = min(1.0, float(emotion.get("intensity") or 0.0) + EMOTION_STEP)
    else:
        emotion["name"] = name
        emotion["streak"] = 1
        emotion["intensity"] = EMOTION_STEP
    emotion["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return emotion


def emotion_note(state):
    emotion = state.get("emotion") or {}
    name = emotion.get("name")
    if name not in EMOTION_LABELS or name == "neutral":
        return ""
    if float(emotion.get("intensity") or 0.0) < EMOTION_NOTE_MIN:
        return ""
    label = EMOTION_LABELS[name]
    streak = int(emotion.get("streak") or 0)
    if streak >= 3:
        label += "（连着第 %d 次了）" % streak
    return "- 你现在" + label


def recall(state, query, *, speaker="", limit=RECALL_LIMIT):
    """挑出与本次提问最相关的事实，返回可直接拼进 system 的文本；没有则返回空串。"""
    note = _affinity_note(state, speaker)
    mood = emotion_note(state)
    facts = state.get("facts") or []
    query_tokens = _tokens(query or "")
    scored = []
    for fact in facts:
        relevance = _relevance(fact, query_tokens)
        if relevance <= 0:
            continue
        score = (
            WEIGHT_RELEVANCE * relevance
            + WEIGHT_RECENCY * _recency(fact)
            + WEIGHT_IMPORTANCE * _clamp_importance(fact.get("importance")) / 10.0
            + TEMP_WEIGHT[_temp_of(fact)]
        )
        if speaker and speaker in (fact.get("subject") or ""):
            score += 0.5
        scored.append((score, fact))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    lines = [line for line in (mood, note) if line]
    seen = set()
    for _score, fact in scored[:limit]:
        text = (fact.get("text") or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        lines.append("- " + text)
    if not lines:
        return ""
    return "\n（你记得）\n" + "\n".join(lines)
