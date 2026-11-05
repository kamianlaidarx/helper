"""playhub 触发词表：61 个整词 + 别名 + 启动期校验（`docs/design/playhub.md:609-632`、`§4` 主表）。

口径（与 §4 主表逐行对齐）：
- **43 个主词 + 18 个别名 = 61 个关键词**，全部 ≤3 字；四字词会被
  `wxbot_framework/text.py` 的 `FOUR_CHINESE` 当成接龙答案吞掉，所以一个都不注册。
- 表里只写「本批及后续批次要实现的功能」。action 名 = `<引擎>.<表内功能短名>`；
  本批已定案的三个是 `daily.signin` / `daily.fortune` / `ranking.top`，
  其余按 §4 的「引擎」列逐行对应，不自创引擎。
- 框架已有的 `提示` / `换一题` / `公布答案` / `退出猜成语` 以及
  `成语接龙` / `合并接龙` / `退出接龙` / `看图猜成语` **不在这里注册**。
"""
from __future__ import annotations

from dataclasses import dataclass

from wxbot_framework.text import FOUR_CHINESE

from .textkit import normalize


class KeywordConflict(Exception):
    """触发词表构建失败：重复、非归一化、四字词、超长。"""


@dataclass(frozen=True)
class Trigger:
    """一个整词到动作的绑定（字段与 §2.6.4 一致）。"""

    keyword: str                 # 归一化后的整词（构建期断言 normalize(k) == k）
    engine: str                  # 引擎名
    action: str                  # 动作 id，路由到 Engine.on_command
    needs_arg: bool = False
    bare_behavior: str = "run"   # "run" / "guide" / "silent"
    risk: str = "low"            # "low" / "mid" / "high"，与 §4.3 联动
    owner: str = "playhub"       # "playhub" / "framework"

    @property
    def feature(self) -> str:
        """功能名：冲突信息里报给运维看的稳定标识。"""
        return self.action


# §4 总表的 18 个别名（主词之外的另一列），用于词账断言。
ALIASES = frozenset({
    "打卡", "点唱", "求签", "比武", "老黄历", "送礼", "求婚", "运势", "缘分",
    "抽盲盒", "情话", "榜单", "猜歌", "猜谜", "拜财神", "宝箱", "找不同", "邀请榜",
})

# ---------------------------------------------------------------------------
# 60 个词，按 §4 主表行序排列（同一行的主词在前、别名在后）。
# 需参数 / 误触等级 / 裸触发策略逐行取自「需参数」「误触」两列，裸触发按 §2.6.6：
# 高 → silent；中 → guide；低且需参数 → guide；低且不需参数 → run。
# ---------------------------------------------------------------------------
TRIGGERS: tuple[Trigger, ...] = (
    # 1 签到 / 打卡
    Trigger("签到", "daily", "daily.signin", risk="low"),
    Trigger("打卡", "daily", "daily.signin", risk="low"),
    # 2 点歌 / 点唱
    Trigger("点歌", "music", "music.request", needs_arg=True, bare_behavior="guide", risk="low"),
    Trigger("点唱", "music", "music.request", needs_arg=True, bare_behavior="guide", risk="low"),
    # 3 抽签 / 求签
    Trigger("抽签", "daily", "daily.fortune", risk="low"),
    Trigger("求签", "daily", "daily.fortune", risk="low"),
    # 4 切磋 / 比武
    Trigger("切磋", "duel", "duel.challenge", needs_arg=True, bare_behavior="guide", risk="low"),
    Trigger("比武", "duel", "duel.challenge", needs_arg=True, bare_behavior="guide", risk="low"),
    # 5 黄历 / 老黄历
    Trigger("黄历", "daily", "daily.almanac", risk="low"),
    Trigger("老黄历", "daily", "daily.almanac", risk="low"),
    # 6 礼物 / 送礼
    Trigger("礼物", "social", "social.gift", needs_arg=True, bare_behavior="silent", risk="high"),
    Trigger("送礼", "social", "social.gift", needs_arg=True, bare_behavior="silent", risk="high"),
    # 7 结婚 / 求婚
    Trigger("结婚", "social", "social.marry", needs_arg=True, bare_behavior="silent", risk="high"),
    Trigger("求婚", "social", "social.marry", needs_arg=True, bare_behavior="silent", risk="high"),
    # 8 宝宝
    Trigger("宝宝", "social", "baby.view", needs_arg=True, bare_behavior="silent", risk="high"),
    # 9 闺蜜
    Trigger("闺蜜", "social", "social.sister", needs_arg=True, bare_behavior="silent", risk="high"),
    # 10 纳妾
    Trigger("纳妾", "social", "social.concubine", needs_arg=True, bare_behavior="silent", risk="high"),
    # 11 雇佣
    Trigger("雇佣", "social", "social.employ", needs_arg=True, bare_behavior="silent", risk="high"),
    # 12 结拜
    Trigger("结拜", "social", "social.brother", needs_arg=True, bare_behavior="silent", risk="high"),
    # 13 天气
    Trigger("天气", "outbound", "outbound.weather", needs_arg=True, bare_behavior="silent", risk="high"),
    # 14 星座 / 运势
    Trigger("星座", "daily", "daily.horoscope", bare_behavior="guide", risk="mid"),
    Trigger("运势", "daily", "daily.horoscope", bare_behavior="guide", risk="mid"),
    # 15 配对 / 缘分
    Trigger("配对", "social", "social.match", needs_arg=True, bare_behavior="silent", risk="high"),
    Trigger("缘分", "social", "social.match", needs_arg=True, bare_behavior="silent", risk="high"),
    # 16 盲盒 / 抽盲盒
    Trigger("盲盒", "lottery", "lottery.blindbox", risk="low"),
    Trigger("抽盲盒", "lottery", "lottery.blindbox", risk="low"),
    # 17 文案 / 情话
    Trigger("文案", "lounge", "lounge.copy", bare_behavior="guide", risk="mid"),
    Trigger("情话", "lounge", "lounge.copy", bare_behavior="guide", risk="mid"),
    # 18 活跃榜
    Trigger("活跃榜", "ranking", "ranking.activity", risk="low"),
    # 19 排行榜 / 榜单（本批唯一已绑定入口）
    Trigger("排行榜", "ranking", "ranking.top", risk="low"),
    Trigger("榜单", "ranking", "ranking.top", risk="low"),
    # 20-30 竞猜 11 类
    Trigger("猜成语", "quiz", "quiz.chengyu", risk="low"),
    Trigger("猜歌名", "quiz", "quiz.song", risk="low"),
    Trigger("猜歌", "quiz", "quiz.song", risk="low"),
    Trigger("猜谜语", "quiz", "quiz.riddle", risk="low"),
    Trigger("猜谜", "quiz", "quiz.riddle", risk="low"),
    Trigger("猜电影", "quiz", "quiz.movie", risk="low"),
    Trigger("猜明星", "quiz", "quiz.star", risk="low"),
    Trigger("猜动物", "quiz", "quiz.animal", risk="low"),
    Trigger("猜植物", "quiz", "quiz.plant", risk="low"),
    Trigger("猜美食", "quiz", "quiz.food", risk="low"),
    Trigger("猜王者", "quiz", "quiz.honor", risk="low"),
    Trigger("猜动漫", "quiz", "quiz.anime", risk="low"),
    Trigger("猜国漫", "quiz", "quiz.guoman", risk="low"),
    # 31 关系卡
    Trigger("关系卡", "social", "social.card", risk="low"),
    # 32 求财神 / 拜财神
    Trigger("求财神", "daily", "daily.wealth", risk="low"),
    Trigger("拜财神", "daily", "daily.wealth", risk="low"),
    # 33 小黑屋
    Trigger("小黑屋", "moderation", "moderation.jail", needs_arg=True, bare_behavior="guide", risk="low"),
    # 34 黑名单
    Trigger("黑名单", "moderation", "moderation.block", needs_arg=True, bare_behavior="guide", risk="low"),
    # 35 开宝箱 / 宝箱
    Trigger("开宝箱", "lottery", "lottery.chest", risk="low"),
    Trigger("宝箱", "lottery", "lottery.chest", risk="low"),
    # 36 群空间
    Trigger("群空间", "lounge", "lounge.space", risk="low"),
    # 38 找茬 / 找不同
    Trigger("找茬", "quiz", "quiz.diff", risk="low"),
    Trigger("找不同", "quiz", "quiz.diff", risk="low"),
    # 39 问答
    Trigger("问答", "quiz", "quiz.trivia", risk="low"),
    # 41 潜水
    Trigger("潜水", "groupstat", "groupstat.dive", risk="low"),
    # 42 邀请 / 邀请榜
    Trigger("邀请", "groupstat", "groupstat.invite", risk="low"),
    Trigger("邀请榜", "groupstat", "groupstat.invite", risk="low"),
    # 45 改名（群管理侧开关，与「宝宝 改名 X」无关）
    Trigger("改名", "groupstat", "groupstat.rename", needs_arg=True, bare_behavior="guide", risk="low"),
    # 47 聊天
    Trigger("聊天", "lounge", "lounge.chat", needs_arg=True, bare_behavior="guide", risk="mid"),
    # 帮助（第 1 批 A 案新增：独立主词，不与各插件各自的 `/help` 抢通道）
    Trigger("帮助", "business", "business.help", risk="low"),
)

# 词账：43 主词 + 18 别名 = 61（§4「触发词账」+ 第 1 批新增的 `帮助`）。
PRIMARY_COUNT = 43
ALIAS_COUNT = 18
KEYWORD_COUNT = 61


@dataclass(frozen=True)
class KeywordTable:
    """O(1) 整词查表。"""

    head_index: dict[str, Trigger]
    by_action: dict[str, tuple[Trigger, ...]]

    @property
    def keywords(self) -> tuple[str, ...]:
        return tuple(self.head_index)

    def trigger(self, keyword: str) -> Trigger | None:
        return self.head_index.get(keyword)


def _reject(message: str) -> None:
    raise KeywordConflict(message)


def build_table(triggers: tuple[Trigger, ...] = TRIGGERS) -> KeywordTable:
    """构建关键词表，跑四道校验；任何一道都不静默降级。"""
    seen: dict[str, Trigger] = {}
    by_action: dict[str, list[Trigger]] = {}

    for t in triggers:
        # 1. 归一化幂等：表里写了带空格/零宽的词，说明有人手写时没走 normalize。
        if normalize(t.keyword) != t.keyword:
            _reject(f"触发词未归一化：{t.keyword!r} → {normalize(t.keyword)!r}（{t.feature}）")

        # 2. 全局唯一：错误信息里带两个功能名。
        old = seen.get(t.keyword)
        if old is not None:
            _reject(
                f"触发词冲突：{t.keyword!r} 同时属于 {old.feature} 与 {t.feature}"
            )

        # 3. 四字排除：四字中文会被 idiom_chain 当接龙答案吞掉。
        if t.owner == "playhub" and FOUR_CHINESE.fullmatch(t.keyword):
            _reject(f"四字词不得注册：{t.keyword!r}（{t.feature}）")

        # 4. 长度上限（本批硬约束：全部 ≤3 字）。
        if t.owner == "playhub" and len(t.keyword) > 3:
            _reject(f"触发词超过 3 字：{t.keyword!r}（{t.feature}）")

        seen[t.keyword] = t
        by_action.setdefault(t.action, []).append(t)

    if len(triggers) != KEYWORD_COUNT:
        _reject(f"关键词数量应为 {KEYWORD_COUNT}，实际 {len(triggers)}")
    primary = sum(1 for t in triggers if t.keyword not in ALIASES)
    aliases = sum(1 for t in triggers if t.keyword in ALIASES)
    if (primary, aliases) != (PRIMARY_COUNT, ALIAS_COUNT):
        _reject(f"词账不符：主词 {primary}/{PRIMARY_COUNT}，别名 {aliases}/{ALIAS_COUNT}")

    return KeywordTable(
        head_index=dict(seen),
        by_action={action: tuple(items) for action, items in by_action.items()},
    )


# 导入即构建：路由与 registry 共用这一张表。
TABLE = build_table()
