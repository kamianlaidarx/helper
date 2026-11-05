"""Store：playhub 唯一持有 SQLite 连接与 DDL 的地方。

约定（`docs/design/playhub.md:309-320`、`1091`）：
- 一个账号一个 DB 文件，`account` 只做诊断标识，不落任何列；
- `isolation_level=None` 手动控事务；写事务一律 `BEGIN IMMEDIATE`；
- 每连接显式 `PRAGMA foreign_keys = ON`（SQLite 默认关闭，不开则外键全失效）；
- 插件内串行，运行时只用一条连接。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .errors import SchemaError, StoreError

# 钱包改为显式两币的版本号（文档称 "钱包契约 v3"）。改表必须递增并走迁移。
SCHEMA_VERSION = 3

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class Store:
    """连接与事务的唯一入口。"""

    def __init__(self, db_path: Path, *, account: str, timeout: float = 3.0) -> None:
        if not account or not isinstance(account, str):
            raise StoreError("account 必须是非空字符串")
        self.db_path = Path(db_path)
        self.account = account
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None：不自动开事务，BEGIN/COMMIT 全由 tx() 显式控制。
        # check_same_thread=False 只是给线程内迁移留余地，运行时仍单线程使用。
        self._conn: sqlite3.Connection | None = sqlite3.connect(
            str(self.db_path), timeout=timeout, isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
        self._conn.execute("PRAGMA synchronous = NORMAL")

    # ---------- 连接 ----------

    @property
    def conn(self) -> sqlite3.Connection:
        """已打开的连接；close() 之后再访问视为使用错误。"""
        if self._conn is None:
            raise StoreError("连接已关闭")
        return self._conn

    def close(self) -> None:
        """关连接；可重复调用。"""
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    # ---------- 迁移 ----------

    def migrate(self) -> None:
        """建表并把 schema_version 写入 meta。幂等：重复调用不报错、不改数据。"""
        try:
            sql = _SCHEMA_PATH.read_text(encoding="utf-8")
        except OSError as error:
            raise SchemaError(f"读取 schema.sql 失败：{error}") from None
        conn = self.conn
        try:
            conn.executescript(sql)
        except sqlite3.Error as error:
            raise SchemaError(f"执行 schema.sql 失败：{error}") from None
        # schema.sql 里全部是 IF NOT EXISTS，所以版本号可以无条件覆盖写。
        with self.tx() as tx_conn:
            tx_conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )

    @property
    def schema_version(self) -> int:
        """meta.schema_version；库还没迁移时返回 0。"""
        try:
            row = self.conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
        except sqlite3.Error:
            return 0
        if row is None:
            return 0
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            raise SchemaError(f"schema_version 不可解析：{row['value']!r}") from None

    # ---------- 事务 ----------

    @contextmanager
    def tx(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """写事务边界：正常提交、异常回滚。

        `immediate=True` 用 `BEGIN IMMEDIATE` 立刻拿写锁——这是钱包防并发的关键，
        迟到的那条连接会直接拿不到锁，而不是先读到旧余额再写。
        """
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield conn
        except BaseException:
            self._rollback(conn)
            raise
        try:
            conn.execute("COMMIT")
        except BaseException:
            # 提交本身失败（如磁盘/约束问题）也必须回滚，避免连接残留在事务里。
            self._rollback(conn)
            raise

    @staticmethod
    def _rollback(conn: sqlite3.Connection) -> None:
        """回滚兜底：连接已关闭或事务已被 SQLite 自行终止时不掩盖原始异常。"""
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
