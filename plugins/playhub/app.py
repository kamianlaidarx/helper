"""playhub 编排层：`on_message → 归一化 → route → registry.dispatch → Reply`
（`docs/design/playhub.md:475-495` §2.5、`:517-594` §2.6）。

本批的三条硬规矩：

1. **只做 O(1) 内存活**：判 `is_self` → `route()`（内部归一化）→ 整词查表 →
   一次短事务 → 返回 `Reply`。回调里没有 HTTP、没有 LLM、没有图片下载。
2. **本批不建慢活执行器**：`adapters/executor.py` 属第 3 批。这里直接构造 `Reply`
   交给调用方发送，**不在事务内发送**（事务边界在引擎里，发送在 `Reply` 返回之后）。
3. **零噪音**：非关键词、未绑定动作、`bare_behavior="silent"` 的裸触发、功能开关关闭，
   一律 `return None`——不回复、不写日志、不写审计。

第 2 批在 `route()` 返回 `None` 之后接了一条**纯文本通路**（`_plain_text`），
供竞猜抢答使用：代价是一条按 `quiz_one_open` 索引的读查询，命中不了对局就立即返回。

事务顺序由引擎保证：`BEGIN IMMEDIATE` → 校验 → 发币/改状态 → 流水 → `COMMIT` → 构造 `Reply`。
"""
from __future__ import annotations

import logging

from .core import activity, clock, idem, render, switches
from .core.menus import menu_for
from .engines import GameRuntime, Reply, build_engines, msg_nickname
from .engines import moderation
from .keywords import TABLE
from .registry import BINDINGS, dispatch
from .router import route

# 动作 → 高危开关（`core/switches.py`）。不在这张表里的动作不受开关约束。
# 只在配置里开、不做群内命令：见 switches 模块 docstring。
ACTION_SWITCH: dict[str, str] = {
    "social.marry": switches.MARRIAGE,
    "social.sister": switches.KINSHIP,
    "social.brother": switches.KINSHIP,
    "social.employ": switches.EMPLOY,
    "social.concubine": switches.CONCUBINE,
    "baby.view": switches.BABY,
}

# 帮助文案。§1 清单把它排在 `business.py`（网页可编辑、8 段上限），
# 但本批交付文件清单里没有 business.py，所以暂放这里，形状按 `§4.5` 的小V 总览卡写；
# business.py 落地后整段搬走，`help_text()` 改为读网页配置即可。
#
# 分组只决定「先看哪一行」，不决定开关：开关在 `core/switches.py`，
# 默认关闭的玩法在帮助里照列，并在行尾标 `（默认关闭）`——藏起来等于让用户猜。
_HELP_GROUPS: tuple[tuple[str, str], ...] = (
    ("💰", "签到 / 🎋 抽签 — 每天的固定收益，连签和签档都会加码"),
    ("🏆", "排行榜 — 本群金币榜，发「排行榜 金币」也认"),
    ("🀄", "竞猜 — 猜成语 / 猜歌名 / 猜谜语 / 猜电影 / 猜明星 / 猜动物 / 猜植物 / 猜美食 / "
           "猜王者 / 猜动漫 / 猜国漫 / 找茬 / 问答"),
    ("💘", "关系 — 结婚 / 闺蜜 / 结拜 / 雇佣 / 纳妾（默认关闭，需管理员在配置里开）"),
    ("🎁", "配套 — 礼物 / 配对 / 关系卡 / 宝宝"),
    ("🎮", "娱乐 — 点歌 / 切磋 / 盲盒 / 开宝箱 / 文案 / 群空间 / 聊天"),
    ("📅", "信息 — 天气 / 黄历 / 星座 / 求财神"),
    ("📊", "群务 — 活跃榜 / 潜水 / 邀请 / 改名 / 小黑屋 / 黑名单"),
)


def help_text() -> str:
    """总览卡（小V 的 `💡使用指引` 形状）：装饰头 → 分组行 → 用法注释 → 已知限制。"""
    rows = [render.head(icon, text) for icon, text in _HELP_GROUPS]
    rows.append(render.rule())
    rows.append(render.note("把某个关键词单独发一次，我就回一张这个玩法的说明书；"
                            "命令前后不加标点"))
    rows.append("💡 出题抢答：发一个词就出题，直接发答案抢答；「提示」看提示、"
                "「公布答案」看答案、「换一题」换一题、「结束」收局")
    rows.append("🔧 已知限制：对局状态存在内存里，机器人重启会丢；接龙开局期间群里"
                "非 / 开头的文本都会被当成接龙答案")
    text = render.Card("✨", "玩法总览", tuple(rows)).render()
    return render.limit(text, max_lines=24, max_chars=1800)


HELP_TEXT = help_text()

ERROR_TEXT = "这个玩法暂时出错了"


def _nickname(msg, ctx) -> str:
    """取发送者昵称，取不到就空串（`member_seen.nickname` 允许空，空不覆盖旧值）。"""
    try:
        return msg_nickname(msg, ctx)
    except Exception:
        return ""

# kv 里 feature.<engine> 的值按这些写法视为「关闭」（§2.5：关闭时静默）
_OFF_VALUES = frozenset({"0", "off", "false", "no", "disabled"})



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
        self._now = now_fn if now_fn is not None else clock.now_ts

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

        # 小黑屋 / 黑名单（§8.2）：静默——不响应命令、不参与抢答、不进排行、不记活跃。
        # 排在 route 之前：连「他这个人在不在线」都不记，才算真的不进榜。
        if self._muted(ctx, msg):
            return None

        # route() 内部已做 NFKC/零宽/空白折叠（textkit.normalize_with_mentions），
        # 这里不重复归一化，避免两次归一化口径不一致。
        cmd = route(text)
        if cmd is None:
            # 纯文本：先记一条活跃（§3.4「命令消息不重复计入」），再看是不是竞猜抢答。
            self._touch(ctx, msg, text)
            # 关键词优先（§2.6 第 6 条）：没命中任何关键词才可能是竞猜抢答的答案。
            return self._plain_text(ctx, msg)

        trigger = TABLE.trigger(cmd.name)
        if trigger is None:                     # 理论上 route 已保证命中，防御性返回
            return None

        binding = BINDINGS.get(cmd.action)
        if binding is None:
            return None                         # 未绑定动作：与「没命中」对外一致，静默
        # 帮助不经引擎：文案随 app 走，避免为了一个静态回包再拉一层引擎。
        # 放在 feature 开关之前：关闭某个玩法不该连「帮助」一起哑掉。
        if cmd.action == "business.help":
            return Reply(help_text(), cmd.action)

        if self._feature_off(binding.engine, msg):
            return None                         # feature.<engine> 关闭：静默

        if self._switch_off(cmd.action, msg):
            return None                         # 高危功能未在本群开启：静默

        if not cmd.had_arg:
            # 裸发分两种，按 §4.5 卡面规范：
            # - 需要参数 / 引导型（结婚、闺蜜、天气…）→ 回一张说明书卡，
            #   对应小V 的「群内发 XX 查看」；
            # - 不需要参数的（签到、抽签、竞猜、排行榜）→ 照常执行，卡片会挡住真正的动作。
            # 卡面排在 feature/高危开关之后：开关没开就什么都不回，不泄露玩法存在。
            card = None
            if trigger.needs_arg or trigger.bare_behavior == "guide":
                card = menu_for(cmd.action)
            if card is not None:
                return Reply(card, cmd.action)
            if trigger.bare_behavior == "silent":
                return None                     # 高风险裸触发且没有卡：静默（§2.6.6）

        engine = self._engines.get(binding.engine)
        if engine is None:
            return None                         # 引擎没接线（后续批次）：静默

        # §4.2.10 消息级幂等：所有静默分支都走完之后才 claim，先 claim 后执行。
        # 重复投递直接返回 None（不回复、不改状态）；claim 失败（写锁超时等）
        # 也按「不执行」处理——宁可这条没处理，也不能在身份不明时改一次钱。
        if not self._claim(msg, cmd.action):
            return None

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

    # -- 消息级幂等（§4.2.10）-----------------------------------------------
    def _claim(self, msg, action: str) -> bool:
        """占下这条消息的幂等键；False 表示重复投递或占不下。

        三种结果分开处理：编不出键（无 native id、无 session/sequence）时照常执行；
        重复时静默返回；claim 自身出错（写锁超时、库坏）时**不执行**——身份不明
        时宁可少做一次，也不能多扣一次钱。claim 成功后业务失败不释放（至多一次）。
        """
        if self._store is None:
            return True
        key = idem.message_key(msg, action)
        if key is None:
            return True
        try:
            claimed = idem.claim(self._store, key, scope=action, now=self._now())
        except Exception:
            self._log.warning(
                "幂等键写入失败，本条不执行：room=%s action=%s",
                getattr(msg, "chat_id", ""),
                action,
            )
            return False
        if not claimed:
            self._log.debug(
                "重复投递已忽略：room=%s action=%s",
                getattr(msg, "chat_id", ""),
                action,
            )
        return claimed

    # -- 纯文本入口（竞猜抢答 + 四个框架子命令词）---------------------------
    def _plain_text(self, ctx, msg) -> Reply | None:
        """关键词没命中时的第二条通路，第 2 批只有 `quiz` 引擎会用。

        三条约束：
        - 引擎自己判断「本群有没有未结束对局」，没有就立刻返回，不回任何东西；
        - 错答/闲聊静默，异常也**不回** `ERROR_TEXT`——群里每句闲聊都冒一句"出错"
          比不实现更糟；
        - 与命令分支共用 `feature.quiz` 开关。
        """
        engine = self._engines.get("quiz")
        if engine is None:
            return None
        handler = getattr(engine, "on_plain_text", None)
        if not callable(handler):
            return None
        if self._feature_off("quiz", msg):
            return None
        try:
            return handler(msg, ctx, self._game)
        except Exception:
            self._log.exception(
                "纯文本处理异常：room=%s uid=%s",
                getattr(msg, "chat_id", ""),
                getattr(msg, "sender_id", ""),
            )
            return None

    # -- 小黑屋 / 黑名单（静默闸）-------------------------------------------
    def _muted(self, ctx, msg) -> bool:
        """成员是否在小黑屋或黑名单里（§8.2）；读失败按「不在」处理。

        为什么失败**开放**：这里挂掉会把全群所有人一起静音——比漏放一个被关的人
        严重得多。真正的权限判定（关门/关门解除）在 `engines/moderation.py`，
        那边是失败关闭。
        """
        if self._store is None:
            return False
        room = getattr(msg, "chat_id", "") or ""
        uid = getattr(msg, "sender_id", "") or ""
        if not isinstance(room, str) or not room or not isinstance(uid, str) or not uid:
            return False
        now = self._now()
        try:
            with self._store.tx(immediate=False) as conn:
                return moderation.is_jailed(conn, room, uid, now) or moderation.is_blocked(conn, room, uid)
        except Exception:
            self._log.exception("读小黑屋/黑名单失败：room=%s uid=%s", room, uid)
            return False

    # -- 活跃计数（§3.4）-----------------------------------------------------
    def _touch(self, ctx, msg, text: str) -> None:
        """记一条活跃。只记**人话**：命令、机器人自己的消息都到不了这里。

        计数失败只记 debug 日志：一条没记上会让活跃榜少一条，但不该让聊天卡住。
        """
        if self._store is None:
            return
        room = getattr(msg, "chat_id", "") or ""
        uid = getattr(msg, "sender_id", "") or ""
        if not isinstance(room, str) or not room or not isinstance(uid, str) or not uid:
            return
        now = self._now()
        try:
            with self._store.tx() as conn:
                activity.record(
                    conn,
                    room=room,
                    uid=uid,
                    day=clock.day_of(now),
                    now=now,
                    nickname=_nickname(msg, ctx),
                    chars=len(text),
                )
        except Exception:
            self._log.debug("活跃计数失败：room=%s uid=%s", room, uid, exc_info=True)

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

    # -- 高危开关（kv: feature.<键>，5 个，默认关）---------------------------
    def _switch_off(self, action: str, msg) -> bool:
        """动作受高危开关约束时，判断它在本群是否**未开启**。

        与 `_feature_off` 的关键差别：读不到按**关**（失败关闭）——这些是
        「改关系状态 + 花大钱」的功能，坏数据和缺配置都不该让它们自动打开。
        """
        name = ACTION_SWITCH.get(action)
        if name is None:
            return False
        if self._store is None:
            return True
        room = getattr(msg, "chat_id", "") or ""
        if not isinstance(room, str) or not room:
            return True
        try:
            with self._store.tx(immediate=False) as conn:
                return not switches.is_on(conn, room, name)
        except Exception:
            self._log.exception("读高危开关失败：switch=%s room=%s", name, room)
            return True
