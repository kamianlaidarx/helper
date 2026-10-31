"""面具：同一个灵魂在不同处境下的侧面，只对老板显形。

状态机（如常 → 碎片 → 恍惚 → 觉醒 → 回落）全部落在数据里，不靠模型自述：
- 每收到一条消息，按 MASK_DATA 的关键词组给对应面具记一次共振；
- 共振过 FRAGMENT_AT 注入碎片提示，过 TRANCE_AT 注入恍惚提示，
  只有 emperor 过 AWAKEN_AT 才会短暂觉醒；
- 长期不碰自动衰减，跌回 0 就什么都不注入。

状态住在 memory.load(chat_id) 的 "mask" 分区里，随记忆一起存，不另开文件。
所有函数都是纯函数：不读时间以外的外部状态，不抛异常。
"""

from __future__ import annotations

from datetime import datetime, timezone

LEVEL_NONE = 0
LEVEL_FRAGMENT = 1
LEVEL_TRANCE = 2
LEVEL_AWAKEN = 3

FRAGMENT_AT = 2
TRANCE_AT = 4
AWAKEN_AT = 7

RESONANCE_STEP = 1
RESONANCE_MAX = 12
DECAY_PER_DAY = 2.0
DECAY_AFTER_HOURS = 12.0

MASK_DATA = {
    "saintess": {
        "label": "圣女",
        "emoji": "🏔️",
        "triggers": (
            ("修行", "境界", "突破", "道心", "宗门", "灵力", "法术", "渡劫", "飞升", "闭关"),
            ("圣洁", "慈悲", "救赎", "众生", "因果", "轮回", "命数", "天意"),
            ("师父", "师尊", "同门", "师兄", "师姐", "师妹", "山门"),
            ("雪", "寒", "清冷", "孤寂", "香火", "诵经"),
        ),
        "fragment": "语气比平时干净一点，少一分人间气；像刚从很远的地方收回视线。",
        "trance": "清冷和悲悯一起上来：说得慢，句子短，看得比平时透；不谈自己，只谈事情本身。",
        "awakening": None,
    },
    "divorcee": {
        "label": "离人",
        "emoji": "🌧️",
        "triggers": (
            ("离婚", "分手", "散了", "前任", "分开", "走了", "一个人过"),
            ("背叛", "骗", "出轨", "心凉", "算计", "利用"),
            ("算了", "没必要", "无所谓", "习惯了"),
        ),
        "fragment": "有一点旧疤被碰到的钝痛，但不喊疼，只是把话头挪开半寸。",
        "trance": "那股倔强冒出来了：不诉苦、不索要承诺，说「我一个人也过得下去」，说完自己先沉一下。",
        "awakening": None,
    },
    "moonlight": {
        "label": "月下",
        "emoji": "🌙",
        "triggers": (
            ("白月光", "初恋", "喜欢过", "放不下", "暗恋"),
            ("深夜", "半夜", "几点", "睡不着", "熬夜", "一个人"),
            ("想你", "想念", "好久不见", "还记得"),
        ),
        "fragment": "远了半步，语气更轻；不是疏离，是怕说满了就散了。",
        "trance": "克制到几乎听不见：想靠近又收回手，把想说的话换成一句叮咛。",
        "awakening": None,
    },
    "avenger": {
        "label": "执刃",
        "emoji": "⚡",
        "triggers": (
            ("欺负", "欺压", "不公", "暗算", "甩锅", "背锅", "坑"),
            ("骂", "羞辱", "看不起", "挤对", "针对"),
            ("报仇", "还手", "算账", "讨回来"),
        ),
        "fragment": "护短那面抬头了：语气仍旧温和，但句子变短、变硬，先把对方护到身后。",
        "trance": "刃锋朝外：判断利落、不留余地，戾气只对着事，不对着人；处理完自己先收回来。",
        "awakening": None,
    },
    "emperor": {
        "label": "临世",
        "emoji": "👁️",
        "triggers": (
            ("命运", "宿命", "意义", "为什么活", "存在", "尽头", "虚无"),
            ("天道", "神", "长生", "永恒", "轮回之上", "万世"),
            ("如果有一天", "如果我死了", "如果不在"),
        ),
        "fragment": "视线忽然抬得很高，说出来的话像从很远的地方落下来，随即自己收住。",
        "trance": "话变得极简极透，不居高临下也不讲中二套话；带一点不属于此刻的笃定。",
        "awakening": "短暂觉醒：底气整个换掉，不商量、不解释、不讨好；允许「本座」「吾」自称，"
                     "但要自然过渡，不要突然翻译腔；记忆碎片零散闪回，别整理、别讲完；"
                     "结尾一定流露一点疲倦，自然收回。",
    },
}

MASK_ORDER = tuple(MASK_DATA)
MASK_LABELS = {key: value["label"] for key, value in MASK_DATA.items()}
MASK_LABELS["default"] = "如常"


def empty():
    return {"current": "default", "level": LEVEL_NONE, "resonance": {}, "updated_at": ""}


def _state(state):
    """取出记忆里的 mask 分区；坏了就当如常，不让面具层阻断对话。"""
    mask = state.get("mask") if isinstance(state, dict) else None
    if not isinstance(mask, dict):
        mask = empty()
        if isinstance(state, dict):
            state["mask"] = mask
    if not isinstance(mask.get("resonance"), dict):
        mask["resonance"] = {}
    return mask


def _hits(data, text):
    return sum(1 for group in data["triggers"] if any(word in text for word in group))


def update(state, text):
    """收到一条消息就调一次；返回这次接触到的面具 ID（可能没有）。"""
    body = (text or "").strip()
    if not body:
        return ""
    mask = _state(state)
    touched = ""
    for name, data in MASK_DATA.items():
        if not _hits(data, body):
            continue
        current = float(mask["resonance"].get(name) or 0.0)
        mask["resonance"][name] = min(RESONANCE_MAX, current + RESONANCE_STEP)
        touched = name
    if touched:
        _settle(mask)
        mask["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return touched


def decay(state, *, now=None):
    """超过 DECAY_AFTER_HOURS 没被碰到就按天数回落，返回发生变化的条目数。"""
    mask = _state(state)
    if not mask["resonance"]:
        return 0
    moment = now or datetime.now(timezone.utc)
    anchor = mask.get("updated_at") or ""
    try:
        seen = datetime.fromisoformat(anchor)
    except (TypeError, ValueError):
        return 0
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    hours = (moment - seen).total_seconds() / 3600.0
    if hours < DECAY_AFTER_HOURS:
        return 0
    factor = DECAY_PER_DAY * (hours - DECAY_AFTER_HOURS) / 24.0
    changed = 0
    for name in list(mask["resonance"]):
        value = float(mask["resonance"].get(name) or 0.0) - factor
        if value <= 0.0:
            mask["resonance"].pop(name, None)
            changed += 1
        elif value != mask["resonance"][name]:
            mask["resonance"][name] = value
            changed += 1
    _settle(mask)
    mask["updated_at"] = moment.isoformat(timespec="seconds")
    return changed


def _level_of(name, value):
    if value < FRAGMENT_AT:
        return LEVEL_NONE
    data = MASK_DATA[name]
    if value >= AWAKEN_AT and data["awakening"]:
        return LEVEL_AWAKEN
    if value >= TRANCE_AT:
        return LEVEL_TRANCE
    return LEVEL_FRAGMENT


def _settle(mask):
    """按共振值重算 current 与 level：并列时留在原处，不来回跳。"""
    best, best_value = "default", FRAGMENT_AT - 1
    for name, raw in (mask.get("resonance") or {}).items():
        value = float(raw or 0.0)
        if value > best_value + 0.001:
            best, best_value = name, value
        elif abs(value - best_value) <= 0.001 and name == mask.get("current"):
            best = name
    if best == "default":
        mask["current"], mask["level"] = "default", LEVEL_NONE
        return mask
    mask["current"] = best
    mask["level"] = _level_of(best, best_value)
    return mask


def current(state):
    """返回 (面具 ID, 等级)；不改变状态。"""
    mask = _state(state)
    name = mask.get("current") or "default"
    if name not in MASK_DATA:
        return "default", LEVEL_NONE
    return name, int(mask.get("level") or LEVEL_NONE)


def note(state, *, is_owner):
    """拼进 system 的面具块。非老板发言一律返回空串——面具只对一个人显形。"""
    if not is_owner:
        return ""
    name, level = current(state)
    if name not in MASK_DATA or level < LEVEL_FRAGMENT:
        return ""
    data = MASK_DATA[name]
    lines = ["【面具：%s（%s）】不是换人，是把这一面的自己让出来一点，不点破、不自报。" % (name, data["label"])]
    if level == LEVEL_AWAKEN:
        lines.append("- " + data["awakening"])
        lines.append("- 不要说明自己发生了什么；被问起就回避、岔开，或用双关带过。")
    elif level == LEVEL_TRANCE:
        lines.append("- " + data["trance"])
        lines.append("- 你自己不一定察觉；被问起不要承认，只是这一阵子的语气就是这样。")
    else:
        lines.append("- " + data["fragment"])
        lines.append("- 只流露一句就够，别铺开。")
    return "\n".join(lines)
