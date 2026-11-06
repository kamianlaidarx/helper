"""钱包：两币余额 + 单事务分录 + 幂等（`docs/design/playhub.md:322-340`、`407-426`）。

这是**唯一的金融写路径**，三条铁律写在函数签名与实现里：

1. 函数本身**不开事务、不提交、不发消息、不调外部服务**——事务边界由调用方的
   `Store.tx()` 持有，且必须是 `BEGIN IMMEDIATE`。
2. 扣款只能走 `debit()`，不存在 `credit(delta=-N)` 的写法；`amount` 必须是 >= 1 的整数。
3. 不变量靠 SQL 保证：`debit` 用条件更新 `balance >= amount`，影响 0 行即视为余额不足；
   `wallet_ledger` 的 `UNIQUE(room, operation_key, posting_no)` 是第二道保护，命中即抛错。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from .errors import (
    DuplicatePosting,
    Insufficient,
    InvalidAmount,
    InvalidCurrency,
    WalletError,
    WalletUninitialized,
)

Currency = Literal["coin", "diamond"]

# 建号时两个币种都必须初始化（§3.2.1），顺序固定，便于排查余额行缺失。
CURRENCIES: tuple[Currency, Currency] = ("coin", "diamond")

# SQLite INTEGER 上限：超过它 SQLite 会退化成 REAL，务必在写入前挡掉。
MAX_BALANCE = 2**63 - 1


@dataclass(frozen=True)
class WalletEntry:
    """一笔已落账的钱包分录。`balance` 是本次变动后的余额，不是变动前。"""

    currency: Currency
    delta: int
    balance: int
    reason: str
    ref: str | None
    operation_key: str
    posting_no: int


@dataclass(frozen=True)
class Player:
    """玩家档案。字段与 `player` 表一一对应，货币不在其中（见 §3.2.3 决定一）。"""

    room: str
    uid: str
    nickname: str
    level: int
    exp: int
    created_at: int
    last_seen: int
    flags: int


# ---------- 参数校验 ----------


def _check_currency(currency: object) -> Currency:
    """currency 必填且只接受两个字面量；不做大小写或别名兼容。"""
    if currency not in CURRENCIES:
        raise InvalidCurrency(f"非法币种：{currency!r}（只接受 'coin' / 'diamond'）")
    return currency  # type: ignore[return-value]


def _check_amount(amount: object) -> int:
    """amount 必须是 >= 1 的 int：bool、浮点、字符串、0、负数全部拒绝。"""
    if type(amount) is not int:
        raise InvalidAmount(f"金额必须是整数，得到 {type(amount).__name__}: {amount!r}")
    if amount < 1:
        raise InvalidAmount(f"金额必须是正整数，得到 {amount}")
    if amount > MAX_BALANCE:
        raise InvalidAmount(f"金额超出可表示范围：{amount}")
    return amount


def _check_text(value: object, field: str) -> str:
    """reason / operation_key / room / uid 这类分类键必须是字符串，避免数字被静默转文本。"""
    if not isinstance(value, str) or not value:
        raise WalletError(f"{field} 必须是非空字符串，得到 {value!r}")
    return value


# ---------- 内部读余额 ----------


def _select_balance(conn: sqlite3.Connection, room: str, uid: str, currency: Currency) -> int | None:
    row = conn.execute(
        "SELECT balance FROM wallet WHERE room = ? AND uid = ? AND currency = ?",
        (room, uid, currency),
    ).fetchone()
    return None if row is None else int(row["balance"])


def _require_balance(conn: sqlite3.Connection, room: str, uid: str, currency: Currency) -> int:
    current = _select_balance(conn, room, uid, currency)
    if current is None:
        raise WalletUninitialized(f"钱包行缺失：room={room} uid={uid} currency={currency}")
    return current


# ---------- 档案 ----------


def ensure_player(
    conn: sqlite3.Connection, *, room: str, uid: str, nickname: str | None, now: int
) -> None:
    """建档案并初始化金币、钻石两个余额行（均为 0）。幂等：已存在则什么都不改。

    已存在玩家的 `last_seen` / 昵称更新由 `touch()` 负责，这里不做覆盖，
    免得每次开游戏都把档案刷一遍。
    """
    _check_text(room, "room")
    _check_text(uid, "uid")
    conn.execute(
        "INSERT OR IGNORE INTO player (room, uid, nickname, created_at, last_seen) "
        "VALUES (?, ?, ?, ?, ?)",
        (room, uid, nickname or "", now, now),
    )
    for currency in CURRENCIES:
        conn.execute(
            "INSERT OR IGNORE INTO wallet (room, uid, currency, balance, updated_at) "
            "VALUES (?, ?, ?, 0, ?)",
            (room, uid, currency, now),
        )


def profile(conn: sqlite3.Connection, *, room: str, uid: str) -> Player | None:
    """读档案；没建过返回 None（调用方据此决定是否 `ensure_player`）。"""
    row = conn.execute(
        "SELECT room, uid, nickname, level, exp, created_at, last_seen, flags "
        "FROM player WHERE room = ? AND uid = ?",
        (room, uid),
    ).fetchone()
    if row is None:
        return None
    return Player(
        room=row["room"],
        uid=row["uid"],
        nickname=row["nickname"],
        level=int(row["level"]),
        exp=int(row["exp"]),
        created_at=int(row["created_at"]),
        last_seen=int(row["last_seen"]),
        flags=int(row["flags"]),
    )


def balance(conn: sqlite3.Connection, *, room: str, uid: str, currency: Currency) -> int:
    """读某币种余额。钱包行缺失视为初始化异常（§3.2.1），不当 0 处理。"""
    currency = _check_currency(currency)
    return _require_balance(conn, room, uid, currency)


# ---------- 借贷 ----------


def _posting_exists(
    conn: sqlite3.Connection, room: str, operation_key: str, posting_no: int
) -> bool:
    """这笔分录是否已经落过账（`§4.2.9`：写序的第一道闸，先查再动余额）。"""
    row = conn.execute(
        "SELECT 1 FROM wallet_ledger WHERE room = ? AND operation_key = ? AND posting_no = ?",
        (room, operation_key, posting_no),
    ).fetchone()
    return row is not None


def _rollback_posting(conn: sqlite3.Connection) -> None:
    """退回保存点；SQLite 已自行终止事务时不掩盖原始异常。"""
    try:
        conn.execute("ROLLBACK TO wallet_posting")
        conn.execute("RELEASE wallet_posting")
    except sqlite3.Error:
        pass


def _write_ledger(
    conn: sqlite3.Connection,
    *,
    room: str,
    uid: str,
    currency: Currency,
    delta: int,
    balance_after: int,
    reason: str,
    ref: str | None,
    operation_key: str,
    posting_no: int,
    now: int,
) -> None:
    """写分录。UNIQUE 冲突一律上抛，绝不 `INSERT OR IGNORE` 吞掉重放。"""
    try:
        conn.execute(
            "INSERT INTO wallet_ledger "
            "(room, uid, currency, delta, balance_after, reason, ref, operation_key, posting_no, at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (room, uid, currency, delta, balance_after, reason, ref, operation_key, posting_no, now),
        )
    except sqlite3.IntegrityError as error:
        if "UNIQUE" in str(error).upper():
            raise DuplicatePosting(
                room=room, operation_key=operation_key, posting_no=posting_no
            ) from None
        raise WalletError(f"写流水失败：{error}") from None


def credit(
    conn: sqlite3.Connection,
    *,
    room: str,
    uid: str,
    currency: Currency,
    amount: int,
    reason: str,
    operation_key: str,
    posting_no: int,
    now: int,
    ref: str | None = None,
) -> WalletEntry:
    """加币。返回入账后的分录；不提交事务。"""
    currency = _check_currency(currency)
    amount = _check_amount(amount)
    _check_text(reason, "reason")
    _check_text(operation_key, "operation_key")

    # §4.2.9 写序：重放先于动账。分录已存在就直接抛，余额一分没动。
    if _posting_exists(conn, room, operation_key, posting_no):
        raise DuplicatePosting(room=room, operation_key=operation_key, posting_no=posting_no)

    conn.execute("SAVEPOINT wallet_posting")
    try:
        # 上限检查放在 SQL 里，避免"读到旧值 → 在 Python 里算出溢出"的竞态：
        # 余额已经接近上限时影响 0 行，下面依据"行是否存在"区分是溢出还是没建号。
        cursor = conn.execute(
            "UPDATE wallet SET balance = balance + ?, updated_at = ? "
            "WHERE room = ? AND uid = ? AND currency = ? AND balance <= ?",
            (amount, now, room, uid, currency, MAX_BALANCE - amount),
        )
        if cursor.rowcount == 0:
            if _select_balance(conn, room, uid, currency) is None:
                raise WalletUninitialized(f"钱包行缺失：room={room} uid={uid} currency={currency}")
            raise WalletError(f"余额溢出上限，拒绝入账：currency={currency} amount={amount}")

        after = _require_balance(conn, room, uid, currency)
        _write_ledger(
            conn,
            room=room,
            uid=uid,
            currency=currency,
            delta=amount,
            balance_after=after,
            reason=reason,
            ref=ref,
            operation_key=operation_key,
            posting_no=posting_no,
            now=now,
        )
    except BaseException:
        # 钱与分录必须同生同死：分录写失败时把余额退回保存点之前。
        _rollback_posting(conn)
        raise
    conn.execute("RELEASE wallet_posting")
    return WalletEntry(
        currency=currency,
        delta=amount,
        balance=after,
        reason=reason,
        ref=ref,
        operation_key=operation_key,
        posting_no=posting_no,
    )


def debit(
    conn: sqlite3.Connection,
    *,
    room: str,
    uid: str,
    currency: Currency,
    amount: int,
    reason: str,
    operation_key: str,
    posting_no: int,
    now: int,
    ref: str | None = None,
) -> WalletEntry:
    """扣币。余额不足抛 `Insufficient`；不提交事务。

    条件更新是唯一的正确性来源：`balance >= amount` 由 SQLite 在写锁内判定，
    多进程并发下也只有一条能拿到写锁并把余额扣下去。
    """
    currency = _check_currency(currency)
    amount = _check_amount(amount)
    _check_text(reason, "reason")
    _check_text(operation_key, "operation_key")

    # §4.2.9 写序：重放先于动账。分录已存在就直接抛，余额一分没动。
    if _posting_exists(conn, room, operation_key, posting_no):
        raise DuplicatePosting(room=room, operation_key=operation_key, posting_no=posting_no)

    conn.execute("SAVEPOINT wallet_posting")
    try:
        cursor = conn.execute(
            "UPDATE wallet SET balance = balance - ?, updated_at = ? "
            "WHERE room = ? AND uid = ? AND currency = ? AND balance >= ?",
            (amount, now, room, uid, currency, amount),
        )
        if cursor.rowcount == 0:
            # 影响 0 行有两种含义，必须分开报：没建号（初始化异常）与钱不够（业务结果）。
            current = _select_balance(conn, room, uid, currency)
            if current is None:
                raise WalletUninitialized(f"钱包行缺失：room={room} uid={uid} currency={currency}")
            raise Insufficient(
                room=room, uid=uid, currency=currency, amount=amount, balance=current
            )

        after = _require_balance(conn, room, uid, currency)
        _write_ledger(
            conn,
            room=room,
            uid=uid,
            currency=currency,
            delta=-amount,
            balance_after=after,
            reason=reason,
            ref=ref,
            operation_key=operation_key,
            posting_no=posting_no,
            now=now,
        )
    except BaseException:
        # 钱与分录必须同生同死：分录写失败时把余额退回保存点之前。
        _rollback_posting(conn)
        raise
    conn.execute("RELEASE wallet_posting")
    return WalletEntry(
        currency=currency,
        delta=-amount,
        balance=after,
        reason=reason,
        ref=ref,
        operation_key=operation_key,
        posting_no=posting_no,
    )
