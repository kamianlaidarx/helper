# Playhub v1 修订增量（Delta）

> 基线：`docs/design/playhub.md`（1382 行，关键词触发版）。本文所有行号均指**修订前**的基线文件。
> 生成：2026-11-04，`ccg_analyze` + `ccg_design` 两段产出。**尚未并入基线**，基线文件未被修改。
> 状态：待并入；待用户拍板项见 §8。

---

## 0. 本文的前提：九条已定裁决

设计不再重新论证这九条，只按它们写。

1. **货币只做两种**：金币 + 钻石。银币 / 魅力 / 身价 / 积分 全部 v2。钻石是稀缺货币，正向产出仅限 `签到（连签第 7 天）`、`开宝箱（金箱）`、`抽签（上上签）` 三处小概率产出。
2. **权限只做一层**：统一门禁用 `ctx.group_member_role()`，只放行 `owner` / `admin`；`unknown` **失败关闭**。修正基线 §7 用 `members.owner_ids` / `is_owner` 的错误表述。
3. **超级宝宝砍到 v2**：v1 只保留 `宝宝 看 / 喂养 / 改名 X / 超极`，`超极` 只说明 v2 开放，不执行升级与扣费。
4. **结婚 v1 补 4 子命令**：`我的婚姻`、`度蜜月`、`申请离婚`、`婚姻统计`。其余 9 条（逼婚/抢婚/指婚/拆散/逃婚/洞房/复婚/商城/证号）v2。
5. **切磋不联动监狱**：v1 只有 约战/同意/拒绝/超时；不复用 `jail` 表（那是群管软禁言，语义不同）。
6. **排行榜 v1 出 4 个**：金币榜、活跃榜、婚姻榜、邀请榜。
7. **活跃榜四档**：日 / 周 / 月 / 总。
8. **潜水只统计不清理**：框架无踢人能力，帮助文案必须写明「只统计不清人」。
9. **`#` 分隔符要做**：只在命令首部拆第一个 `#`，参数值内部的 `#` 保留；NFKC 已把全角 `＃` 折成半角。

---

## 1. 变更清单

### 1.1 总览、预算与分层

| 操作 | 原文锚点 | 并入内容 |
|---|---|---|
| 替换 | §0，`docs/design/playhub.md:43-54` | 货币缩减为金币、钻石两种；插件预算改为 **42 文件 / 323 KiB**；基础排期改为 **49 人日**，另加 15% 风险缓冲后为 **56.35 人日，排期取 57 人日**。 |
| 新增 | §1.2，`docs/design/playhub.md:89-145` | 在 `plugins/playhub/core/` 增加 `permissions.py`，只负责纯权限决策，不访问 `ctx`、数据库或网络。 |
| 替换 | §1 文件预算，`docs/design/playhub.md:147-196` | 采用本文第 5 节预算增量。题库继续位于插件目录之外；测试文件同样位于插件目录之外。 |
| 新增 | §2.1—§2.4，`docs/design/playhub.md:214-345` | `adapters/memberfeed.py` 读取微信群角色；`core/permissions.py` 判断；`app.py` 编排权限审计与执行。引擎和适配器仍不直接写 SQL。 |
| 替换 | §2.4 钱包接口，`docs/design/playhub.md:283-345` | 金币单余额接口替换为显式币种钱包；记账、业务状态、幂等键在同一事务提交。具体契约见第 3 节。 |

### 1.2 解析、DDL 与排行榜

| 操作 | 原文锚点 | 并入内容 |
|---|---|---|
| 保留并补充 | §2.6.1，`docs/design/playhub.md:409-420` | 保留 NFKC；增加「艾特范围先识别、规范化时重映射范围」的约束。全角 `＃` 因 NFKC 与半角 `#` 等价。 |
| 替换 | §2.6.2，`docs/design/playhub.md:433-456` | 使用本文第 3.3 节解析契约；只在命令首部识别第一个分隔符，参数中的 `#` 不再拆分。 |
| 替换 | §3.2，`docs/design/playhub.md:609-638` | 删除 `player.coins` 和金币专用物理流水表，采用 `wallet`、`wallet_ledger`，提供只读 `coin_ledger` 兼容视图。完整 DDL 见第 2 节。 |
| 新增 | §3 抽奖记录，`docs/design/playhub.md:775-788` | `lottery_draw` 增加 `cost_currency TEXT CHECK(cost_currency IN ('coin','diamond'))`；免费抽取时 `cost=0`、`cost_currency=NULL`，付费时必须同时记录币种和金额。 |
| 替换 | §3 排行视图，`docs/design/playhub.md:904-927` | 金币榜读取 `wallet(currency='coin')`；活跃榜按 `activity_daily` 查询；v1 不依赖 `rank_period` 快照。等级榜、答题榜不作为 v1 对外排行榜。 |
| 替换 | §6 幂等，`docs/design/playhub.md:1204-1220` | 删除「UPSERT/主键自然保证计数幂等」的表述。增量计数、邀请计分、钱包操作均须先取得业务幂等权，再执行更新。 |

### 1.3 玩法范围

以下替换 §4 总表中对应行，并同步 §4 的说明、帮助文案和批次验收条件。

| 原文锚点 | 替换后的 v1 范围 |
|---|---|
| §4 签到、抽签、开宝箱，`docs/design/playhub.md:951-990` | 钻石正向产出只有：连签第 7 天签到、金箱、上上签；均为小概率产出，概率和数量待拍板。其他玩法不产钻石。 |
| §4 结婚，`docs/design/playhub.md:958`；§4 补充说明，`:1052` | 保留求婚、同意、拒绝；补充「我的婚姻、度蜜月、申请离婚、婚姻统计」；补充 `结婚 清空求婚次数`，扣 100 钻石。删除直接 `解除 @` 作为正式入口的约定，其行为由申请离婚状态机替代。 |
| §4 宝宝，`docs/design/playhub.md:959,990,1049` | 明确标注 **「v1 是基础版」**，只提供 `宝宝 看 / 喂养 / 改名 X / 超极`。`超极` 为保留入口，仅说明超级宝宝在 v2 开放，不执行升级、扣费或超级玩法。 |
| §4 纳妾，`docs/design/playhub.md:961,1054` | v1 默认永久关闭，即无普通开关可以启用；帮助返回「v1 未开放」。保留未来启用门槛 `diamond >= 300`，**门槛不是入场扣款**，v1 不发生纳妾扣钻。 |
| §4 切磋，`docs/design/playhub.md:955` 及 `:983` 附近相关说明 | 只有约战、同意、拒绝、超时；状态存 `game_state`，不写 `jail`，不建立临时婚姻关系。越狱、保释、偷跑、出院全部 v2。 |
| §4 活跃、排行榜，`docs/design/playhub.md:969-970` | 对外只出金币榜、活跃榜、婚姻榜、邀请榜；活跃榜支持日、周、月、总。钻石榜及其他货币榜不进入 v1。 |
| §4 潜水，`docs/design/playhub.md:991` | 帮助和结果标题固定包含 **「只统计不清人」**；没有踢人、禁言、移出成员动作。 |
| §4 邀请，`docs/design/playhub.md:992` | 保留群级进退统计；按人邀请统计以有效 `actor_id` 为前提；缺失邀请人时只记群级统计，不猜测归属。 |
| §4 改名、定时推送，`docs/design/playhub.md:995-996,1023-1039` | 改名开关、推送配置需微信群主/管理员门禁；普通成员查看自己的宝宝、修改宝宝名字等个人玩法不套用群管理门禁。 |
| §4 不做项，`docs/design/playhub.md:1064` | 删除「一律不做角色权限/需要另建权限体系」的表述；替换为本文的一层角色门禁。 |

结婚以下子命令全部列入 v2：**逼婚、抢婚、指婚、拆散、逃婚、洞房、复婚、商城、证号**。银币、魅力、身价、积分也全部列入 v2。

### 1.4 权限、风险和待拍板

| 操作 | 原文锚点 | 并入内容 |
|---|---|---|
| 替换 | §7 第四批，`docs/design/playhub.md:1278` | 「统一调用 `ctx.group_member_role(room, actor, expected_session=...)`，只放行 `owner/admin`；`unknown` 拒绝执行并写 `audit.result='denied'`。不使用 `members.owner_ids` 或 `ctx.speaker(message).is_owner`。」 |
| 同步替换 | §8 权限风险，`docs/design/playhub.md:1314` | 同上。自建 owner 白名单与微信群角色不等价。 |
| 替换 | §8 数据保留，`docs/design/playhub.md:1308` | `activity_daily` 是稀疏日聚合，v1 永久保留；90 天清理规则不适用于该表，否则「总榜」会退化为最近 90 天榜。 |
| 替换 | §8 邀请风险，`docs/design/playhub.md:1351` | 改为本文第 4 节 C1 方案；落实邀请人完整传播和缺失降级。 |
| 替换 | §9，`docs/design/playhub.md:1365-1381` | 删除已拍板的币种、角色权限、超级宝宝范围、切磋监狱、四榜、活跃四档、潜水清人、`#` 是否支持等选择题；保留具体业务数值及本文第 8 节未决项。 |
| 修正 | §9，`docs/design/playhub.md:1369` | 删除「不会产生上游合并冲突」的笼统承诺；插件内新增通常独立，C1 则直接修改 fork 的框架文件，存在上游合并冲突。 |

---

## 2. DDL 增量：两币钱包与流水兼容

### 2.1 选择独立 `wallet` 表

采用独立钱包表，不在 `player` 增加 `diamonds`。

理由：

1. 币种约束、扣减、幂等和流水使用同一套实现，避免两列余额形成两套分支。
2. `player` 只保留玩家资料和等级经验，金融状态集中在钱包。
3. 金币榜增加一次限定币种的查询，但可由索引直接支持；这点成本低于余额与流水长期维护两套逻辑的成本。

本表只允许两种币种，不以「可扩展」为理由提前加入 v2 货币。

### 2.2 可替换原 §3.2 的完整片段

```sql
CREATE TABLE IF NOT EXISTS player (
    room        TEXT NOT NULL,
    uid         TEXT NOT NULL,
    nickname    TEXT NOT NULL DEFAULT '',
    level       INTEGER NOT NULL DEFAULT 1 CHECK (level >= 1),
    exp         INTEGER NOT NULL DEFAULT 0 CHECK (exp >= 0),
    created_at  INTEGER NOT NULL,
    last_seen   INTEGER NOT NULL,
    flags       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (room, uid)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS player_by_exp
    ON player(room, exp DESC, uid);

CREATE TABLE IF NOT EXISTS wallet (
    room        TEXT NOT NULL,
    uid         TEXT NOT NULL,
    currency    TEXT NOT NULL
                CHECK (currency IN ('coin', 'diamond')),
    balance     INTEGER NOT NULL DEFAULT 0
                CHECK (typeof(balance) = 'integer' AND balance >= 0),
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (room, uid, currency),
    FOREIGN KEY (room, uid)
        REFERENCES player(room, uid)
        ON DELETE RESTRICT
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS wallet_by_rank
    ON wallet(room, currency, balance DESC, uid);

CREATE TABLE IF NOT EXISTS wallet_ledger (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    room            TEXT NOT NULL,
    uid             TEXT NOT NULL,
    currency        TEXT NOT NULL
                    CHECK (currency IN ('coin', 'diamond')),
    delta           INTEGER NOT NULL
                    CHECK (typeof(delta) = 'integer' AND delta <> 0),
    balance_after   INTEGER NOT NULL
                    CHECK (
                        typeof(balance_after) = 'integer'
                        AND balance_after >= 0
                    ),
    reason          TEXT NOT NULL,
    ref             TEXT,
    operation_key   TEXT NOT NULL,
    posting_no      INTEGER NOT NULL CHECK (posting_no >= 0),
    at              INTEGER NOT NULL,
    FOREIGN KEY (room, uid, currency)
        REFERENCES wallet(room, uid, currency)
        ON DELETE RESTRICT,
    UNIQUE (room, operation_key, posting_no)
);

CREATE INDEX IF NOT EXISTS wallet_ledger_user
    ON wallet_ledger(room, uid, currency, at DESC, id DESC);

CREATE INDEX IF NOT EXISTS wallet_ledger_reason
    ON wallet_ledger(room, currency, reason, at DESC);

-- 兼容旧查询的只读视图；新代码禁止向该视图 INSERT。
CREATE VIEW IF NOT EXISTS coin_ledger AS
SELECT
    id, room, uid, delta, balance_after, reason, ref, at
FROM wallet_ledger
WHERE currency = 'coin';

CREATE VIEW IF NOT EXISTS v_rank_coin AS
SELECT room, uid, balance AS score
FROM wallet
WHERE currency = 'coin';
```

补充约束：

- 每次连接启用 `PRAGMA foreign_keys=ON`；延续原 WAL、`busy_timeout=3000`。
- 玩家建立时初始化金币、钻石两个余额行，均为 0。
- `reason` 使用代码枚举，不由聊天文本直接传入。
- 钻石正向记账枚举仅为：

  ```text
  signin_day7
  chest_gold
  fortune_best
  ```

- 钻石负向记账枚举为 `blind_box_buy_attempts`、`marriage_reset_proposals`；纳妾 v1 无实际扣款入口。
- 中奖但奖励数为 0 时不写钱包流水，由业务抽奖记录体现。
- `operation_key` 标识一次业务操作；`posting_no` 是该操作内的确定性分录序号。一笔业务奖励金币和钻石时使用两个序号。

### 2.3 与旧 `coin_ledger` 的兼容及迁移

**尚未实现数据库时**：直接以本 DDL 替换原文，不制造历史迁移工作。

**已有旧版数据库时**：在单次迁移事务中执行以下步骤，成功后才提升 schema 版本。

1. 检查旧 `coin_ledger` 是物理表还是兼容视图，按 schema 版本选择迁移路径。
2. 旧表改名为 `coin_ledger_v1_legacy`，不丢弃原始数据。
3. 新建 `wallet`、`wallet_ledger`。
4. 每个玩家的 `player.coins` 原值复制到金币钱包，钻石钱包初始化为 0。
5. 历史流水复制为 `currency='coin'`，`operation_key='legacy:coin:' || id`、`posting_no=0`；历史流水编号保留。
6. 旧流水若存在 `delta=0`，保留在 legacy 表，不导入新金融流水；记录迁移数量，不能静默混入。
7. 重建不含 `coins` 的 `player`，保留资料字段；重建相关视图、索引及外键关系。
8. 创建只读 `coin_ledger` 兼容视图；运行 `foreign_key_check`，核对迁移前后玩家数、金币余额总量、非零流水数。
9. 任一核对失败，整体回滚，不留下半迁移状态。

迁移导入历史分录不再次更新余额。旧历史流水若缺少期初记录，不要求「历史流水净和等于当前余额」；迁移以原余额为权威基线，此后每笔余额变化必须对应新流水。

兼容范围仅限旧流水查询。旧版向 `coin_ledger` 写入、向 `player.coins` 更新的代码必须切换到钱包接口，不增加可写视图触发器掩盖旧写路径。

---

## 3. 接口契约

### 3.1 钱包：显式币种、正数存取、统一事务

```python
Currency = Literal["coin", "diamond"]

@dataclass(frozen=True)
class WalletEntry:
    currency: Currency
    delta: int
    balance: int
    reason: str
    ref: str | None
    operation_key: str
    posting_no: int

def balance(
    conn, *, room: str, uid: str, currency: Currency
) -> int: ...

def credit(
    conn, *, room: str, uid: str, currency: Currency,
    amount: int, reason: str,
    operation_key: str, posting_no: int,
    now: int, ref: str | None = None,
) -> WalletEntry: ...

def debit(
    conn, *, room: str, uid: str, currency: Currency,
    amount: int, reason: str,
    operation_key: str, posting_no: int,
    now: int, ref: str | None = None,
) -> WalletEntry: ...
```

契约：

- `amount` 必须是正整数；不再用 `credit(delta=-N)` 表示扣款。
- 币种参数必填，不以默认金币掩盖遗漏。
- 钱包函数不自行提交事务、不发送消息、不调用外部服务。
- `debit()` 使用带余额条件的更新：

  ```sql
  UPDATE wallet
  SET balance = balance - :amount, updated_at = :now
  WHERE room = :room
    AND uid = :uid
    AND currency = :currency
    AND balance >= :amount;
  ```

  影响 0 行表示余额不足或钱包初始化异常，分别返回领域错误；不生成负余额。

- 更新成功后，在同一事务读取余额并插入流水；插入失败则回滚余额更新。
- `credit()` 同样检查整数上限，避免 SQLite 整数溢出转为浮点数。
- 外层使用 `BEGIN IMMEDIATE`，覆盖：

  ```text
  检查业务幂等键
    → 校验业务状态/次数
    → 扣款或发奖
    → 更新业务状态
    → 写流水、审计、幂等结果
    → COMMIT
    → 发送 Reply
  ```

- 余额不足属于业务结果；需要保留失败审计时，先回滚业务事务，再独立提交失败审计。
- 重复消息命中已完成操作，直接返回已存结果，不重新扣款。
- 分录唯一键是第二道保护，不能代替外层业务幂等。
- 同插件串行回调仅减少并发来源；多进程仍以数据库事务和条件更新保证正确性。

### 3.2 一层权限门禁

```python
GroupRole = Literal["owner", "admin", "member", "unknown"]

@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    role: GroupRole
    reason: Literal["allowed", "member_denied", "role_unknown"]

# core/permissions.py：纯函数
def management_gate(role: GroupRole) -> GateDecision: ...

# adapters/memberfeed.py：调用 SDK，不访问数据库
def read_group_role(
    ctx, *, room: str, actor: str,
    expected_session: str | None = None,
) -> GroupRole: ...
```

执行顺序：

1. `app.py` 根据动作元数据判断是否需要群管理门禁。
2. 在业务事务外查询 `ctx.group_member_role()`。
3. 查询异常、异常返回值及缺失会话统一映射为 `unknown`。
4. `owner/admin` 放行；`member/unknown` 返回拒绝结果。
5. 拒绝时提交 `audit.result='denied'`，包含动作、调用者、目标、角色和拒绝原因，不记录原始消息正文。
6. 拒绝路径不得修改业务状态、扣款或建立定时配置。

适用动作：

- 小黑屋设置、解除；
- 黑名单增加、移除；
- 潜水统计；
- 群成员改名监测开关；
- 定时推送配置、启停；
- 将来保留的清理类管理动作。

普通成员玩法不调用该门禁。这里的 `改名` 指群管理开关，不包括 `宝宝 改名 X`。

角色来自可能滞后的本地缓存；v1 不为门禁主动同步成员。`unknown` 固定失败关闭，不回退至自建 owner 白名单。

#### 定时推送的配置入口补丁

现有 `scheduled_tasks.py` 保存、启停任务时没有可信的配置人微信身份；`on_schedule()` 也不携带人类配置者。面板增加一个可填写的 `wxid` 字段不能解决身份问题。

本增量的**推荐实现路径**：

- 群内由管理员通过已过门禁的命令发布推送计划。
- 将 `plan_id、room、action、参数、启用状态、revision、approved_by` 存入现有 `kv`。
- 面板任务只绑定 `plan_id/revision`，执行前读取已批准计划。
- 修改面板原始参数不能扩大计划范围；停用或修订后旧 revision 立即失效。
- 每次执行重查批准者当前缓存角色；`member/unknown` 停发并记拒绝审计。
- `quiz_gc` 等内部维护任务不伪装成群推送配置，不要求一个虚构的群管理员身份。

这条配置入口仍需用户在第 8 节拍板；不能在文档中宣称「现有面板保存时已经完成群角色门禁」。

### 3.3 `parse()`：只拆命令首部

```python
@dataclass(frozen=True)
class MentionSpan:
    start: int
    end: int                  # 左闭右开
    uid: str | None

@dataclass(frozen=True)
class Command:
    name: str
    action: str
    raw: str
    args: str
    tokens: tuple[str, ...]
    had_arg: bool
    separator: Literal["space", "#", "@"] | None
    mentions: tuple[MentionSpan, ...]

def parse(
    text: str,
    table: KeywordTable,
    *,
    mentions: tuple[MentionSpan, ...] = (),
) -> Command | None: ...
```

`mentions` 的输入下标对应原始 `text`。规范化器必须在 NFKC、去零宽字符和空白折叠过程中重映射范围，不能沿用变换前的下标。

具体算法替换原 `partition`：

```python
normalized, spans = normalize_with_mentions(text, mentions)

if not normalized or len(normalized) > 64:
    return None

# 句首是艾特，保持原方案：不作为命令触发。
if mention_starts_at(spans, 0) or normalized.startswith("@"):
    return None

cut = None
sep = None
i = 0

while i < len(normalized):
    if mention_starts_at(spans, i):
        cut, sep = i, "@"
        break

    ch = normalized[i]
    if ch == " ":
        cut, sep = i, "space"
        break
    if ch == "#":
        cut, sep = i, "#"
        break
    if ch == "@":             # 保留旧的无空格 @ 边界
        cut, sep = i, "@"
        break
    i += 1

head = normalized if cut is None else normalized[:cut]
trigger = table.lookup(head)
if trigger is None:
    return None

if cut is None:
    args = ""
elif sep == "@":
    args = normalized[cut:].strip()       # 艾特整体留在参数中
else:
    args = normalized[cut + 1:].strip()   # 只消耗这一个首部分隔符

# 参数里只按空白分词，不再 split("#")，也不再 lstrip("@")。
tokens = tuple(args.split())
```

返回结构由既有关键词映射填入 `name/action`，`raw` 保存原始输入，`mentions` 返回规范化文本中的完整艾特范围。裸命令行为、回声过滤及关键词冲突规则维持原方案。

#### 等价类与非等价类

| 输入 | 规范化后的 `args` | 结论 |
|---|---|---|
| `宝宝 改名 小明` | `改名 小明` | 基准 |
| `宝宝#改名 小明` | `改名 小明` | 与基准等价 |
| `宝宝＃改名 小明` | `改名 小明` | 与半角 `#` 等价 |
| `宝宝#改名#姓名` | `改名#姓名` | **只拆第一个 `#`**；不自动变成「改名 姓名」 |
| `宝宝 改名 A#B` | `改名 A#B` | 名字中的 `#` 保留 |
| `宝宝#改名 A＃B` | `改名 A#B` | 内部字符经 NFKC 变化，但不被拆分 |
| `结婚@昵称#甲`，有完整艾特范围 | `@昵称#甲` | 艾特中的 `#` 保留 |
| `@昵称#甲 宝宝 看` | — | 句首艾特，不触发 |
| `宝宝看` | — | 不新增前缀匹配 |

无结构化范围且没有可靠终止符的艾特串，不按显示昵称反推 uid；涉及指定对象的玩法返回「请使用有效艾特」的参数错误。

`我的婚姻、度蜜月、申请离婚、婚姻统计` 都是 `结婚` 的子命令，不注册新的四字顶层关键词，避免扩大与接龙插件的冲突面。

### 3.4 排行榜契约

```python
Board = Literal["coin", "activity", "marriage", "invite"]
Period = Literal["day", "week", "month", "all"]

def top(
    conn, *, board: Board, room: str,
    period: Period | None = None,
    now: int, limit: int = 10,
) -> tuple[RankRow, ...]: ...
```

确定的统计口径：

- 时间统一 `Asia/Shanghai`。
- 日：本地自然日；周：周一至周日；月：本地自然月；总：自本群开始采集以来。
- 活跃榜采用 `SUM(activity_daily.messages)`，沿用既有有效消息判定；不把缺失历史补成真实统计。
- 所有时间范围采用左闭右开边界。
- 活跃榜同分按 `uid` 稳定排序，名次使用连续序号。
- 金币榜为当前金币余额，不接受时间参数。
- 月榜、总榜直接查询日聚合，不生成 `rank_period` 快照；增加 `(room, day, uid)` 索引支持期间筛选。
- 婚姻榜的分数定义、邀请榜的重新入群计分和默认周期仍待拍板，不在实现时临时发明规则。

---

## 4. C1：邀请人修复与两个选项

### 4.1 推荐：修改框架，提供可核验的按人邀请统计

需要修改的不止 `_ACTOR` 和数据类，还包括中间重建路径。

| 位置 | 修改内容 |
|---|---|
| `wxbot_framework/group_events.py:22` | `_ACTOR` 从非捕获组改为命名捕获，如 `(?P<actor>...)`，保留现有占位符和「你/群主/管理员」的语法。 |
| `wxbot_framework/group_events.py:43-49` | 末尾新增 `actor_id: str = ""`，保留旧五参数构造兼容性。字段语义是事件操作者，不保证一定是邀请人。 |
| `wxbot_framework/group_events.py:100-106` | `_classify()` 返回目标标记与操作者标记；同步所有调用方解包。 |
| `wxbot_framework/group_events.py:109-202` | XML 按操作者自己的链接解析稳定 uid；纯文本只提供昵称时，`actor_id=""`，不匹配本地同名成员猜身份。 |
| `wxbot_framework/group_events.py:407-408` | `observe_system()` 重建事件时显式保留 `actor_id`，或使用 `dataclasses.replace()`；否则字段会在这里再次丢失。 |
| `wxbot_framework/group_events.py:303-355` | 成员快照差分产生的事件继续使用默认空操作者。 |
| Playhub `adapters/memberfeed.py` | 仅已确认邀请入群、有效 `actor_id` 的事件计入 `invite_credit`；成员差分补录不得生成邀请归属。 |

解析规则：

- 操作者链接必须唯一、可验证且属于操作者占位符，不能拿目标成员链接充当邀请人。
- 「你」只有在 `self_id` 有效时映射为机器人自身。
- 「群主」「管理员」等文字不映射到猜测的成员。
- 操作者解析失败不丢弃有效的成员事件，仍更新成员状态及群级统计。
- 邀请人为空时，按人榜不加分，同时记录「邀请人缺失」的可观测计数。
- 同一入群事件只记一次；现有 `(room, inviter, invitee, day)` 主键仅能执行特定的「同日去重」规则，不能自动代表所有重邀口径。

### 4.2 对 `group_notice` 的回归边界

`plugins/group_notice/__init__.py:18-46` 按字典键读取 `kind/room_id/member_id/nickname`；额外 `actor_id` 本身不影响消费。

真正需要防止的回归是：

1. 新正则改变既有目标识别，导致欢迎消息缺失或错人。
2. 将邀请人追加为目标成员，产生双欢迎。
3. 新返回元组未同步解包，引发整个系统消息解析失败。
4. 修改事件去重或基线逻辑，导致重复欢迎。
5. `observe_system()` 重建事件丢失邀请人。

`runtime.py` 的 `asdict(event)` 自然携带新增字段，不需要另造一套投递协议。保留 `group_notice` 对欢迎/退群通知的唯一所有权，Playhub 不再发送第二份欢迎。

### 4.3 两个选项的后果

| 选项 | 能交付什么 | 代价与风险 |
|---|---|---|
| **改框架，推荐** | 有证据时按人统计邀请；缺失时降级为群级统计 | 修改 fork 上游文件；增加正则、XML、事件传播及群通知回归测试；后续上游更新可能在同一区域发生合并冲突。 |
| 不改框架 | 群级进退人数、成员变化统计 | 按人邀请榜缺乏输入证据，只能显示「暂无可归属邀请记录」；不得用昵称猜邀请人，也不得把群级数量当成某个人的成绩。 |

推荐选择「改框架」。将补丁限制在一个框架文件，固定输入样例与输出断言；上游更新时先跑这些契约测试，再解决冲突。此风险与插件目录内开发不同，必须进入维护预算。

---

## 5. 文件预算增量

不为每个新子命令新建文件；继续使用既有引擎和 core 文件。

### 5.1 插件源码快照预算

| 文件 | 文件数变化 | 估算体积变化 | 内容 |
|---|---:|---:|---|
| `plugins/playhub/core/permissions.py` | +1 | +2 KiB | 角色决策类型及纯函数 |
| `plugins/playhub/core/wallet.py` | 0 | +3 KiB | 两币、扣减、分录与幂等 |
| `plugins/playhub/core/schema.sql` | 0 | +3 KiB | 钱包、流水、兼容视图及索引 |
| `plugins/playhub/router.py` | 0 | +2 KiB | `#` 与艾特范围解析 |
| `plugins/playhub/core/rank.py` | 0 | +2 KiB | 四榜及活跃期间查询 |
| `plugins/playhub/engines/social.py` | 0 | +2 KiB | 婚姻新增入口与基础宝宝裁剪后的净变化 |
| `plugins/playhub/adapters/memberfeed.py` | 0 | +1 KiB | 角色、邀请人读取 |
| `plugins/playhub/app.py` | 0 | +1 KiB | 权限及审计编排 |
| `plugins/playhub/engines/groupstat.py` | 0 | +1 KiB | 邀请降级与榜单 |
| `plugins/playhub/README.md` | 0 | +1 KiB | 帮助及 v1 范围 |
| **合计** | **+1** | **+18 KiB** | **41 / 305 → 42 / 323** |

结论：

- 距 64 文件限制剩 **22 文件**。
- 距 1 MiB 限制约剩 **701 KiB**。
- 单文件仍须低于 256 KiB；本轮没有接近上限的文件。
- 上表是设计预算，不是已生成代码的实测值；每批交付验收重新计量。

### 5.2 不进入插件快照的文件

- 修改 `wxbot_framework/group_events.py`，不新增框架文件。
- 测试位于 `tests/playhub/`，计划新增：

  ```text
  test_wallet.py
  test_permissions.py
  test_router.py
  test_rank.py
  test_invite.py
  test_help_scope.py
  test_schedule_gate.py
  ```

- 框架事件回归测试位于 `tests/playhub/test_invite.py`，直接调用框架解析器并验证 `group_notice` 消费契约；不为此复制框架实现。
- 不新增任何超级宝宝专用文件、独立婚姻引擎或新币种模块。
- 题库继续外置；不得将大规模内容文件搬回插件目录消耗源码快照配额。

---

## 6. 四批重排与人日变化

以下为设计估算，按 C1 采用「改框架」、定时推送采用「群内批准计划」路径计算。

| 批次 | 原范围/人日 | 范围增减与净变化 | 新人日 |
|---|---|---|---:|
| 第一批 | 路由、核心、签到抽签、金币榜、帮助；9 | 两币钱包及迁移 **+1.25**；一层权限基础 **+0.5**；`#`/艾特解析 **+0.25**。签到、抽签钻石入口随本批进入，但业务概率未拍板则不启用产出。 | **11** |
| 第二批 | 答题与内容管线；12 | 范围不变；复用第一批钱包和解析契约，不另建计分货币。 | **12** |
| 第三批 | 社交、抽奖、切磋、点歌、闲聊等；14 | 婚姻四子命令 **+1.25**；钻石买盲盒次数及清求婚次数 **+0.5**；裁掉超级宝宝扩展及其验收 **−0.75**；裁掉切磋监狱/医院联动及相关状态测试 **−1.0**。 | **14** |
| 第四批 | 群统计、管理、定时、榜单；10 | 四榜及活跃月/总 **+0.5**；C1 与群通知回归 **+0.75**；定时配置批准契约 **+0.5**；跨模块一致性验收 **+0.25**。 | **12** |
| **合计** | **45** | **新增 5.75，删减 1.75，净增 4** | **49** |

补充说明：

- 第一批先落权限框架，第四批再接群管理动作，避免最后才发现管理入口没有可信身份。
- 第三批明确验收「基础宝宝」，不按官方 20 条超级宝宝子指令验收。
- 第四批不实现潜水踢人、禁言或清人，也不将其列为后端待补功能。
- 原文 `rank_period` 从第四批必交项移除；本轮采用实时聚合查询。
- 原 45 人日没有细分到超级宝宝、监狱子项；以上删减是本次 WBS 重估，不代表旧文已有准确的单项报价。
- 49 × 15% = 7.35；总计划 **56.35 人日，取 57 人日**。
- 题库生产沿用原文独立预算，不并入本轮开发人日。
- 如选择扩展面板身份认证而不是群内批准计划，第四批需重新估算，不能套用上述 12 人日。

---

## 7. 测试与验收增量

### 7.1 权限

- `owner/admin` 可执行每个受控动作。
- `member` 被拒绝，业务状态不变，审计为 `denied`。
- `unknown`、角色读取异常、异常角色值均失败关闭。
- 自建 `owner_ids` 命中而微信群角色为 `member` 时仍被拒绝。
- 审计提交不被后续业务回滚吞掉。
- 宝宝改名等个人功能不因普通成员身份被误拦。
- 定时计划缺失、停用、revision 过期、批准者降权或变成 `unknown` 时不发送。
- 面板篡改 room、action、参数不能覆盖已批准计划。

### 7.2 钱包与迁移

- 两个独立连接同时从 100 金币扣 80：仅一个成功，最终余额 20，只出现一笔成功扣款流水。
- 金币不足不能扣钻石补足，钻石不足不能扣金币补足。
- 钱包更新后模拟流水插入失败：余额、次数和业务状态整体回滚。
- 同一 `operation_key` 重放：不重复扣款、不重复发奖。
- 同一操作内两个币种分录使用不同 `posting_no`，允许原子提交。
- 非法币种、零金额、负金额、浮点金额、整数上限溢出均被拒绝。
- 钻石正向来源只有三处；普通礼物、切磋、排行榜不能产生钻石。
- 清求婚次数固定扣 100 钻石；余额不足时次数不重置。
- 纳妾默认关闭时，300 钻石以上玩家同样不进入玩法、不扣款。
- 旧金币迁移前后余额和非零流水对应；兼容视图只返回金币分录。
- 迁移中断后重启，结果只能是旧版本完整状态或新版本完整状态。

### 7.3 解析

覆盖第 3.3 节全部等价类，额外测试：

- `宝宝#改名#姓名` 只有一次拆分。
- 参数多个 `#` 完整保留。
- 艾特昵称含 `#`、`＃`、空格时范围不被拆坏。
- NFKC 改变字符长度、去零宽字符后，艾特范围正确重映射。
- 首部空白、全角空白及重复空白。
- 句首艾特不触发。
- 超过原限制长度、未知关键词、关键词前缀碰撞行为保持。
- 不再通过 `lstrip("@")` 把普通文本伪装成有效成员 ID。

### 7.4 排行与统计

- 月榜跨月、跨年以及 2 月边界。
- 周榜以周一为起点，跨月不重置周窗口。
- 总榜包含 90 天以前的日聚合。
- 只统计本群、本账号数据库的数据。
- 同分排序稳定。
- 新接入群显示「自开始采集以来」，不声称拥有接入前历史。
- 金币榜不混入钻石。
- 潜水帮助和结果固定显示「只统计不清人」，没有清人执行分支。

### 7.5 邀请与群通知

- 有效邀请人链接能贯穿解析、事件跟踪、`asdict()`、插件消费。
- 邀请人缺失、同名、链接异常时只记群级变化，不给个人加分。
- 快照入群不产生虚构邀请人。
- 同一系统消息重放不重复计数。
- 多目标入群只给真实目标发送欢迎，不给邀请人额外欢迎。
- 旧五参数 `GroupMemberEvent` 构造仍可用。
- 无成员基线、重复成员变化等既有过滤行为保持。
- `group_notice` 面对新增字段保持欢迎次数、对象、内容契约不变。

### 7.6 范围与帮助一致性

- 宝宝帮助明确写「v1 是基础版」，只列四个保留入口。
- `宝宝 超极` 只返回 v2 说明，无扣款、升级和超级状态变化。
- 婚姻帮助包含新增四子命令及清次数；v2 子命令不展示成可用。
- 切磋帮助不出现可用的越狱、保释、偷跑、出院。
- 排行榜帮助只列四类，活跃榜列日/周/月/总。
- 纳妾默认关闭文案与实际执行一致。
- 顶层关键词注册表、总表、帮助、测试基线数量一致；同时修正原文 §4 汇总数字与逐行分类不一致的问题。

---

## 8. 风险与仍需用户拍板的项

### 8.1 13 项官方无规则：实现前口径清单

官方没有解释以下玩法的完整规则。不得把「对标小V」当成已确定的业务规格。

| 项目 | 实现前必须确认的内容 | 阻塞位置 |
|---|---|---|
| 签到 | 金币数量、连签中断处理、时区日切；第 7 天钻石概率和数量；每逢第 7 天还是仅首次达成 | 第一批奖励启用 |
| 点歌 | 数据源、费用、冷却、返回文字/链接形式、点歌失败是否退费 | 第三批外部接口及费用 |
| 抽签 | 每日次数、签等分布、金币奖励；上上签钻石概率和数量 | 第一批奖励启用 |
| 黄历 | 使用原方案本地确定性口径还是指定来源；日期和个人化规则 | 第三批内容规则 |
| 礼物 | 目录、金币价格、库存或永久物品、赠送是否转移价值、收礼收益 | 第三批经济模型 |
| 天气 | 服务商、默认城市、地点解析、接口额度和失败文案 | 第三批适配器 |
| 星座 | 查询语法、内容源、更新周期、是否个人化 | 第三批内容接入 |
| 求财神 | 次数、费用、奖励、是否只出文案 | 第三批经济模型 |
| 小黑屋 | 时长、对 Playhub 哪些功能生效、到期恢复、解除规则；只做插件内软限制 | 第四批管理动作 |
| 黑名单 | 群级作用域、屏蔽范围、是否保留帮助/申诉入口，与框架控制命令的边界 | 第四批管理动作 |
| 开宝箱 | 铜银金箱取得方式、开启成本、掉落表、金箱钻石概率和数量 | 第三批抽奖 |
| 潜水统计 | 默认天数、从未发言成员基准、退群成员/机器人是否排除、结果条数；**只统计不清人已定** | 第四批统计验收 |
| 智能聊天 | 保留既有聊天插件还是 Playhub 提供；使用何种真实开关、模型接入、触发条件和费用 | 聊天联动，不以空开关验收 |

原文已有提议可以保留为候选默认值，但这 13 项并非来自官方规则。涉及奖励、付费、外部服务和限制范围的项目，必须先形成配置或规则表再写业务分支。

### 8.2 其他未决项（2026-11-05 已全部拍板）

以下 1—5 项由设计方按经济一致性与可实施性定值（用户授权代决），6—7 项由用户确认。定值依据与数值锚见 §8.4。

1. **婚姻业务口径（已定）**
   - 度蜜月：**520 金币**/次，冷却 **24 小时**，给双方挂 **24 小时**「蜜月」标记（纯展示，无属性收益），婚姻统计 `honeymoon_count +1`。定位是**金币回收池**，不返利——金币必须有 sink，否则签到与抽奖会持续通胀。
   - 申请离婚：发起方付 **200 金币** 手续费；对方 **24 小时**内回复 `同意离婚` / `拒绝离婚`；超时自动失效并**全额退费**。**不提供单方强离**，避免恶意拆婚。
   - 婚姻统计字段（个人维度）：`结婚时间`、`婚龄（天）`、`蜜月次数`、`求婚次数（本人发起累计）`、`离婚次数（本人历史）`。
   - 求婚为**每日 3 次**限额，日界 `Asia/Shanghai`。

2. **婚姻榜定义（已定）**
   **现存婚姻按婚龄降序，夫妻合并为一行**（`A ❤ B · N 天`）；已离婚的婚姻不上榜；婚龄相同按结婚时间升序。理由：婚龄不可刷（离婚再结会归零），"结婚次数"可刷、"送花"我们没有花。

3. **邀请榜定义（已定）**
   默认 **总榜**（自本群开始采集以来），同时支持日/周/月；**同一 invitee 对同一 inviter 终身只计 1 次**（防两人互踢互拉刷分）；机器人自身邀请**计分**（管理员拉人算成绩）；`actor_id` 缺失只进群级统计、不计个人分。

4. **盲盒次数购买（已定）**
   **5 钻石** / 次（+1 次抽奖机会），每人**每日限购 2 次**，次数当日 `24:00 Asia/Shanghai` 清零；不累积、不退还、不可转赠。

5. **清求婚次数细则（已定）**
   **15 钻石**（**推翻原「100 钻石已定」**，理由见下）；清的是**当日**窗口（求婚为每日 3 次限额），清零后当日可重新计满；**当前计数为 0 时拒绝支付**并提示「当前无需清空」，避免误扣。
   > 推翻 100 钻石的理由：那是官方在**可充值**环境下的定价。本群无充值渠道，按 §8.4 的钻石产出（约 5.5 钻/周），100 钻≈5 个月才能攒够，等于把一个便利功能做成死内容。等比缩放为 15 钻（≈3 周）。

6. **C1 路径确认（已定：改框架）**
   修改 `wxbot_framework/group_events.py` 五处，按 §4.1 执行；把补丁限制在单个框架文件，并配 §7.5 的契约测试作为上游合并前的回归门。接受上游更新时在该区域的冲突风险。

7. **定时配置入口确认（已定：群内批准计划）**
   按 §3.2 末尾的「定时推送的配置入口补丁」执行：群内管理员过门禁后发布计划 → 面板只绑定 `plan_id/revision` → 每次执行重查批准者角色。**不在面板侧新造身份绑定**，第四批工时仍按 12 人日计。

8. **原 §9 尚存的集成事项**
   接龙插件是否允许定点修改、四个共享图片类关键词的所有权、关键词冲突时整插件失败或局部禁用、裸关键词提示策略、真实聊天开关、群空间面板接入，以及原文尚未确定的自身消息识别入口。

### 8.3 主导本轮设计的两个决定

- **独立两币钱包 + 单事务分录**：拒绝继续扩展 `player.coins` 的单币写路径，避免钻石功能上线后余额、费用、流水分别维护。代价是一张余额表和一次 schema 迁移，收益是所有经济操作使用同一并发与幂等契约。
- **活跃实时日聚合 + 永久保留聚合**：不引入 v1 快照维护任务，避免快照失效、回补和期间切换增加实现成本。代价是总榜查询随日聚合增长；先用索引与现有数据规模验收，达到性能瓶颈后再设计累计表或快照，不用删除历史换取「总榜」速度。

### 8.4 第一批所需数值锚（2026-11-05 定，用户授权代决）

设计原则：**金币靠固定产出，钻石靠稀有掉落**；每个新增金币来源都要配一个 sink（度蜜月 520、离婚 200、盲盒次数、礼物），否则不能上线。

#### 金币产出

| 来源 | 数值 | 频次上限 |
|---|---|---|
| 签到 | `30 + 5 × min(连签天数-1, 10)`，即 30 → 80 封顶 | 每日 1 次 |
| 抽签 | 见下方签等表（0–30） | 每日 1 次 |
| 开宝箱 | 铜 50 / 银 150 / 金 400 | 取决于箱源，见下 |
| 群内发言 | **不产金币** | — |

发言不产币是刻意的：发言给币会直接奖励刷屏，且与「活跃榜」重复计功。

#### 抽签签等表

| 签等 | 概率 | 金币 | 钻石 |
|---|---:|---:|---:|
| 上上签 | 1/12 | 30 | **+3** |
| 上吉 | 2/12 | 20 | — |
| 中吉 | 4/12 | 12 | — |
| 小吉 | 3/12 | 8 | — |
| 末吉 | 2/12 | 5 | — |

#### 宝箱

| 箱型 | 获取方式 | 开启成本 | 金币 | 钻石 |
|---|---|---|---:|---|
| 铜箱 | 每日签到赠 1 个 | 免费 | 50 | 1/20 概率 +1 |
| 银箱 | 活跃榜每周前 10 名各 1 个 | 免费 | 150 | 1/12 概率 +1 |
| 金箱 | 连签第 7 天赠 1 个 | 免费 | 400 | 1/8 概率 +2 |

#### 钻石收支总账（用于校验定价）

产出期望 ≈ 连签 3/周 + 上上签 1.75/周 + 铜箱 0.35/周 + 银箱 0.08/周 + 金箱 0.25/周 ≈ **5.5 钻/周**。

| 消耗项 | 价格 | 攒够所需 |
|---|---:|---|
| 盲盒次数（+1 次） | 5 钻 | ≈1 周 |
| 清求婚次数 | 15 钻 | ≈3 周 |
| 纳妾门槛（非扣款） | 30 钻 | ≈6 周 |

凡后续新增钻石消耗，价格必须落在这个表里校验；不允许出现「要攒半年」的定价。

#### 仍需在开工前补的外部依赖（不影响第一批）

- `点歌`：音频源（第三方 API 或本地曲库）、费用、冷却。
- `天气`：服务商与 key、默认城市。
- `星座`：内容源（本地静态表即可，无需外部接口）。
- `智能聊天`：复用既有 `mention_chat` 还是新接，模型与开关口径。
- `礼物`：物品表由谁定义（建议 v1 内置 8 件固定物品，价格 50–500 金币）。
