"""调用 DeepSeek Search 接口补充实时资料；任何失败都静默跳过，不影响聊天。"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "search_config.json"
LLM_CONFIG_PATH = ROOT / "llm_config.json"

MAX_SUMMARY = 400
MAX_RESULTS = 10

DEFAULT_TRIGGERS = (
    "今天", "昨天", "最新", "最近", "现在", "实时", "新闻",
    "多少钱", "价格", "谁是", "什么时候", "天气", "比分",
    "发布了", "出了吗", "怎么样了", "查一下", "搜一下",
)

TIME_MARK = re.compile(r"\d+\s*(?:秒|分钟|小时|天|周|月)前")
NOISE_MARK = re.compile(r"[＋+]\s*关注|责任编辑[：:][^\s]*|分享到[^\s]*")
ELLIPSIS = re.compile(r"\s*(?:\.{2,}|…+)\s*")

_cache = {"mtime": None, "value": None}


def _read_json(path, default):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default
    return data if isinstance(data, dict) else default


def _normalize(raw):
    count = raw.get("count", 5)
    timeout = raw.get("timeout_seconds", 3)
    triggers = raw.get("triggers")
    if not isinstance(triggers, (list, tuple)):
        triggers = DEFAULT_TRIGGERS
    words = tuple(str(word).strip() for word in triggers if str(word).strip())
    return {
        "enabled": bool(raw.get("enabled", True)),
        "count": max(1, min(MAX_RESULTS, count if isinstance(count, int) else 5)),
        "timeout_seconds": timeout if isinstance(timeout, (int, float)) and 1 <= timeout <= 10 else 3,
        "triggers": words or DEFAULT_TRIGGERS,
    }


def load_settings():
    """读取 search_config.json；文件改动后自动重新加载，不必重启进程。"""
    try:
        mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        return _normalize({})
    if _cache["mtime"] != mtime:
        _cache["value"] = _normalize(_read_json(CONFIG_PATH, {}))
        _cache["mtime"] = mtime
    return _cache["value"]


def _endpoint():
    """只在 DeepSeek 官方端点上启用；其他网关没有 tools/search 接口。"""
    cfg = _read_json(LLM_CONFIG_PATH, {})
    base = cfg.get("base_url")
    key = cfg.get("api_key")
    if not isinstance(base, str) or not isinstance(key, str) or not key.strip():
        return None, None
    base = base.strip().rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")].rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3].rstrip("/")
    try:
        parsed = urlsplit(base)
    except ValueError:
        return None, None
    if parsed.scheme != "https" or parsed.hostname != "api.deepseek.com":
        return None, None
    return base, key.strip()


def _clean(text, limit=MAX_SUMMARY):
    """只归一空白并截断，语义交给模型判断（聚合页文本无法靠正则还原）。"""
    if not isinstance(text, str):
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _is_root_page(url):
    """站点首页通常只是一堆链接，没有可引用的事实。带查询参数的是具体页面。"""
    if not isinstance(url, str):
        return True
    try:
        parsed = urlsplit(url)
    except ValueError:
        return True
    if parsed.query:
        return False
    path = parsed.path.strip("/")
    return not path or path in ("home", "index.html", "index.htm")


def search(query, count=5, timeout=3):
    """POST /v1/tools/search，只保留能读的短摘要文本。"""
    base, key = _endpoint()
    text = (query or "").strip()
    if not base or not key or not text:
        return []
    body = {"query": text[:100], "count": count, "need_content": False}
    try:
        request = Request(base + "/v1/tools/search",
                          data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                          headers={"Authorization": "Bearer " + key,
                                   "Content-Type": "application/json"},
                          method="POST")
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(512 * 1024)
        data = json.loads(raw)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        logging.getLogger("SEARCH").warning("联网搜索失败，本轮跳过")
        return []
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return []
    rows = []
    for item in results:
        if not isinstance(item, dict):
            continue
        url = item.get("url") if isinstance(item.get("url"), str) else ""
        if _is_root_page(url):
            continue
        snippet = item.get("snippet") or item.get("summary") or ""
        summary = _clean(snippet)
        if not summary:
            continue
        rows.append(summary)
    return rows[:count]


def block_for(query, settings=None):
    """命中触发词才联网；把结果当作背景事实交给模型，不提来源也不提搜索。"""
    cfg = settings or load_settings()
    if not cfg["enabled"]:
        return ""
    text = (query or "").strip()
    if not text or not any(word in text for word in cfg["triggers"]):
        return ""
    rows = search(text, count=cfg["count"], timeout=cfg["timeout_seconds"])
    if not rows:
        return ""
    return "\n".join([""] + ["- " + summary for summary in rows])
