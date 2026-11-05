"""playhub 动作绑定表 + 加载期自检 + dispatch（`docs/design/playhub.md:578`、`§7 第 1 批`）。

第 2 批把 13 个竞猜动作全部绑到 `quiz` 引擎：
`quiz.chengyu` / `song` / `riddle` / `movie` / `star` / `animal` / `plant` / `food`
/ `honor` / `anime` / `guoman` / `diff` / `trivia`。
第 3 批起接 `social` 引擎：婚姻 + 闺蜜/结拜/雇佣/纳妾 + 关系卡/配对/礼物/宝宝
（宝宝与关系卡同属社交面，共用 `pair` / `baby` 两张表，不另开引擎）。

未绑定的 action 一律静默 `return None`：不回「未开放」、不抛异常、不写日志噪音
—— 与「关键词未命中」对外表现完全一致。

自检的开关是 `check_bindings(allow_unbound=...)` 与 `bindings=...` 注入：
- 默认 `allow_unbound=True`：只要求**本批（batch=CURRENT_BATCH）**的动作都已绑定，
  后续批次的词可以暂时悬空；
- `allow_unbound=False`：任何未绑定动作都报错，用于全量发布前的严格校验；
- 注入 `bindings`：测试可构造「故意缺绑定」的现场验证报错内容。
"""
from __future__ import annotations

from dataclasses import dataclass

from .keywords import TRIGGERS, Trigger

CURRENT_BATCH = 3


class BindingError(Exception):
    """动作绑定自检失败：本批动作没有绑定，或 action 未在计划表登记。"""


@dataclass(frozen=True)
class Binding:
    """一个已绑定的动作；`batch` 必须是已实现批次。"""

    action: str
    engine: str
    batch: int | None


# ---------------------------------------------------------------------------
# 绑定表：第 1 批 4 个动作 + 第 2 批 13 个竞猜动作。
# 新增绑定必须同时改这里与 ACTION_PLAN，否则导入即自检失败。
# ---------------------------------------------------------------------------
BINDINGS: dict[str, Binding] = {
    "daily.signin": Binding("daily.signin", "daily", 1),
    "daily.fortune": Binding("daily.fortune", "daily", 1),
    "ranking.top": Binding("ranking.top", "ranking", 1),
    # 帮助：不经引擎，由 app 直接回包（见 app.HELP_TEXT）。
    "business.help": Binding("business.help", "business", 1),
    # 第 2 批：13 个类目共用一个 quiz 引擎、一张 quiz_round 表。
    "quiz.chengyu": Binding("quiz.chengyu", "quiz", 2),
    "quiz.song": Binding("quiz.song", "quiz", 2),
    "quiz.riddle": Binding("quiz.riddle", "quiz", 2),
    "quiz.movie": Binding("quiz.movie", "quiz", 2),
    "quiz.star": Binding("quiz.star", "quiz", 2),
    "quiz.animal": Binding("quiz.animal", "quiz", 2),
    "quiz.plant": Binding("quiz.plant", "quiz", 2),
    "quiz.food": Binding("quiz.food", "quiz", 2),
    "quiz.honor": Binding("quiz.honor", "quiz", 2),
    "quiz.anime": Binding("quiz.anime", "quiz", 2),
    "quiz.guoman": Binding("quiz.guoman", "quiz", 2),
    "quiz.diff": Binding("quiz.diff", "quiz", 2),
    "quiz.trivia": Binding("quiz.trivia", "quiz", 2),
    # 第 3 批：社交段（婚姻 + 闺蜜/结拜/雇佣/纳妾，共用 pair 状态机）。
    "social.marry": Binding("social.marry", "social", 3),
    "social.sister": Binding("social.sister", "social", 3),
    "social.brother": Binding("social.brother", "social", 3),
    "social.employ": Binding("social.employ", "social", 3),
    "social.concubine": Binding("social.concubine", "social", 3),
    # 第 3 批：关系卡 / 配对 / 礼物 / 宝宝（同一个 social 引擎，宝宝也放在这里）。
    "social.card": Binding("social.card", "social", 3),
    "social.match": Binding("social.match", "social", 3),
    "social.gift": Binding("social.gift", "social", 3),
    "baby.view": Binding("baby.view", "social", 3),
}

# ---------------------------------------------------------------------------
# 43 个动作的计划：值为批次号，`None` 表示尚未排期。
# 与 `keywords.py` 的 43 个 action 一一对应，供后续批次对照与自检放行。
# ---------------------------------------------------------------------------
ACTION_PLAN: dict[str, int | None] = {
    "daily.signin": 1,
    "daily.fortune": 1,
    "ranking.top": 1,
    "business.help": 1,
    "music.request": None,
    "duel.challenge": None,
    "daily.almanac": None,
    "social.gift": 3,
    "social.marry": 3,
    "baby.view": 3,
    "social.sister": 3,
    "social.concubine": 3,
    "social.employ": 3,
    "social.brother": 3,
    "outbound.weather": None,
    "daily.horoscope": None,
    "social.match": 3,
    "lottery.blindbox": None,
    "lounge.copy": None,
    "ranking.activity": None,
    "quiz.chengyu": 2,
    "quiz.song": 2,
    "quiz.riddle": 2,
    "quiz.movie": 2,
    "quiz.star": 2,
    "quiz.animal": 2,
    "quiz.plant": 2,
    "quiz.food": 2,
    "quiz.honor": 2,
    "quiz.anime": 2,
    "quiz.guoman": 2,
    "social.card": 3,
    "daily.wealth": None,
    "moderation.jail": None,
    "moderation.block": None,
    "lottery.chest": None,
    "lounge.space": None,
    "quiz.diff": 2,
    "quiz.trivia": 2,
    "groupstat.dive": None,
    "groupstat.invite": None,
    "groupstat.rename": None,
    "lounge.chat": None,
}


def check_bindings(
    *,
    triggers: tuple[Trigger, ...] = TRIGGERS,
    bindings: dict[str, Binding] | None = None,
    plan: dict[str, int | None] | None = None,
    allow_unbound: bool = True,
) -> int:
    """加载期自检：返回绑定条目数，失败抛 `BindingError` 并列出**全部**缺失项。

    - `action` 不在 `plan` 里 → 无论开关如何都报错（关键词指向了没排期的动作，
      说明词表与计划表脱节，不能靠「未排期」蒙混过去）。
    - `action` 属于本批（`plan[action] == CURRENT_BATCH`）却没绑定 → 报错。
    - `allow_unbound=False` 时，任何未绑定动作都报错。
    """
    bindings = BINDINGS if bindings is None else bindings
    plan = ACTION_PLAN if plan is None else plan

    unplanned = sorted({t.action for t in triggers if t.action not in plan})
    if unplanned:
        raise BindingError(f"动作未在 ACTION_PLAN 登记：{', '.join(unplanned)}")

    grouped: dict[str, list[str]] = {}
    for t in triggers:
        if t.action in bindings:
            continue
        if allow_unbound and plan[t.action] != CURRENT_BATCH:
            continue
        grouped.setdefault(t.action, []).append(t.keyword)

    if grouped:
        detail = "；".join(
            f"{action}（触发词：{'/'.join(words)}）" for action, words in grouped.items()
        )
        raise BindingError(f"本批动作缺少绑定（{len(grouped)} 项）：{detail}")

    return len(bindings)


def dispatch(
    engine,
    action: str,
    cmd,
    msg=None,
    ctx=None,
    game=None,
    *,
    bindings: dict[str, Binding] | None = None,
):
    """把命令派给引擎；未绑定动作、引擎不匹配、引擎没有 handler 时静默返回 `None`。

    `engine` 可以是带 `name` / `on_command` 的引擎实例，也可以是引擎名字符串
    （字符串没有 handler，等于未接线，同样静默）。
    """
    table = BINDINGS if bindings is None else bindings
    binding = table.get(action)
    if binding is None:
        return None

    name = engine if isinstance(engine, str) else getattr(engine, "name", None)
    if name != binding.engine:
        return None

    handler = getattr(engine, "on_command", None)
    if handler is None:
        return None
    return handler(cmd, msg, ctx, game)


# 导入即自检：本批动作漏绑 → 插件加载直接失败，不用运行期静默掩盖。
check_bindings()
