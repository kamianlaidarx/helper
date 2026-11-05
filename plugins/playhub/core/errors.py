"""playhub 的异常类型。

全部从 StoreError / WalletError 两个基类派生，调用方可以按粒度捕获：
- 只关心"钱包出了事" → 捕 WalletError；
- 关心"整个存储层出了事" → 捕 StoreError。

命名与文档对齐：`Insufficient` 见 §2.4（`docs/design/playhub.md:340`）；
"余额行缺失按初始化异常归类"见 §3.2.1（同文件 1092 行）。
"""


class StoreError(Exception):
    """存储层异常基类：连接、迁移、事务边界问题。"""


class SchemaError(StoreError):
    """schema.sql 读取/执行失败，或 schema_version 不可解析。"""


class WalletError(StoreError):
    """钱包异常基类：所有余额与流水相关错误的公共父类。"""


class InvalidCurrency(WalletError):
    """币种不在 'coin' / 'diamond' 之内（文档要求非法币种直接拒绝，§2.4）。"""


class InvalidAmount(WalletError):
    """amount 不是 >= 1 的整数（文档要求正整数，§2.4）。"""


class WalletUninitialized(WalletError):
    """钱包行不存在：玩家未建，或两币余额行未初始化（§3.2.1）。"""


class Insufficient(WalletError):
    """余额不足。

    这是**业务结果**而非程序故障（§2.4）：调用方按需捕获后转成用户可读回复，
    并在独立事务里补失败审计。
    """

    def __init__(self, *, room: str, uid: str, currency: str, amount: int, balance: int) -> None:
        super().__init__(f"余额不足：{currency} 需 {amount}，实有 {balance}")
        self.room = room
        self.uid = uid
        self.currency = currency
        self.amount = amount
        self.balance = balance


class DuplicatePosting(WalletError):
    """同一 (room, operation_key, posting_no) 分录被重放。

    对应 `wallet_ledger` 的 UNIQUE 约束：它是第二道保护，命中即说明外层业务
    幂等键没兜住（§2.4）。这里必须抛错而不是静默忽略，否则重放会被误判为成功。
    """

    def __init__(self, *, room: str, operation_key: str, posting_no: int) -> None:
        super().__init__(
            f"分录重复：room={room} operation_key={operation_key} posting_no={posting_no}"
        )
        self.room = room
        self.operation_key = operation_key
        self.posting_no = posting_no
