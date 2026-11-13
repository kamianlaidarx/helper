"""playhub 引擎包：回包类型、运行期上下文、引擎注册表。

本包只放三样东西，别的一律不加：

1. `Reply`：引擎交给 `app.py` 的**纯文本**结果。本批没有图片/语音/卡片，
   也没有慢活执行器（`§1 表尾`：第 1 批由 `app` 直连发送，`executor` 留到第 3 批）。
2. `store_of()`：从运行期上下文里取 `Store` 的唯一入口。引擎不持有连接，
   连接只属于 `core/store.py`，且永不跨线程（§2.5 第 4 条：执行器线程禁碰 Store）。
3. `build_engines()`：引擎名 → 实例。键必须与 `keywords.Trigger.engine` 逐字一致，
   否则 `registry.dispatch` 按引擎名匹配时会把请求静默丢掉。

关于 `§1 清单` 里的 `engines/base.py`（Engine 协议、Command、Reply）：
本批交付文件清单里没有它，所以 `Reply` 与引擎协议
（`name` 属性 + `on_command(cmd, msg, ctx, game)` 方法）临时落在这里。
`base.py` 落地时把这两个定义整段搬走即可——调用方统一从 `engines` 包导入，不用改。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# §3.4 `top(..., limit=10)` 的默认条数；后续三榜复用同一常量。
DEFAULT_LIMIT = 10


@dataclass(frozen=True)
class Reply:
    """一条待发文本。

    `action` 只用于日志/审计定位，不参与发送；本批没有附件字段，
    因为 `§2.5` 禁止回调内出现 HTTP/LLM/图片。
    """

    text: str
    action: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text:
            raise ValueError("Reply.text 必须是非空字符串")


@dataclass(frozen=True)
class GameRuntime:
    """运行期上下文（本批极简版）：只带 `Store`。

    `core/__init__.py` 的 `Game` 门面尚未落地（本批 core 只交付 store/wallet），
    引擎因此只依赖 `store` 一个属性；门面落地后在此加字段，引擎签名不变。
    """

    store: Any


def store_of(game: Any):
    """取 `Store`；允许直接把 `Store` 当 `game` 传（测试与脚本更省事）。"""
    store = getattr(game, "store", game)
    if store is None or not callable(getattr(store, "tx", None)):
        raise ValueError("运行期上下文缺少 Store（需要 game.store 带 tx()）")
    return store


def msg_room(msg: Any) -> str:
    """群 id：群消息时 `sdk.Message.chat_id` 等于 `room_wxid`（`text.py:110-116`）。"""
    value = getattr(msg, "chat_id", "") or ""
    return value if isinstance(value, str) else ""


def msg_uid(msg: Any) -> str:
    """发言者 wxid：`sdk.Message.sender_id`。身份判定只认它，不认昵称。"""
    value = getattr(msg, "sender_id", "") or ""
    return value if isinstance(value, str) else ""


def msg_nickname(msg: Any, ctx: Any = None) -> str:
    """昵称：先取消息自带的 `sender_wechat_name`，再退回 `ctx.speaker()`，都没有给空串。

    昵称只用于建档展示，不参与任何判定（§6.1：身份是 uid）。
    """
    data = getattr(msg, "data", None)
    if isinstance(data, Mapping):
        name = data.get("sender_wechat_name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    speaker = getattr(ctx, "speaker", None) if ctx is not None else None
    if callable(speaker):
        try:
            name = getattr(speaker(msg), "name", "")
        except Exception:      # 昵称取不到不影响发币，按「没有昵称」处理
            name = ""
        if isinstance(name, str) and name.strip():
            return name.strip()
    return ""


def build_engines(*, rng=None, now_fn=None, repo_root=None, config=None,
                  executor=None) -> dict[str, Any]:
    """构建引擎表。

    子模块在函数内延迟 import：`engines/__init__` 定义 `Reply`，
    子模块又要 `from . import Reply`，顶层 import 会形成循环。

    `config` 是 `bot_config.json → plugins.playhub` 那一节：只有出站能力（天气）
    需要它——`build_provider()` 据此决定联网还是离线（见 `adapters/outbound.py`）。
    `executor` 是插件的慢活执行器（`adapters/executor.py`）；为 `None` 时天气退回
    同步查询（测试与未启用执行器的场景）。
    """
    from .daily import DailyEngine
    from .quiz import QuizEngine
    from .ranking import RankingEngine
    from .social import SocialEngine
    from .lottery import LotteryEngine
    from .lounge import LoungeEngine
    from .duel import DuelEngine
    from .music import MusicEngine
    from .outbound import OutboundEngine
    from .groupstat import GroupStatEngine
    from .moderation import ModerationEngine
    from ..adapters.outbound import build_provider
    from ..adapters.xiaoapi import build_music_provider

    return {
        "daily": DailyEngine(rng=rng, now_fn=now_fn, repo_root=repo_root),
        "ranking": RankingEngine(now_fn=now_fn),
        # 第 2 批：竞猜/找茬/问答共用这一个引擎（§4 第 10 条）
        "quiz": QuizEngine(rng=rng, now_fn=now_fn, repo_root=repo_root),
        # 第 3 批：社交（婚姻 / 四关系 / 关系卡 / 配对 / 礼物 / 宝宝）
        "social": SocialEngine(now_fn=now_fn),
        # 第 3 批：盲盒、开宝箱（物品来源）
        "lottery": LotteryEngine(rng=rng, now_fn=now_fn),
        # 第 3 批：文案馆 / 群空间 / 群聊
        "lounge": LoungeEngine(now_fn=now_fn, repo_root=repo_root),
        # 第 3 批：切磋（不造币，只记战绩）
        "duel": DuelEngine(rng=rng, now_fn=now_fn),
        # 第 3 批：点歌（配了 xiaoapi key 就查曲库，否则退回只登记报位）
        "music": MusicEngine(
            now_fn=now_fn, provider=build_music_provider(config), executor=executor
        ),
        # 第 3 批：出站能力，天气走 adapters.outbound 的 provider（默认离线）；
        # 有执行器时两跳 HTTP 交给 worker，回调线程不再等网络（§2.5 第 3 条）
        "outbound": OutboundEngine(
            now_fn=now_fn, provider=build_provider(config), executor=executor
        ),
        # 第 4 批：潜水 / 邀请 / 改名巡检（只统计，不清人）
        "groupstat": GroupStatEngine(now_fn=now_fn),
        # 第 4 批：小黑屋 / 黑名单（只在本插件内生效）
        "moderation": ModerationEngine(now_fn=now_fn),
    }
