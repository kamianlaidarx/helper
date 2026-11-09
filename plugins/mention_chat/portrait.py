"""「@某人 是个什么样的人」：只拿这个人自己的发言说话，可选再画一张画像。

三件事都在这里，插件只管收发：
- `intent()` 认这句话是不是在问某个人，且要不要配张图；
- `sample()` 从原始发言里挑出有信息量的句子，剔除寒暄、纯表情和重复；
- `build_prompt()` / `parse_reply()` 负责提示词与结果清洗，`image_prompt()` 把结论折成画面描述。

数据面只有一个来源：`MemberStore.member_messages()`，也就是这个人本人在本群说过的话。
群里别人的话不进提示词——那会把群氛围当成他的性格。
"""
import importlib.util
import re
import sys
from dataclasses import dataclass

from wxbot_framework.personality import system_with_personality

DAYS = 30
LIMIT = 60
MAX_LINES = 4
MAX_REPLY_CHARS = 80
MAX_SAMPLE_CHARS = 120
IMAGE_PER_DAY = 2
DISCLAIMER = "（这是我从他在本群说过的话里读出来的，不是事实判断）"

# 问「这个人怎么样」的说法；刻意列全一点，避免把普通提问也卷进来。
TEXT_TRIGGERS = ("什么样的人", "怎样的人", "啥样的人", "什么人", "什么性格", "性格怎么样",
                 "人品怎么样", "靠谱吗", "这人怎么样", "这个人怎么样")
# 想要一张图；「画像」是最明确的信号，其余都要求同时出现「画」或「图」。
IMAGE_TRIGGERS = ("画像", "画一张", "画个像", "画一幅", "画张", "画一下", "长什么样")

# 只有这些词的话，说明不了任何事：占了屏幕也只是噪音。
NOISE = frozenset({
    "在", "在吗", "在不在", "嗯", "嗯嗯", "哦", "哦哦", "好", "好的", "好吧", "行", "可以",
    "收到", "谢谢", "谢了", "哈哈", "哈哈哈", "哈哈哈哈", "笑死", "笑死我了", "6", "666",
    "牛", "牛逼", "厉害", "是", "对", "对的", "不是", "没有", "有事", "没事", "晚安", "早安",
    "早", "睡了", "来了", "走了", "上班", "下班", "吃饭", "吃了吗", "打卡", "+1", "1",
})
# 评价里不该出现的方向：模型一写这些就越界了，整句丢掉比发出去再道歉便宜。
FORBIDDEN = ("外貌", "长相", "颜值", "身材", "胖", "瘦", "丑", "漂亮", "帅", "年龄", "岁数",
             "收入", "工资", "职业", "学历", "老家", "身价")

_PUNCT = re.compile(r"[\s\W_]+", re.UNICODE)
_URL = re.compile(r"https?://|www\.", re.IGNORECASE)

_SYSTEM = """群里有人想听你说说另一位成员是个什么样的人。
规则：
- 只依据下面列出的、他本人在这段时间说过的话来判断，不要编造没出现的事。
- 用你自己的性格和说话方式写，像平时在群里聊天那样，别写成测评报告。
- 只谈说话风格、关心的事、待人方式；不评价外貌、年龄、收入、职业、学历、地域，不做人身攻击。
- 3 到 {max_lines} 行，每行一句，尽量短，不加编号、不加引号、不用 markdown。
- 另外判断他的性别，只能依据他自己的说法、别人对他的称呼、他的昵称和用词习惯；
  没有把握就写「不确定」，不要按刻板印象猜，也不要为了让画像好看而硬猜。
- 只输出 JSON：{{"评价": ["第一句", "第二句", "第三句"], "性别": "男"}}，
  性别只能是「男」「女」「不确定」三者之一。"""


class Request:
    """一次画像请求：要不要图，由问法决定。"""

    __slots__ = ("wants_image",)

    def __init__(self, wants_image):
        self.wants_image = bool(wants_image)

    def __repr__(self):
        return f"Request(wants_image={self.wants_image})"


def intent(text):
    """判断这句话是不是在问某个人；不是就返回 None。"""
    if not isinstance(text, str):
        return None
    whole = text.strip()
    if not whole:
        return None
    if not any(trigger in whole for trigger in TEXT_TRIGGERS + IMAGE_TRIGGERS):
        return None
    return Request(any(trigger in whole for trigger in IMAGE_TRIGGERS))


def _content(text):
    return _PUNCT.sub("", text)


def sample(rows, *, limit=LIMIT):
    """挑出有信息量的句子，按时间顺序返回（旧→新）。"""
    picked = []
    seen = set()
    for row in rows:
        text = (row.get("text") or "").strip()
        if not text or _URL.search(text):
            continue
        content = _content(text)
        if len(content) < 4 or content in NOISE:
            continue
        text = text[:MAX_SAMPLE_CHARS]
        if text in seen:
            continue
        seen.add(text)
        picked.append(text)
        if len(picked) >= limit:
            break
    return list(reversed(picked))


def build_prompt(state, speaker, display, lines):
    """把人格、目标称呼和这个人的发言拼成 (system, user)。"""
    system = system_with_personality(_SYSTEM.replace("{max_lines}", str(MAX_LINES)), state,
                                     speaker=speaker)
    spoken = "\n".join(f"- {line}" for line in lines)
    user = (f"要说的这位是「{display}」。以下是他本人在本群 {DAYS} 天内说过的话，"
            f"按时间顺序，共 {len(lines)} 句：\n{spoken}\n\n"
            f"用你自己的口吻说说「{display}」是个什么样的人，按约定的 JSON 输出。")
    return system, user


GENDERS = ("男", "女", "不确定")


@dataclass(frozen=True)
class Verdict:
    """模型的一次评价：几句评价 + 一个性别判断（只用于画像，不进聊天正文）。"""

    lines: tuple = ()
    gender: str = "不确定"

    def __bool__(self):
        return bool(self.lines)


def _gender(payload):
    """把模型给的性别归一成三种；只认明确写了男或女的，其余一律不确定。"""
    if not isinstance(payload, dict):
        return "不确定"
    raw = payload.get("性别") or payload.get("gender") or ""
    if not isinstance(raw, str):
        return "不确定"
    text = raw.strip()
    if "女" in text:
        return "女"
    if "男" in text:
        return "男"
    return "不确定"


def parse_reply(payload):
    """从模型结果里取评价句与性别：最多 MAX_LINES 句，过长、越界、空的都丢掉。"""
    if isinstance(payload, dict):
        raw = payload.get("评价") or payload.get("评价句") or payload.get("reply") or []
    elif isinstance(payload, list):
        raw = payload
    else:
        raw = []
    if isinstance(raw, str):
        raw = [line for line in raw.splitlines() if line.strip()]
    lines = []
    for item in raw:
        if not isinstance(item, str):
            continue
        text = item.strip().replace("**", "").replace("`", "").strip()
        if text[:1] in ("-", "•", "·"):
            text = text[1:].strip()
        text = re.sub(r"^\d+[.、)）]\s*", "", text)
        text = text.strip('"“”「」\'').strip()
        if not text or len(text) > MAX_REPLY_CHARS:
            continue
        if any(word in text for word in FORBIDDEN):
            continue
        if text in lines:
            continue
        lines.append(text)
        if len(lines) >= MAX_LINES:
            break
    return Verdict(tuple(lines), _gender(payload))


_STYLE = ("写实人像摄影风格，自然光，浅景深，日常生活场景，面部细节清晰自然，"
          "像一张手机随手拍的合影；不是卡通、不是动漫、不是插画、不是油画；"
          "不出现文字、水印、logo，不出现任何群体标识")
_GENDER_HINT = {"男": "三十岁上下的男性", "女": "三十岁上下的女性",
                "不确定": "不刻意强调性别特征的中性人物"}


def image_prompt(lines, gender="不确定"):
    """把评价折成画面描述；不带名字、不描述具体长相。

    性别只来自模型对被艾特者说话内容的判断，没把握时用中性形象，
    避免把一个女生画成男的（或反过来）。
    """
    traits = "、".join(tuple(lines)[:MAX_LINES]) or "话不多但很有意思"
    who = _GENDER_HINT.get(gender, _GENDER_HINT["不确定"])
    return (f"一张{_STYLE}。画面里是一位{who}，"
            f"神态与穿着要传达这些特征：{traits}。"
            f"不要求与真人长得完全一致，但人物比例与光影要接近真实照片。")


def load_provider(source, config):
    """按路径加载 image_gen 的 provider 并读取生图配置；拿不到就返回 None。

    插件模块名带随机后缀（`_wxbot_plugins.<name>_<uuid>`），没有稳定的 import 名字，
    所以这里直接按文件加载同一个 provider，配置与 image_gen 完全共用。
    """
    source, config = str(source), str(config)
    module = sys.modules.get("_helper_image_provider")
    try:
        if module is None:
            spec = importlib.util.spec_from_file_location("_helper_image_provider", source)
            if spec is None or spec.loader is None:
                return None
            module = importlib.util.module_from_spec(spec)
            sys.modules["_helper_image_provider"] = module
            spec.loader.exec_module(module)
        return module.ImageProvider.from_file(config)
    except Exception:
        sys.modules.pop("_helper_image_provider", None)
        return None
