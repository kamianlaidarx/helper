"""探测当前大模型配置能否接受图片输入；只打印结论，不写日志、不打印密钥。"""
import argparse
import base64
from io import BytesIO
import json
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent


def sample():
    """内存里画三个色块，不依赖外部素材；太小的图有些网关会直接断连。"""
    from PIL import Image, ImageDraw
    image = Image.new("RGB", (160, 120), "white")
    pen = ImageDraw.Draw(image)
    pen.ellipse((10, 10, 70, 70), fill="red")
    pen.rectangle((90, 10, 150, 70), fill="blue")
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def payload(config):
    return {"model": config["model"], "max_tokens": 32, "stream": False,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "这张图是什么颜色？只回答颜色。"},
                {"type": "image_url", "image_url": {"url": sample()}}]}]}


def probe(config, open_url=urlopen):
    """返回 (是否接受图片, 一句结论)。不支持时结论里带上服务端的原始拒绝原因。"""
    base = config.get("base_url", "https://api.deepseek.com").rstrip("/")
    endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"
    request = Request(endpoint, data=json.dumps(payload(config)).encode("utf-8"),
                      headers={"Authorization": "Bearer " + config.get("api_key", ""),
                               "Content-Type": "application/json"}, method="POST")
    try:
        with open_url(request, timeout=config.get("timeout_seconds", 30)) as response:
            body = json.loads(response.read(65536))
    except HTTPError as error:
        detail = error.read(4096).decode("utf-8", "replace")
        return False, f"HTTP {error.code}：{detail[:300]}"
    except (URLError, TimeoutError, OSError) as error:
        return False, f"连接失败或超时：{type(error).__name__}"
    content = body["choices"][0]["message"].get("content")
    return True, f"接口接受了图片输入，模型回答：{content!r}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "llm_config.json"), help="大模型配置文件")
    args = parser.parse_args()
    path = Path(args.config)
    if not path.is_file():
        print(f"找不到配置文件：{path}；先在仓库根目录放好 llm_config.json")
        return 1
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    if not config.get("api_key"):
        print("llm_config.json 里没有 api_key")
        return 1
    accepted, message = probe(config)
    print(("支持图片输入" if accepted else "不支持图片输入") + "｜" + message)
    print(f"model={config.get('model')} base_url={config.get('base_url')}")
    return 0 if accepted else 2


if __name__ == "__main__":
    sys.exit(main())
