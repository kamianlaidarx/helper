"""盲盒与开宝箱（第 3 批 `lottery` 引擎，对应 §4 总表第 16 / 35 项）。

数值来源与落地口径
------------------
**已定案**（`docs/design/playhub.md` §4.4）：盲盒次数 5 钻石/次、每日限购 2 次；
宝箱三型 铜 50 / 银 150 / 金 400 金币、开启免费、钻石命中 铜 1/20、银 1/12、金 1/8
（金币 +1/+1/+2）。

**落地口径**（设计没写，本文件定，已写进文档 §4.4）：

1. **盲盒的免费次数**：每日 1 次。设计只定了「买次数」，没说免费额度；若一次都不免费，
   `盲盒` 就成了纯钻石功能，而钻石产出只有 ≈5.5/周，等于一周只能玩一次。
   定 1 次免费 + 最多买 2 次 = 每天最多 3 抽。
2. **盲盒抽的是礼物**（`core/items.py` 的 `BLIND_BOX_LOOT`）。设计没定掉落表；
   **不给随机金币**——§4.4 明写「金币产出全部为固定值，无随机金币」，盲盒若随机发币
   就破了这条，金币池会失控。开出的礼物进背包，之后可用 `礼物` 送人。
3. **开宝箱消耗背包里的箱子**，不再收开启费（§4.4「开启成本：免费」）。箱子来源只有
   签到（铜箱 1/日、金箱 连签第 7 天 1 个）；银箱来自活跃榜周榜前 10，属第 4 批的定时
   动作，本批**不发银箱**，`开宝箱 银` 只会回「背包里没有」。

为什么这里没有 SQL 豁免
------------------------
`lottery_draw` 的写入是本引擎的一部分（抽奖台账，统计爆率用），不搬 `core/`；
其余账目一律经 `core/wallet.py`。`engines` 禁 SQL 的规则见 §2.5，本文件的两条
`INSERT INTO lottery_draw` 与 `daily.py`/`quiz.py` 的计数器同属已登记豁免。
"""
from __future__ import annotations

from ..core import balance, clock, credit, debit, ensure_player, items, render
from ..core.errors import DuplicatePosting, Insufficient, WalletUninitialized
from . import Reply, msg_nickname, msg_room, msg_uid, store_of

#: 盲盒每日免费次数 / 加购价格与限购（§4.4「钻石消耗」+ 本文件落地口径）。
BLIND_BOX_FREE_PER_DAY = 1
BLIND_BOX_BUY_PRICE = 5
BLIND_BOX_BUY_CAP = 2
BLIND_BOX_MAX_BATCH = 10

FREE_KEY = "blindbox_free"       # daily_counter.key：免费次数消耗
BUY_KEY = "blindbox_buy"         # daily_counter.key：**买到的**加购额度
USED_BUY_KEY = "blindbox_used_buy"   # daily_counter.key：已消耗的加购额度
# 「买」和「用」必须分开记：BUY_KEY 只由 `_buy()` 自增（代表额度），
# USED_BUY_KEY 只在抽奖时自增。合成一个计数器的话，没买过的加购额度
# 也会被当成可用次数，裸发「盲盒」能白抽 3 次（曾真写出过这个 bug）。

COIN_CHEST_REASON = "chest"          # 金币流水 reason
DIAMOND_CHEST_REASON = "chest_diamond"   # 钻石流水 reason（§4.4 正向枚举之一）
DIAMOND_BUY_REASON = "blindbox_buy"      # 钻石流水 reason（sink）

SUB_BUY = "买次数"

USAGE_BLIND = (
    "💡 用法：盲盒（抽 1 次）/ 盲盒 3（连抽）/ 盲盒 买次数"
)
USAGE_CHEST = "💡 用法：开宝箱（默认铜箱）/ 开宝箱 银 / 开宝箱 金"


def _counter_used(conn, *, room: str, uid: str, day: str, key: str) -> int:
    row = conn.execute(
        "SELECT used FROM daily_counter WHERE room = ? AND uid = ? AND day = ? AND key = ?",
        (room, uid, day, key),
    ).fetchone()
    return int(row[0]) if row else 0


def _counter_consume(conn, *, room: str, uid: str, day: str, key: str, cap: int, now: int) -> None:
    conn.execute(
        """
        INSERT INTO daily_counter (room, uid, day, key, used, cap, updated_at)
        VALUES (?, ?, ?, ?, 1, ?, ?)
        ON CONFLICT(room, uid, day, key)
        DO UPDATE SET used = used + 1, cap = excluded.cap, updated_at = excluded.updated_at
        """,
        (room, uid, day, key, cap, now),
    )


def _left(used: int, cap: int) -> int:
    return max(0, cap - used)


class LotteryEngine:
    """盲盒、开宝箱。`name` 必须与 `keywords.Trigger.engine` 逐字一致。"""

    name = "lottery"

    def __init__(self, *, rng=None, now_fn=None):
        import random

        self._rng = rng if rng is not None else random.Random()
        self._now = now_fn if now_fn is not None else clock.now_ts

    # -- 入口 ---------------------------------------------------------------
    def on_command(self, cmd, msg=None, ctx=None, game=None) -> Reply | None:
        action = getattr(cmd, "action", "")
        if action == "lottery.blindbox":
            return self.blindbox(cmd, msg, ctx, game)
        if action == "lottery.chest":
            return self.chest(cmd, msg, ctx, game)
        return None

    # -- 盲盒 ---------------------------------------------------------------
    def blindbox(self, cmd, msg=None, ctx=None, game=None) -> Reply | None:
        room, uid = msg_room(msg), msg_uid(msg)
        if not room or not uid:
            return None
        store = store_of(game)
        now = self._now()
        day = clock.day_of(now)
        nickname = msg_nickname(msg, ctx)
        args = getattr(cmd, "args", "") or ""

        if args.strip().startswith(SUB_BUY):
            return self._buy(msg, room, uid, nickname, store, now, day)

        count = self._parse_count(args)
        if count is None:
            return Reply(USAGE_BLIND, "lottery.blindbox")

        draws: list[items.Gift] = []
        free_left = buy_left = 0
        try:
            with store.tx() as conn:
                ensure_player(conn, room=room, uid=uid, nickname=nickname or None, now=now)
                used_free = _counter_used(conn, room=room, uid=uid, day=day, key=FREE_KEY)
                purchased = _counter_used(conn, room=room, uid=uid, day=day, key=BUY_KEY)
                used_buy = _counter_used(conn, room=room, uid=uid, day=day, key=USED_BUY_KEY)
                free_left = _left(used_free, BLIND_BOX_FREE_PER_DAY)
                buy_left = max(0, purchased - used_buy)
                if free_left + buy_left <= 0:
                    return Reply(self._no_count_text(), "lottery.blindbox")
                take = min(count, free_left + buy_left)
                for _ in range(take):
                    if free_left > 0:
                        _counter_consume(
                            conn, room=room, uid=uid, day=day, key=FREE_KEY,
                            cap=BLIND_BOX_FREE_PER_DAY, now=now,
                        )
                        free_left -= 1
                    else:
                        _counter_consume(
                            conn, room=room, uid=uid, day=day, key=USED_BUY_KEY,
                            cap=purchased, now=now,
                        )
                        buy_left -= 1
                    gift = items.roll_blind_box(self._rng)
                    items.add(conn, room, uid, gift.item_id)
                    conn.execute(
                        "INSERT INTO lottery_draw "
                        "(room, uid, source, box_id, cost, cost_currency, item_id, qty, at) "
                        "VALUES (?, ?, 'blind_box', ?, 0, NULL, ?, 1, ?)",
                        (room, uid, items.BLIND_BOX_ID, gift.item_id, now),
                    )
                    draws.append(gift)
        except (DuplicatePosting, WalletUninitialized):
            return Reply("这个玩法暂时出错了", "lottery.blindbox")

        return Reply(
            self._blind_text(nickname, draws, free_left, buy_left, count - len(draws)),
            "lottery.blindbox",
        )

    def _buy(self, msg, room: str, uid: str, nickname: str, store, now: int, day: str) -> Reply | None:
        remaining_free = remaining_buy = 0
        try:
            with store.tx() as conn:
                ensure_player(conn, room=room, uid=uid, nickname=nickname or None, now=now)
                used_buy = _counter_used(conn, room=room, uid=uid, day=day, key=BUY_KEY)
                if used_buy >= BLIND_BOX_BUY_CAP:
                    return Reply(
                        render.result(
                            nickname,
                            "🧾 今天的加购次数用完了",
                            [render.row("├ ", f"每天最多买 {BLIND_BOX_BUY_CAP} 次")],
                            tail="⏳ 每天 24:00 清零，明天再来",
                        ),
                        "lottery.blindbox",
                    )
                debit(
                    conn,
                    room=room,
                    uid=uid,
                    currency="diamond",
                    amount=BLIND_BOX_BUY_PRICE,
                    reason=DIAMOND_BUY_REASON,
                    operation_key=f"blindbox_buy:{day}:{uid}:{used_buy}",
                    posting_no=0,
                    now=now,
                )
                _counter_consume(
                    conn, room=room, uid=uid, day=day, key=BUY_KEY,
                    cap=BLIND_BOX_BUY_CAP, now=now,
                )
                remaining_free = _left(
                    _counter_used(conn, room=room, uid=uid, day=day, key=FREE_KEY),
                    BLIND_BOX_FREE_PER_DAY,
                )
                purchased = used_buy + 1
                used_now = _counter_used(conn, room=room, uid=uid, day=day, key=USED_BUY_KEY)
                remaining_buy = max(0, purchased - used_now)
                diamond_after = balance(conn, room=room, uid=uid, currency="diamond")
        except Insufficient:
            return Reply(
                render.result(
                    nickname,
                    "❌ 钻石不够",
                    [render.row("├ ", f"加购一次要 {BLIND_BOX_BUY_PRICE} 钻石")],
                    tail="💡 钻石靠连签第 7 天、上上签、开宝箱攒",
                ),
                "lottery.blindbox",
            )
        except (DuplicatePosting, WalletUninitialized):
            return Reply("这个玩法暂时出错了", "lottery.blindbox")

        return Reply(
            render.result(
                nickname,
                "🧾 加购成功",
                [
                    render.row("├ ", f"钻石：-{BLIND_BOX_BUY_PRICE}"),
                    render.current("💎", "钻石", diamond_after),
                    render.current("🎰", "今天还能抽", f"免费 {remaining_free} / 加购 {remaining_buy}"),
                ],
                tail="💡 直接发「盲盒」就能用掉这些次数",
            ),
            "lottery.blindbox",
        )

    def _parse_count(self, args: str) -> int | None:
        text = (args or "").strip()
        if not text:
            return 1
        if not text.isdigit():
            return None
        value = int(text)
        if value < 1:
            return None
        return min(value, BLIND_BOX_MAX_BATCH)

    def _no_count_text(self) -> str:
        return render.tip(
            "🎰",
            f"今天的盲盒次数用完了（免费 {BLIND_BOX_FREE_PER_DAY} 次 + 加购 {BLIND_BOX_BUY_CAP} 次）。"
            f"发「盲盒 买次数」，{BLIND_BOX_BUY_PRICE} 钻石加 1 次",
        )

    def _blind_text(
        self, nickname: str, draws: list[items.Gift], free_left: int, buy_left: int, skipped: int
    ) -> str:
        rows = [render.row("├ ", f"{g.icon} {g.name} ×1") for g in draws]
        rows.append(
            render.current("🎰", "今天还能抽", f"免费 {free_left} / 加购 {buy_left}")
        )
        headline = "🎰 盲盒开出" if len(draws) == 1 else f"🎰 连开 {len(draws)} 个"
        tail = f"📦 礼物进了背包，发「礼物 @某人 玫瑰」就能送"
        if skipped:
            tail = f"⚠ 次数不够，少抽了 {skipped} 次\n" + tail
        return render.result(nickname, headline, rows, tail=tail)

    # -- 开宝箱 -------------------------------------------------------------
    def chest(self, cmd, msg=None, ctx=None, game=None) -> Reply | None:
        room, uid = msg_room(msg), msg_uid(msg)
        if not room or not uid:
            return None
        store = store_of(game)
        now = self._now()
        nickname = msg_nickname(msg, ctx)
        arg = (getattr(cmd, "args", "") or "").strip()

        chest = items.CHESTS[0] if not arg else items.find_chest(arg)
        if chest is None:
            return Reply(
                render.result(
                    nickname,
                    "❓ 认不出这个箱子",
                    [render.row("├ ", items.chest_price_text())],
                    tail=USAGE_CHEST,
                ),
                "lottery.chest",
            )

        coin_gain = diamond_gain = 0
        coin_after = 0
        try:
            with store.tx() as conn:
                ensure_player(conn, room=room, uid=uid, nickname=nickname or None, now=now)
                held = items.qty(conn, room, uid, chest.item_id)
                if not items.take(conn, room, uid, chest.item_id):
                    return Reply(self._no_chest_text(nickname, chest), "lottery.chest")
                entry = credit(
                    conn,
                    room=room,
                    uid=uid,
                    currency="coin",
                    amount=chest.coin,
                    reason=COIN_CHEST_REASON,
                    operation_key=f"chest:{chest.item_id}:{uid}:{now}:{held}",
                    posting_no=0,
                    now=now,
                )
                coin_gain, coin_after = chest.coin, entry.balance
                if self._rng.randrange(chest.diamond_odds) == 0:
                    credit(
                        conn,
                        room=room,
                        uid=uid,
                        currency="diamond",
                        amount=chest.diamond,
                        reason=DIAMOND_CHEST_REASON,
                        operation_key=f"chest:{chest.item_id}:{uid}:{now}:{held}",
                        posting_no=1,
                        now=now,
                    )
                    diamond_gain = chest.diamond
                conn.execute(
                    "INSERT INTO lottery_draw "
                    "(room, uid, source, box_id, cost, cost_currency, item_id, qty, at) "
                    "VALUES (?, ?, 'chest', ?, 0, NULL, ?, 1, ?)",
                    (room, uid, chest.item_id, chest.item_id, now),
                )
        except DuplicatePosting:
            return Reply("这个箱子刚刚开过了，稍后再试", "lottery.chest")
        except WalletUninitialized:
            return Reply("这个玩法暂时出错了", "lottery.chest")

        rows = [render.row("├ ", f"金币：+{coin_gain}")]
        if diamond_gain:
            rows.append(render.row("├ ", f"钻石：+{diamond_gain}"))
        rows.append(render.current("💰", "金币", coin_after))
        tail = f"🎲 钻石命中率 {chest.diamond_odds} 分之 1"
        if not diamond_gain:
            tail = "🍀 这次没开出钻石，下次吧\n" + tail
        return Reply(
            render.result(nickname, f"{chest.icon} 打开{chest.name}", rows, tail=tail),
            "lottery.chest",
        )

    def _no_chest_text(self, nickname: str, chest) -> str:
        rows = [render.row("├ ", f"{chest.name}：背包里没有")]
        if chest.item_id == items.CHEST_BRONZE:
            rows.append(render.row("├ ", "获取：每天签到送 1 个"))
        elif chest.item_id == items.CHEST_GOLD:
            rows.append(render.row("├ ", "获取：连签第 7 天送 1 个"))
        else:
            rows.append(render.row("├ ", "获取：活跃榜周榜前 10 名（第 4 批上线）"))
        return render.result(nickname, f"📦 没有{chest.name}", rows, tail=USAGE_CHEST)
