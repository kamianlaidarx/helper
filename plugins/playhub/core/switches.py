"""5 个高危社交开关：只在配置里开，不做群内命令。

为什么不是 47 个开关（2026-11-05 定案，`docs/design/playhub.md:1627`）：

- 「参数形状」已经是第一道闸：`结婚`/`纳妾`/`闺蜜`/`结拜`/`雇佣` 都是
  `needs_arg=True` + `bare_behavior="silent"`，裸发一个字的回复都没有；
  `宝宝` 强制子命令，`礼物` 要 `@`+物品，`配对` 要 `@`。
- 群内 `开启XX` 本身又是新的三字关键词，还要再叠一层权限门禁，只是把
  复杂度搬家；误触治理靠「整条消息精确等于关键词」+ 静默 + 50 条闲聊语料
  零回复的验收，不靠开关数量。

因此只对「会改关系状态且不可逆」和「会花大钱」的留开关，共 5 个键，
**默认全关**；其余 42 项默认全开（走 `app._feature_off` 的引擎级开关）。

取值顺序（2026-11-13 补齐配置面，面板「插件配置」即可开）：

1. `kv(room, 'feature.<键>')` —— 群内显式设置，有行就以它为准；
2. 配置 `bot_config.json → plugins.playhub.features`（= 面板「插件配置」里那段
   JSON），如 `{"features": {"marriage": true, "concubine": "on"}}`；
3. 都没有 → 关。

面板保存插件配置后框架会热重载本插件（`web_panel.plugin_action` 的 `configure`），
不需要重启机器人。

存储：`kv(room, 'feature.<键>', value)`，按群作用域。值语义与
`app._OFF_VALUES` 一致：`0/off/false/no/disabled` 视为关，其余非空值视为开；
**读不到按关**（与引擎级开关「读不到按开」相反——这正是本模块存在的理由）。
"""
from __future__ import annotations

import sqlite3

# 5 个键（顺序即帮助/文档里的展示顺序）
MARRIAGE = "marriage"      # 结婚 / 求婚 / 度蜜月 / 离婚（含 520、200 金币支出）
KINSHIP = "kinship"        # 闺蜜 / 结拜
EMPLOY = "employ"          # 雇佣
CONCUBINE = "concubine"    # 纳妾（永不做钻石扣款入口）
BABY = "baby"              # 宝宝（看 / 喂养 / 改名）

KEYS: tuple[str, ...] = (MARRIAGE, KINSHIP, EMPLOY, CONCUBINE, BABY)

LABELS: dict[str, str] = {
    MARRIAGE: "结婚",
    KINSHIP: "闺蜜与结拜",
    EMPLOY: "雇佣",
    CONCUBINE: "纳妾",
    BABY: "宝宝",
}

# 与 app._OFF_VALUES 同一套写法；刻意各存一份——app 那份属于引擎级开关，
# 语义不同（那边读不到按开），合并会让两处一起改错。
_OFF_VALUES = frozenset({"0", "off", "false", "no", "disabled"})

# 配置里的位置：`plugins.playhub.features`（面板「插件配置」的 JSON 就是这一段）
FEATURES_KEY = "features"


def kv_key(name: str) -> str:
    """把开关名转成 kv 键；未登记的键名直接拒绝（防拼写错误静默失效）。"""
    if name not in KEYS:
        raise ValueError(f"未登记的开关：{name!r}（只接受 {', '.join(KEYS)}）")
    return f"feature.{name}"


def _truthy(value: object) -> bool:
    """配置里的值 → 开关。布尔直取；字符串按 `_OFF_VALUES` 判；其余一律假。"""
    if type(value) is bool:
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        return bool(text) and text not in _OFF_VALUES
    return False


def parse_defaults(section: object) -> dict[str, bool]:
    """从插件配置（`bot_config.json → plugins.playhub`）读出 5 个开关的默认值。

    配置面：`{"features": {"marriage": true, "concubine": "on"}}`。
    未登记的键名忽略（配置写错不该让引擎崩），类型不对按关。
    """
    if not isinstance(section, dict):
        return {}
    raw = section.get(FEATURES_KEY)
    if not isinstance(raw, dict):
        return {}
    defaults: dict[str, bool] = {}
    for name in KEYS:
        if name in raw:
            defaults[name] = _truthy(raw[name])
    return defaults


def is_on(
    conn: sqlite3.Connection, room: str, name: str, *, default: bool = False
) -> bool:
    """该群这个开关是否打开。

    边界（三条，别合并）：
    1. `kv` 里有这一行 → 以它为准（群内显式设置优先于配置默认值）；
    2. 没有这一行 → 用 `default`（来自配置 `plugins.playhub.features`）；
    3. 读库抛错 / 没有群号 → 关（失败关闭）。
    """
    if not room:
        return False
    try:
        row = conn.execute(
            "SELECT value FROM kv WHERE room = ? AND key = ?", (room, kv_key(name))
        ).fetchone()
    except sqlite3.Error:
        return False
    if row is None:
        return bool(default)
    value = row[0]
    if not isinstance(value, str):
        return False
    value = value.strip().lower()
    return bool(value) and value not in _OFF_VALUES


def read_all(
    conn: sqlite3.Connection, room: str, *, defaults: dict[str, bool] | None = None
) -> dict[str, bool]:
    """一次读出 5 个开关的当前状态（`kv` 缺行时用 `defaults`，两者都没有即关）。"""
    defaults = defaults or {}
    return {name: is_on(conn, room, name, default=defaults.get(name, False)) for name in KEYS}
