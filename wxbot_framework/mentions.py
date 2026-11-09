"""真实群艾特名单来自微信 msgsource，不凭可见昵称猜测目标。"""
import re
from xml.etree import ElementTree

from .account import validate_account


def parse_at_members(source):
    if not isinstance(source, str) or len(source) > 4095 or "<!DOCTYPE" in source.upper() or "<!ENTITY" in source.upper():
        return ()
    try:
        root = ElementTree.fromstring(source)
    except (ElementTree.ParseError, ValueError):
        return ()
    node = root if root.tag == "atuserlist" else root.find(".//atuserlist")
    if node is None or not node.text:
        return ()
    result = []
    for value in node.text.split(","):
        value = value.strip()
        try:
            validate_account(value)
        except ValueError:
            continue
        if value not in result:
            result.append(value)
    return tuple(result)


def strip_at_labels(text):
    return re.sub(r"@[^@\r\n\u2005]+\u2005", "", text).strip()


def keep_at_names(text, drop=()):
    """保留被艾特的人名，只把分隔用的 U+2005 换成空格。

    `strip_at_labels` 回答的是「这句话在要什么」，所以它把 `@某某` 整段删掉；
    但交给模型的上下文必须知道刚才说的是谁——否则「你觉得 @某人 怎么样」会变成
    「你觉得 怎么样」，模型只能反问「你说的是谁」。

    `drop` 里的名字（机器人自己的群昵称、微信名）会被丢掉：它知道自己正在被叫，
    留着只会让模型把自己当成在场的第三个人。
    """
    if not isinstance(text, str):
        return ""
    dropped = {name.strip() for name in drop if isinstance(name, str) and name.strip()}

    def replace(match):
        label = match.group(1)
        return " " if label[1:].strip() in dropped else label + " "

    result = re.sub(r"(@[^@\r\n\u2005]+)\u2005", replace, text)
    return re.sub(r"[ \t]{2,}", " ", result).strip()
