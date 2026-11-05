"""playhub 编排层：`on_message → 归一化 → route → registry.dispatch → Reply`
（`docs/design/playhub.md:475-495` §2.5、`:517-594` §2.6）。

本批的三条硬规矩：

1. **只做 O(1) 内存活**：判 `is_self` → `route()`（内部归一化）→ 整词查表 →
   一次短事务 → 返回 `Reply`。回调里没有 HTTP、没有 LLM、没有图片下载。
2. **本批不建慢活执行器**：`adapters/executor.py` 属第 3 批。这里直接构造 `Reply`
   交给调用方发送，**不在事务内发送**（事务边界在引擎里，发送在 `Reply` 返回之后）。
3. **零噪音**：非关键词、未绑定动作、`bare_behavior="silent"` 的裸触发、功能开关关闭，
   一律 `return None`——不回复、不写日志、不写审计。

事务顺序由引擎保证：`BEGIN IMMEDIATE` → 校验 → 发币/改状态 → 流水 → `COMMIT` → 构造 `Reply`。
"""
from __future__ import annotations

import logging

from .engines import GameRuntime, Reply, build_engines
from .keywords import TABLE
from .registry import BINDINGS, dispatch
from .router import route

# 帮助文案。§1 清单把它排在 `business.py`（网页可编辑、8 段上限），
# 但本批交付文件清单里没有 business.py，所以暂放这里，内容按 §7 / §2.6.1 / §8.6 写死；
# business.py 落地后整段搬走，`help_text()` 改为读网页配置即可。
HELP_TEXT = "\n".join(
    (
        "playhub 当前可用（第 1 批）：",
        "· 签到（别名 打卡）：每天一次，得金币；连签越久给得越多",
        "· 抽签（别名 求签）：每天一次，得金币，上上签另加钻石",
        "· 排行榜（别名 榜单）：金币榜前 10 名",
        "· 帮助：就是本条，随时可以再看",
        "其余玩法将在后续批次开放。",
        "命令前后不加标点，直接发词即可。",
        "已知限制：接龙开局后，群里任何不以 / 开头的文本（包括签到）都会被当成接龙答案，"
        "机器人会回一句「请输入一个完整的四字成语」，不花额度，但刷屏。",
    )
)

ERROR_TEXT = "这个玩法暂时出错了"

# kv 里 feature.<engine> 的值按这些写法视为「关闭」（§2.5：关闭时静默）
_OFF_VALUES = frozenset({"0", "off", "false", "no", "disabled"})


def help_text() -> str:
    """帮助文案（本批可用动作 + 后续批次说明 + 已知限制）。"""
    return HELP_TEXT


class App:
    """一条消息的完整处理链。

    `store` 必须由调用方按账号传入（每个账号一个 `Store`，连接不跨线程）；
    测试可以注入 `engines` 与 `rng` 以便复现。
    """

    def __init__(self, *, store=None, rng=None, now_fn=None, repo_root=None, engines=None, logger=None):
        self._store = store
        self._game = GameRuntime(store=store)
        self._engines = (
            engines
            if engines is not None
            else build_engines(rng=rng, now_fn=now_fn, repo_root=repo_root)
        )
        self._log = logger if logger is not None else logging.getLogger("playhub.app")

    # -- 主链路 -------------------------------------------------------------
    def handle_message(self, ctx, msg) -> Reply | None:
        if msg is None:
            return None
        # I1 第一道闸：机器人自己发的消息根本不进插件（§2.6.5）。
        if getattr(msg, "is_self", False):
            return None

        text = getattr(msg, "text", None)
        if not isinstance(text, str) or not text.strip():
            return None

        # route() 内部已做 NFKC/零宽/空白折叠（textkit.normalize_with_mentions），
        # 这里不重复归一化，避免两次归一化口径不一致。
        cmd = route(text)
        if cmd is None:
            return None

        trigger = TABLE.trigger(cmd.name)
        if trigger is None:                     # 理论上 route 已保证命中，防御性返回
            return None
        if not cmd.had_arg and trigger.bare_behavior == "silent":
            return None                         # 高风险裸触发：静默（§2.6.6）

        binding = BINDINGS.get(cmd.action)
        if binding is None:
            return None                         # 未绑定动作：与「没命中」对外一致，静默
        # 帮助不经引擎：文案随 app 走，避免为了一个静态回包再拉一层引擎。
        # 放在 feature 开关之前：关闭某个玩法不该连「帮助」一起哑掉。
        if cmd.action == "business.help":
            return Reply(help_text(), cmd.action)

        if self._feature_off(binding.engine, msg):
            return None                         # feature.<engine> 关闭：静默

        engine = self._engines.get(binding.engine)
        if engine is None:
            return None                         # 引擎没接线（后续批次）：静默

        try:
            reply = dispatch(engine, cmd.action, cmd, msg, ctx, self._game)
        except Exception:                       # §2.5：异常不上抛，转成一句固定回复
            self._log.exception(
                "action 执行异常：action=%s room=%s uid=%s",
                cmd.action,
                getattr(msg, "chat_id", ""),
                getattr(msg, "sender_id", ""),
            )
            return Reply(ERROR_TEXT, cmd.action)
        return reply

    # -- 功能开关（kv: feature.<engine>）-------------------------------------
    def _feature_off(self, engine: str, msg) -> bool:
        """读 kv 里的 `feature.<engine>`（按群作用域）；读不到按「开启」处理。

        读失败不能把玩法关掉——开关是运维手段，坏数据不该变成故障。
        """
        if self._store is None:
            return False
        room = getattr(msg, "chat_id", "") or ""
        if not isinstance(room, str) or not room:
            return False
        try:
            with self._store.tx(immediate=False) as conn:
                row = conn.execute(
                    "SELECT value FROM kv WHERE room = ? AND key = ?",
                    (room, f"feature.{engine}"),
                ).fetchone()
        except Exception:
            self._log.exception("读功能开关失败：engine=%s room=%s", engine, room)
            return False
        if row is None:
            return False
        value = row[0]
        return isinstance(value, str) and value.strip().lower() in _OFF_VALUES
