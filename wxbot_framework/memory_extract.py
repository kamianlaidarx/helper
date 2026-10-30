"""从一批群聊消息里抽结构化事实，喂给 memory.merge_facts。

刻意保持无状态：一次调用换一批事实，失败返回空列表，绝不抛到消息主循环。
"""

from __future__ import annotations

SYSTEM = """你在替一个群聊机器人整理它该长期记住的事。

输入是一段群聊，每行形如 `[U3] 张三：内容`，编号只在本次整理内有效。
只输出 JSON：{"facts":[{"subject":"","predicate":"","object":"","text":"","importance":5,"speaker":"[U3]"}]}

要求：
1. subject 是被描述的人、群或事物，用群里实际叫法。
2. predicate 是简短关系或动作，两到六个字，如「养了」「在做」「住在」「讨厌」。
3. object 是具体内容，尽量短。
4. text 用一句自然中文把这件事说完整，不超过 25 个字，要能脱离上下文单独读懂。
5. importance 取 1 到 10：随口闲聊 1-3，偏好习惯 4-6，身份职业关系 7-9，明确计划或承诺 10。
6. speaker 填说话人编号，形如 [U3]。
7. 不要记：一次性的情绪、玩笑玩梗、机器人自己说过的话、没有主语的对答、命令与系统消息。
8. 相对时间换成绝对日期，拿不准就不记。
9. 最多 12 条。没有值得记的就返回 {"facts":[]}。"""

MAX_FACTS_PER_BATCH = 12


def _marker_row(rows, marker):
    text = str(marker or "").strip()
    if not (text.startswith("[U") and text.endswith("]")):
        return None
    try:
        index = int(text[2:-1]) - 1
    except ValueError:
        return None
    if 0 <= index < len(rows):
        return rows[index]
    return None


def extract(client, rows):
    """返回可直接交给 merge_facts 的事实列表；任何异常都退化成空列表。"""
    if not rows:
        return []
    lines = []
    for index, row in enumerate(rows, 1):
        text = (row.get("text") or "").strip()
        if text:
            lines.append("[U%d] %s：%s" % (index, row.get("speaker") or "?", text))
    if not lines:
        return []
    try:
        result = client.complete_json(SYSTEM, "messages:\n" + "\n".join(lines))
    except Exception:
        return []
    facts = result.get("facts") if isinstance(result, dict) else None
    if not isinstance(facts, list):
        return []
    cleaned = []
    for item in facts[:MAX_FACTS_PER_BATCH]:
        if not isinstance(item, dict):
            continue
        subject = str(item.get("subject") or "").strip()
        text = str(item.get("text") or "").strip()
        if not subject or not text:
            continue
        row = _marker_row(rows, item.get("speaker"))
        cleaned.append(
            {
                "subject": subject,
                "predicate": str(item.get("predicate") or "").strip(),
                "object": str(item.get("object") or "").strip(),
                "text": text,
                "importance": item.get("importance"),
                "speaker": (row or {}).get("speaker") or "",
            }
        )
    return cleaned
