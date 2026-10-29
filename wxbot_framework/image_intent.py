"""判断一段艾特文本是不是在要求画图：纯规则匹配，不调用模型。"""
import re

TRIGGERS = ("画", "画图", "画张", "画个", "画只", "画幅", "生图", "生成图", "出图",
            "draw", "imagine", "paint")

ASKING_ABOUT = re.compile(r"(?:图|照片|图片|画)(?:里|中|上)?(?:是|有|写|画)?.{0,4}?(?:什么|谁|啥|哪)")

REQUESTS = (
    re.compile(r"(?:帮|给|请|麻烦|替|想|要|能不能|可以)[^\r\n]{0,6}?(?:画|生成|绘制|出图)"),
    re.compile(r"(?:来|要|想看|想)[^\r\n]{0,4}?(?:张|幅|个)[^\r\n]{0,8}?(?:图|照片|画)"),
    re.compile(r"(?:想|要)(?:看|见)[^\r\n]{0,15}?(?:照片|图片|样子)"),
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
