"""物品字典与背包（第 3 批）：`item_def` 的种子数据 + `inventory` 的读写。

设计里 `item_def` 的价格是「由物品表定，见 §9」（`docs/design/playhub.md:1385`），
§9 未定，所以这里的礼价是**落地口径**，写死在本文件、由 `seed()` 灌进 `item_def`
（`world='*'` 全局默认行）：

| 礼物 | 金币 |
|---|---|
| 玫瑰 | 20 |
| 巧克力 | 50 |
| 蛋糕 | 88 |
| 烟花 | 200 |
| 戒指 | 520 |

定这几档的理由：签到每天 30–80 金币，玫瑰 20 是「随手能送」的档，戒指 520
与度蜜月同价、是「攒一周」的档；不设 1314 这种量级，否则等于死内容
（与「清求婚次数从 100 钻降到 15 钻」同一条理由，见 §4.4）。

`礼物` 的两种取物方式（设计未写，落地口径）：
1. 背包里有 → 直接取 1 件，**不花钱**；
2. 背包里没有、但 `item_def.price` 非空 → **代买**（当场扣金币），因为 v1
   还没有盲盒 / 开宝箱（`lottery` 引擎属本批后续），没有别的物品来源，
   否则「礼物」这一项在 v1 完全不可用。

本模块只碰 `item_def` / `inventory` 两张表；余额一律经 `core/wallet.py`，
扣款发生在调用方，不在这里。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ..textkit import normalize


@dataclass(frozen=True)
class Gift:
    """一件可送的礼物；`price` 为 0 表示非卖品（只能靠抽奖得到）。"""

    item_id: str
    name: str
    price: int
    icon: str = "🎁"


#: v1 礼价表（落地口径，见模块 docstring）。
GIFTS: tuple[Gift, ...] = (
    Gift("rose", "玫瑰", 20, "🌹"),
    Gift("choc", "巧克力", 50, "🍫"),
    Gift("cake", "蛋糕", 88, "🎂"),
    Gift("firework", "烟花", 200, "🎆"),
    Gift("ring", "戒指", 520, "💍"),
)

KIND_GIFT = "gift"
KIND_BOX = "box"


@dataclass(frozen=True)
class Chest:
    """一种宝箱：`coin`/`diamond` 是开启奖励，`diamond_odds` 是钻石命中概率的分母。"""

    item_id: str
    name: str
    short: str
    coin: int
    diamond: int
    diamond_odds: int
    icon: str = "🎁"


#: 箱子三型（数值来自 §4.4「宝箱三型」表，属**已定案**数值）。
CHESTS: tuple[Chest, ...] = (
    Chest("chest_bronze", "铜箱", "铜", 50, 1, 20, "🥉"),
    Chest("chest_silver", "银箱", "银", 150, 1, 12, "🥈"),
    Chest("chest_gold", "金箱", "金", 400, 2, 8, "🥇"),
)

CHEST_BRONZE, CHEST_SILVER, CHEST_GOLD = (c.item_id for c in CHESTS)

_BY_ID = {g.item_id: g for g in GIFTS}
_BY_NAME = {normalize(g.name): g for g in GIFTS}
_CHEST_BY_ID = {c.item_id: c for c in CHESTS}
_CHEST_BY_SHORT = {normalize(c.short): c for c in CHESTS}
_CHEST_BY_NAME = {normalize(c.name): c for c in CHESTS}
_CHEST_BY_SHORT.update({normalize(c.name[:-1]): c for c in CHESTS})

#: 盲盒掉落表：`(item_id, weight)`。权重按「越便宜越常出」排，合计 100。
#: 设计只定了盲盒的**次数价格**（5 钻/次，日限 2），没定掉落表，这里是落地口径；
#: 门槛是「期望价值低于自购」——玫瑰 40% + 巧克力 25% 已经把 5 钻的期望压到 ≈82 金币，
#: 低于戒指 520 的自购价，不至于让「买次数」变成稳赚的金币洗出通道。
BLIND_BOX_ID = "blind_box_v1"
BLIND_BOX_LOOT: tuple[tuple[str, int], ...] = (
    ("rose", 40),
    ("choc", 25),
    ("cake", 18),
    ("firework", 12),
    ("ring", 5),
)


def gift_price_text() -> str:
    """`礼物` 卡/用法里那行价目表。"""
    return " / ".join(f"{g.name} {g.price}" for g in GIFTS)


def seed(conn: sqlite3.Connection) -> None:
    """把 v1 礼物与三种宝箱灌进 `item_def`（幂等：已存在的行不覆盖，便于群里改价）。"""
    conn.executemany(
        """
        INSERT INTO item_def (world, item_id, kind, name, rarity, price, stackable, weight, enabled)
        VALUES ('*', ?, ?, ?, 1, ?, 1, 10, 1)
        ON CONFLICT(world, item_id) DO NOTHING
        """,
        [(g.item_id, KIND_GIFT, g.name, g.price) for g in GIFTS],
    )
    # 箱子不是卖品（price=NULL）：只能靠签到/活跃榜发，不能在群里买。
    conn.executemany(
        """
        INSERT INTO item_def (world, item_id, kind, name, rarity, price, stackable, weight, enabled)
        VALUES ('*', ?, ?, ?, ?, NULL, 1, 10, 1)
        ON CONFLICT(world, item_id) DO NOTHING
        """,
        [(c.item_id, KIND_BOX, c.name, 2 + i) for i, c in enumerate(CHESTS)],
    )


def chest_price_text() -> str:
    """`开宝箱` 用法里那行「铜 50 / 银 150 / 金 400」。"""
    return " / ".join(f"{c.short} {c.coin}" for c in CHESTS)


def find_chest(text: str) -> Chest | None:
    """按短名（铜/银/金）或全名（铜箱/银箱/金箱）认箱子；认不出返回 None。"""
    key = normalize(text or "")
    if not key:
        return None
    return _CHEST_BY_SHORT.get(key) or _CHEST_BY_NAME.get(key) or _CHEST_BY_ID.get(key)


def chest_of(item_id: str) -> Chest | None:
    return _CHEST_BY_ID.get(item_id)


def gift_of(item_id: str) -> Gift | None:
    return _BY_ID.get(item_id)


def roll_blind_box(rng) -> Gift:
    """按 `BLIND_BOX_LOOT` 的权重抽一件礼物；`rng` 需有 `randrange`。"""
    total = sum(w for _, w in BLIND_BOX_LOOT)
    pick = rng.randrange(total)
    for item_id, weight in BLIND_BOX_LOOT:
        if pick < weight:
            return _BY_ID[item_id]
        pick -= weight
    return _BY_ID[BLIND_BOX_LOOT[-1][0]]


def find_by_name(conn: sqlite3.Connection, text: str) -> Gift | None:
    """按名字（归一化）或 `item_id` 找一件礼物；认不出返回 None。

    先认本模块的 v1 表，认不到再查 `item_def` 里同名的 `gift` 行——这样群里
    手工往 `item_def` 加礼物也能送，不必改代码。
    """
    key = normalize(text or "")
    if not key:
        return None
    gift = _BY_NAME.get(key)
    if gift is not None:
        return gift
    if key in _BY_ID:
        return _BY_ID[key]
    row = conn.execute(
        "SELECT item_id, name, price FROM item_def "
        "WHERE world = '*' AND kind = ? AND enabled = 1 AND lower(name) = ?",
        (KIND_GIFT, key),
    ).fetchone()
    if row is None:
        return None
    return Gift(str(row["item_id"]), str(row["name"]), int(row["price"] or 0))


def qty(conn: sqlite3.Connection, room: str, uid: str, item_id: str) -> int:
    row = conn.execute(
        "SELECT qty FROM inventory WHERE room = ? AND uid = ? AND item_id = ?",
        (room, uid, item_id),
    ).fetchone()
    return int(row[0]) if row else 0


def add(conn: sqlite3.Connection, room: str, uid: str, item_id: str, n: int = 1) -> int:
    """入包（给盲盒 / 开宝箱用），返回入包后的数量。"""
    if n <= 0:
        return qty(conn, room, uid, item_id)
    conn.execute(
        """
        INSERT INTO inventory (room, uid, item_id, qty) VALUES (?, ?, ?, ?)
        ON CONFLICT(room, uid, item_id) DO UPDATE SET qty = qty + excluded.qty
        """,
        (room, uid, item_id, n),
    )
    return qty(conn, room, uid, item_id)


def take(conn: sqlite3.Connection, room: str, uid: str, item_id: str, n: int = 1) -> bool:
    """出包 1 件；不够返回 False（数量不动）。"""
    if qty(conn, room, uid, item_id) < n:
        return False
    conn.execute(
        "UPDATE inventory SET qty = qty - ? WHERE room = ? AND uid = ? AND item_id = ?",
        (n, room, uid, item_id),
    )
    conn.execute(
        "DELETE FROM inventory WHERE room = ? AND uid = ? AND item_id = ? AND qty <= 0",
        (room, uid, item_id),
    )
    return True
