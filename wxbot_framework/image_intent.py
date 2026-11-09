"""判断一段艾特文本是不是在要求画图：纯规则匹配，不调用模型。"""
import re

TRIGGERS = ("画", "画图", "画张", "画个", "画只", "画幅", "生图", "生成图", "出图",
            "draw", "imagine", "paint")

ASKING_ABOUT = re.compile(r"(?:图|照片|图片|画)(?:里|中|上)?(?:是|有|写|画)?.{0,4}?(?:什么|谁|啥|哪)")

REQUESTS = (
    re.compile(r"(?:帮|给|请|麻烦|替|想|要|能不能|可以)[^\r\n]{0,6}?(?:画|生成|绘制|出图)"),
    # 量词和宾语之间必须有实际内容："要张图"是空请求，交给聊天追问；"要张夕阳的图"才是生图。
    re.compile(r"(?:要|想看|想)[^\r\n]{0,4}?(?:张|幅|个)[^\r\n]{1,8}?(?:图|照片|画)"),
    re.compile(r"(?:想|要)(?:看|见)(?:一|两|几)?(?:张|幅|个)?[^\r\n张幅个]{1,15}?(?:照片|图片|样子)"),
    re.compile(r"(?:画|生成|绘制|出图)[^\r\n]{0,10}?(?:图片|照片|图|画|海报|插画)"),
    re.compile(r"生成[^\r\n]{0,8}?(?:一张|一幅)"),
)


def wants_image(text):
    """这段文字是否在要求生图；识图类提问（图里是什么）一律算聊天。"""
    value = (text or "").strip()
    if not value:
        return False
    if ASKING_ABOUT.search(value):
        return False
    lowered = value.lower()
    if any(lowered.startswith(trigger) for trigger in TRIGGERS):
        return True
    return any(pattern.search(value) for pattern in REQUESTS)


# 「画一张 @某人 的画像」既像生图又像人物画像：这条路由 mention_chat 出图文，
# image_gen 见到就要让路，否则同一个艾特会被两个插件各画一次。
PORTRAIT_MARKERS = ("画像", "画个像", "画像照")


def is_portrait_request(text):
    """这段文字是不是在要某个群成员的画像（交给 mention_chat，不要生图）。"""
    value = text or ""
    if not isinstance(value, str) or not value.strip():
        return False
    return any(marker in value for marker in PORTRAIT_MARKERS)
