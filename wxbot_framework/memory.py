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
EMOTION_SMOOTH_NEW = 0.85
EMOTION_SMOOTH_OLD = 0.15
ARC_BONUS_STEP = 0.05
ARC_BONUS_MAX = 0.3
ARC_MIN_STREAK = 3
ARC_HISTORY_LIMIT = 10
ARC_PREVIOUS_LIMIT = 4

# 九类情绪：全部是「她自己的感受」，不是给对方打分。
EMOTION_KEYWORDS = {
    "joy": ("哈哈", "笑死", "太好了", "开心", "高兴", "舒服", "爽", "棒", "赞", "牛", "满意", "顺"),
    "moved": ("谢谢", "感动", "有心", "记得", "辛苦", "靠你了", "麻烦你", "不容易", "挺你"),
    "missing": ("想你", "想念", "好久不见", "回来", "还记得", "当年", "以前那个", "多久没"),
    "worry": ("怎么办", "来不及", "崩了", "挂了", "出问题", "严重", "风险", "担心", "紧急", "炸了"),
    "aggrieved": ("欺负", "不公平", "明明", "凭什么", "加班", "背锅", "被骂", "委屈", "挨批"),
    "excited": ("太强", "绝了", "稳了", "成了", "终于", "冲", "搞起", "开干", "上线了"),
    "tsundere": ("才不是", "讨厌", "哼", "随便你", "谁要", "别管我", "懒得", "不用你管"),
    "focus": ("报错", "日志", "编译", "部署", "测试", "定位", "排查", "栈", "接口", "重构"),
    "tired": ("困", "睡", "累", "熬夜", "通宵", "好晚", "几点了", "撑不住", "歇"),
}
EMOTION_LABELS = {
    "joy": "心情不错",
    "moved": "有点动容",
    "missing": "想起旧事",
    "worry": "有点担心",
    "aggrieved": "有点委屈",
    "excited": "来劲了",
    "tsundere": "有点嘴硬",
    "focus": "很专注",
    "tired": "有点累",
    "neutral": "平静",
}

# 否定词贴着关键词出现时，这个词不算数：「不开心」不是开心。
NEGATION_WORDS = ("不", "没", "别", "无", "未", "免", "非")
NEGATION_WINDOW = 3
# 程度副词压低或抬高同一条关键词的权重。
INTENSITY_MODIFIERS = (
    ("非常", 1.6), ("特别", 1.6), ("极其", 1.7), ("极了", 1.5),
    ("超", 1.5), ("太", 1.5), ("很", 1.3), ("真", 1.2),
    ("有点", 0.6), ("稍微", 0.6), ("一点点", 0.6), ("一点", 0.7),
)
MODIFIER_WINDOW = 4

# 天赋光谱对情绪的放大系数：某个维度越极端，相关情绪越容易被点着。
SPECTRUM_EMOTION_MAP = {
    "joy": {"warmth": 0.6, "expressiveness": 0.4},
    "moved": {"vulnerability": 0.6, "warmth": 0.4},
    "missing": {"vulnerability": 0.5, "loyalty": 0.5},
    "worry": {"vulnerability": 0.7, "loyalty": 0.3},
    "aggrieved": {"vulnerability": 0.6, "independence": 0.2},
    "excited": {"expressiveness": 0.6, "charm": 0.3},
    "tsundere": {"independence": 0.5, "charm": 0.3},
    "focus": {"intelligence": 0.8},
    "tired": {"vulnerability": 0.4, "intelligence": 0.2},
}

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


def _spectrum_boost(name):
    """天赋光谱把相关情绪放大或压低；导入放进函数里，避免 memory → personality 的循环。"""
    dims = SPECTRUM_EMOTION_MAP.get(name) or {}
    if not dims:
        return 1.0
    try:
        from .personality import SPECTRUM
    except Exception:
        return 1.0
    total = 0.0
    for dim, weight in dims.items():
        total += weight * (float(SPECTRUM.get(dim, 0.5)) - 0.5)
    return max(0.5, min(1.8, 1.0 + total))


def _negated(body, index):
    """关键词前面贴着否定词才算否定；「非常」里的「非」是程度副词，不能当否定。"""
    prefix = body[max(0, index - NEGATION_WINDOW):index]
    for word in NEGATION_WORDS:
        start = prefix.find(word)
        while start >= 0:
            tail = prefix[start:]
            if not any(tail.startswith(mod) for mod, _weight in INTENSITY_MODIFIERS
                       if mod.startswith(word)):
                return True
            start = prefix.find(word, start + 1)
    return False


def _modifier(body, index):
    """看关键词前面几个字有没有程度副词；没有就返回中性 1.0。"""
    prefix = body[max(0, index - MODIFIER_WINDOW):index]
    factor = 1.0
    for word, weight in INTENSITY_MODIFIERS:
        if word not in prefix:
            continue
        factor = max(factor, weight) if weight >= 1.0 else min(factor, weight)
    return factor


def _emotion_score(body, words):
    """同一类情绪里命中的每个关键词加权累加；被否定的不算数。"""
    score = 0.0
    for word in words:
        start = 0
        while True:
            index = body.find(word, start)
            if index < 0:
                break
            start = index + len(word)
            if _negated(body, index):
                continue
            score += _modifier(body, index)
    return score


def update_emotion(state, text):
    """按正在说的话推她自己的情绪；关键词 + 光谱加权 + 情感弧线，全程不调模型。"""
    emotion = state.setdefault("emotion", {})
    for key, value in (("name", "neutral"), ("intensity", 0.0), ("streak", 0),
                       ("updated_at", ""), ("history", []), ("arc_history", [])):
        emotion.setdefault(key, value)
    body = (text or "").strip()
    hits = {}
    if body:
        for name, words in EMOTION_KEYWORDS.items():
            score = _emotion_score(body, words)
            if score:
                hits[name] = score * _spectrum_boost(name)
    if not hits:
        emotion["intensity"] = max(0.0, float(emotion.get("intensity") or 0.0) - EMOTION_FADE)
        if emotion["intensity"] <= 0.0:
            emotion["name"] = "neutral"
            emotion["streak"] = 0
            emotion["intensity"] = 0.0
        return emotion
    name = max(hits, key=hits.get)
    before = float(emotion.get("intensity") or 0.0)
    if name == emotion.get("name"):
        streak = int(emotion.get("streak") or 0) + 1
        bonus = min(ARC_BONUS_MAX, (streak - ARC_MIN_STREAK) * ARC_BONUS_STEP) if streak >= ARC_MIN_STREAK else 0.0
        emotion["streak"] = streak
        emotion["intensity"] = min(1.0, before + EMOTION_STEP + bonus)
    else:
        # 情绪切换要平滑：新的占大头，旧的余温留着一点，不能瞬间归零。
        emotion["name"] = name
        emotion["streak"] = 1
        emotion["intensity"] = min(1.0, max(EMOTION_STEP,
                                            before * EMOTION_SMOOTH_OLD + EMOTION_STEP * EMOTION_SMOOTH_NEW))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    emotion["updated_at"] = now
    emotion["history"] = (list(emotion.get("history") or []) +
                          [{"name": name, "intensity": round(float(emotion["intensity"]), 3), "at": now}])[-20:]
    emotion["arc_history"] = (list(emotion.get("arc_history") or []) + [name])[-ARC_HISTORY_LIMIT:]
    return emotion


def _peak_emotion(emotion):
    """最近这段时间最常出现的情绪；没重复过两次就算没有峰值。"""
    arc = [item for item in (emotion.get("arc_history") or [])
           if item in EMOTION_LABELS and item != "neutral"]
    if not arc:
        return ""
    counts = {}
    for item in arc:
        counts[item] = counts.get(item, 0) + 1
    name, count = max(counts.items(), key=lambda pair: pair[1])
    return name if count >= 2 else ""


def emotion_note(state):
    emotion = state.get("emotion") or {}
    name = emotion.get("name")
    if name not in EMOTION_LABELS or name == "neutral":
        return ""
    if float(emotion.get("intensity") or 0.0) < EMOTION_NOTE_MIN:
        return ""
    line = "- 你现在" + EMOTION_LABELS[name]
    streak = int(emotion.get("streak") or 0)
    if streak >= ARC_MIN_STREAK:
        line += "（连着 %d 次了，可以自然表现得更明显一点，但不要自报情绪）" % streak
    peak = _peak_emotion(emotion)
    if peak and peak != name:
        line += "；最近你多半是%s，这会儿不一样" % EMOTION_LABELS[peak]
    return line


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
