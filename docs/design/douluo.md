# douluo 插件设计方案（一期草案）

> 状态：**草案，未开工**。本文件只描述设计，不含实现代码。
> 决策人：用户。实现前需回答文末「需要拍板的问题」。
> 代号 `douluo`（内部代号，对外可换皮肤，见 §8.5）。

---

## 0. 读源码后纠正的两处前提

| 原假设 | 源码事实 | 影响 |
|---|---|---|
| `plugin.json` 有 `commands` 字段 | **不存在**。`discover()` 只读 `api` / `description` / `requires` / `message_types` / `default_enabled`（`wxbot_framework/plugins.py:176-189`）。 | 命令表只能写在 Python 里（`commands.py`），`plugin.json` 写 `commands` 是死字段。 |
| 参考库 `wuhun.category` 是武魂类别 | `dldlol_bot.sql:342-345` 里 `category` 存的是**职业系别**（敏攻系/食物系），武魂类别（兽武魂/器武魂）塞在 `des` 列。 | 我们的 `dict_wuhun` 必须把 `category`（武魂类别）与 `role`（职业系别）拆成两列。 |

---

## 1. 目录骨架（8 个文件）

```
plugins/douluo/
├── plugin.json     # 清单：api/message_types/requires/business（不写 commands，框架不认）
├── __init__.py     # Plugin 类：on_load/on_message/on_unload；只做「群聊守卫 → 解析 → 开事务 → 分发 → 回消息」
├── commands.py     # 前缀归一化、最长匹配、参数切分、COMMANDS 注册表、帮助条目表
├── rules.py        # 纯函数：经验曲线、属性成长、战力、猎环战斗、年限抽取、境界/槽位换算（无 IO、无 SQL、rng 注入）
├── settle.py       # 结算编排：一个命令一个 settle_*，读 → 算(rules) → 写(store) → 产出结果对象
├── store.py        # SQLite：连接、PRAGMA、迁移、tx() 事务上下文、全部 DAO（全仓唯一允许出现 SQL 的文件）
├── render.py       # 视图模型 → 中文文案；只做字符串，不碰 DB，不碰 ctx
└── schema.sql      # 18 张表 DDL + 索引 + 字典种子插入位（随代码发布）
```

切分理由：

- **`__init__.py` 必须瘦**。`PluginRecord._run` 在同一插件的单工作线程里串行执行回调（`wxbot_framework/plugins.py:86-111`），`on_message` 里任何耗时动作都会阻塞**这个群全部玩家**的命令。
- **`store.py` 是唯一 SQL 出口**，并加一条 CI 门禁：除 `store.py` 外任何 `.py` 出现 `SELECT `/`INSERT `/`UPDATE `/`DELETE ` 即测试失败。
- **`rules.py` 必须纯函数**，注入 `random.Random` 才能固定种子（与 `humanize.py:12` 一致）。
- **`render.py` 独立**：把 `render_help` / `render_status` 声明进 `plugin.json` 的 `business`，网页只开放这两个函数（`plugin_business.py:28-43`：1–8 个函数、按 `file`+`function` 定位、禁止 `on_load`/`on_unload`）。
- **`schema.sql` 单独放**：它在网页编辑器里不可编辑（`plugin_sources.py:14` 的 `EDITABLE` 只含 `.py/.json/.md/.txt`），改表必须走代码发布——正是想要的效果。

**数据文件绝不能放插件目录**：网页改文案会把插件源码快照复制到 `.runtime/plugin_code/douluo/versions/<rev>/`（`plugin_sources.py:72-80`），DB 若按 `__file__` 定位，改一次文案就换一份存档。正确位置：

```python
db_path = Path(ctx.project_root) / ".runtime" / "douluo" / "douluo.sqlite3"
```

`ctx.project_root` 由 `plugins.py:203` 设为 plugins 目录的父目录；`.runtime/` 与 `*.sqlite*` 都在 `.gitignore` 里。

---

## 2. 模块边界与依赖方向

```
                  ┌──────────────────────────────────────────┐
  微信消息 ──────► │ __init__.py  Plugin.on_message           │  只做：群聊守卫 / 解析 / 开事务 / 提交后回复
                  └───────────────┬──────────────────────────┘
                                  │ Parsed(name, args)
                  ┌───────────────▼──────────────────────────┐
                  │ commands.py   前缀归一化 + 最长匹配       │  纯函数，零依赖
                  └───────────────┬──────────────────────────┘
                                  │ handler = COMMANDS[name]
                  ┌───────────────▼──────────────────────────┐
                  │ settle.py     settle_awaken/meditate/…   │  编排层：事务内读 → 算 → 写
                  └──────┬─────────────────────┬─────────────┘
                         │ 纯计算               │ 持久化
          ┌──────────────▼───────┐   ┌─────────▼────────────────┐
          │ rules.py  公式/概率   │   │ store.py  DAO（唯一 SQL） │
          │ 无 IO，rng 注入       │   │ + tx() 事务上下文         │
          └──────────────────────┘   └─────────┬────────────────┘
                                               │
                                     ┌─────────▼──────────┐
                                     │ schema.sql  18 表   │
                                     └────────────────────┘

          render.py  ← settle 产出的 View 对象；单向依赖
```

依赖规则（review checklist）：

1. 箭头单向：`__init__ → commands → settle → {rules, render, store}`。`store.py` 不 import 其它插件内模块。
2. 只有 `store.py` 出现 SQL 字符串；`settle.py` 只调 `store.xxx()`。
3. `rules.py` 不 import `store`，只吃 dataclass，公式可脱离数据库单测。
4. `render.py` 不接收 `sqlite3.Row`，只接收 View dataclass，避免文案层耦合列名。
5. **事务内不做发送**。`ctx.reply` 走 `ServiceFacade.send_text`，内部有按字数的等待（`sdk.py:141-152`）；在事务里等 = 全群锁死 + 长事务持锁。顺序固定为「提交 → 发送」。

---

## 3. 关键接口签名

### 3.1 为什么每个方法首参恒为 `world_id`

- 表设计上 `world_id` 是每张表第一列且是所有索引前缀，SQL 里必然出现 `WHERE world_id = ?`；做成首参，漏了就 `TypeError`，**世界隔离错误在开发期就崩**，不会变成线上串档。
- 日志/异常栈里能直接看到是哪个群出的问题。
- `tx` 里也存一份 `world_id`，DAO 入口做 `if tx.world_id != world_id: raise` 防御断言——两份冗余换来"复制粘贴写错群"的零容忍。

### 3.2 数据访问层（`store.py`）

```python
WorldId = int
PlayerId = int
Now = int          # Unix 秒，全库统一

class Tx:
    """事务句柄。不暴露 execute()——只有 store.py 内部能拿到 sqlite3.Cursor。"""
    world_id: WorldId
    now: Now

class Store:
    def __init__(self, path: Path, *, now: Callable[[], Now] = _unix_now) -> None: ...
    def open(self) -> None:
        """建库 + PRAGMA + 按 schema_version 逐版迁移。幂等。"""
    def close(self) -> None: ...

    @contextmanager
    def tx(self, world_id: WorldId) -> Iterator[Tx]:
        """写事务。内部 BEGIN IMMEDIATE；正常 COMMIT，异常 ROLLBACK。

        不用 DEFERRED：先读后写会从读锁升级写锁，并发下直接 SQLITE_BUSY 且无法等待重试；
        IMMEDIATE 一开始就拿写锁，配合 busy_timeout 变成排队而不是报错。
        """

    @contextmanager
    def read(self, world_id: WorldId) -> Iterator[Tx]:
        """只读快照；面板/背包走这条，永不升级成写锁。"""

# ---- 世界 ----
def ensure_world(world_id, chat_id, *, tx) -> WorldRow: ...
def touch_world(world_id, day_key, *, tx) -> None: ...
def next_event_seq(world_id, *, tx) -> int: ...
def append_event(world_id, seq, kind, actor_wxid, payload: dict, *, tx) -> None: ...

# ---- 玩家 ----
def get_player_by_wxid(world_id, wxid, *, tx) -> PlayerRow | None: ...
def insert_player(world_id, wxid, nickname, wuhun_code, *, tx) -> PlayerRow:
    """UNIQUE(world_id, wxid) 兜底：并发/重投递下第二次插入 IntegrityError，翻译成「已觉醒」。"""
def load_player(world_id, player_id, *, tx) -> PlayerRow: ...
def save_player(world_id, row, *, tx) -> None:
    """整行 UPDATE + WHERE level=? AND exp=?（乐观校验），命中 0 行抛 Conflict。"""

# ---- 每日计数 / buff / 魂环 / 魂技 / 背包 ----
def load_daily(world_id, player_id, day_key, *, tx) -> DailyRow: ...
def save_daily(world_id, row, *, tx) -> None: ...
def bump_daily(world_id, player_id, day_key, field, delta, *, tx) -> int:
    """INSERT ... ON CONFLICT DO UPDATE 自增，返回新值；不读改写，少一次竞态窗口。"""
def load_buffs(world_id, player_id, at: Now, *, tx) -> tuple[BuffRow, ...]: ...
def load_rings(world_id, player_id, *, tx) -> tuple[RingRow, ...]: ...
def insert_ring(world_id, player_id, slot, year_code, beast_code, skill_code, power_mult, *, tx) -> RingRow: ...
def load_skills(world_id, player_id, *, tx) -> tuple[SkillRow, ...]: ...
def load_inventory(world_id, player_id, *, tx) -> tuple[BagRow, ...]: ...
def add_item(world_id, player_id, item_code, qty, *, tx) -> int: ...

# ---- 字典（world_id=0 全局默认，>0 该世界覆写）----
def dict_wuhun_all(world_id, *, tx) -> tuple[WuhunDef, ...]: ...
def dict_wuhun_one(world_id, code, *, tx) -> WuhunDef: ...
def dict_rank_of(world_id, level, *, tx) -> RankDef: ...
def dict_years_for_slot(world_id, slot, *, tx) -> tuple[YearDef, ...]: ...
def dict_beasts(world_id, map_code | None, level, *, tx) -> tuple[BeastDef, ...]: ...
def dict_maps(world_id, level, *, tx) -> tuple[MapDef, ...]: ...
def dict_skill_one(world_id, code, *, tx) -> SkillDef: ...

# ---- 账本 / 幂等 ----
def add_ledger(world_id, player_id, kind, delta, reason, ref, *, tx) -> None: ...
def claim_op(world_id, dedup_key, player_id | None, command, *, tx) -> bool:
    """INSERT INTO op_log(...) 抢占 dedup_key。False = 已处理过，调用方静默返回。"""
def finish_op(world_id, dedup_key, outcome, detail: dict, *, tx) -> None: ...
```

### 3.3 玩法层（`settle.py`，全部在调用方给的事务内运行）

```python
@dataclass(frozen=True)
class Outcome:
    """所有 settle_* 的统一返回：文案已渲染好，命令层只管 ctx.reply。"""
    ok: bool
    code: str        # 'ok' | 'not_awakened' | 'already' | 'cooldown' | 'exhausted'
                     # | 'not_bottleneck' | 'exp_not_full' | 'no_beast' | 'defeat' | ...
    text: str
    changed: bool    # 是否需要写库（失败分支通常 False，但扣次数/扣血要 True）

def settle_awaken(world_id, wxid, nickname, *, now, rng, tx) -> Outcome: ...
def settle_meditate(world_id, wxid, now, day_key, *, rng, tx) -> Outcome: ...
def settle_hunt(world_id, wxid, map_code, beast_code, now, day_key, *, rng, tx) -> Outcome: ...
def settle_absorb(world_id, wxid, beast_code, slot, now, *, rng, tx) -> Outcome: ...
    """必须与 settle_hunt 同事务，否则「兽死了但环没拿到」丢档。"""
def settle_signin(world_id, wxid, now, day_key, *, tx) -> Outcome: ...
def settle_status(world_id, wxid, now, day_key, *, tx) -> Outcome: ...
def settle_bag(world_id, wxid, now, *, tx) -> Outcome: ...
def settle_help(world_id, *, tx) -> Outcome: ...
```

### 3.4 规则层（`rules.py`，纯函数）

```python
def rank_of(level: int) -> tuple[str, str]              # (rank_code, 中文名)
def slot_for(level: int) -> int                          # 瓶颈等级 9/19/…/99 → 1..10
def is_bottleneck(level: int, exp: int) -> bool           # level%10==9 且 exp >= exp_needed(level)
def exp_needed(level: int) -> int                         # §6.1
def meditate_yield(level, ring_sum, rng) -> int
def absorb_attrs(base: WuhunDef, level: int, ring_sum) -> Attrs
def combat_power(a: Attrs) -> int
def hunt_resolve(p: Attrs, p_hp_cur: int, b: BeastDef, rng) -> HuntOutcome
def draw_year(slot: int, beast_year: YearDef, pool, rng) -> tuple[YearDef, str]  # (年限, '同档'|'升档'|'降档')
def absorb_chance(year: YearDef) -> float
def draw_skill(year: YearDef, role: str, skills, rng) -> SkillDef
```

### 3.5 命令解析（`commands.py`）

```python
PREFIXES: Final[tuple[str, ...]] = ('.', '。', '．', '·')
MAX_INPUT_CHARS: Final[int] = 64

@dataclass(frozen=True)
class Parsed:
    name: str                  # 命令名（不含前缀），如 '冥想'
    args: tuple[str, ...]
    raw: str

def normalize(text: str) -> str:
    """去零宽字符/首尾空白、NBSP 与全角空格→半角、连续空白折叠。不做全角转半角。"""

def parse(text: str, known: Container[str]) -> Parsed | None:
    """首字符必须在 PREFIXES 内；按 len(name) 降序尝试（最长匹配优先）；
    命中后要求剩余为空或以空白开头，否则视为「点命令但不像已知命令」→ None（静默）。
    未知命令、超长输入一律 None。"""

def build_commands() -> dict[str, Handler]: ...
def help_entries() -> tuple[HelpEntry, ...]: ...
```

`parse` 的验收样例（进单测）：

| 输入 | 结果 |
|---|---|
| `.冥想` | `Parsed('冥想', ())` |
| `。冥想` / `．冥想` / `·冥想` | 同上（中文句号键） |
| `  .冥想  ` | 同上 |
| `.冥想 abc` | `Parsed('冥想', ('abc',))` |
| `.冥想abc` | `None`（无此命令，静默） |
| `.觉醒武魂` | 注册了就命中长名，否则 `None` |
| `冥想` | `None`（无前缀，交给其它插件/大模型） |
| `.` / `.  ` | `None` |
| `.` + 100 字 | `None`（超 `MAX_INPUT_CHARS`） |

### 3.6 渲染（`render.py`）

```python
def render_status(v: StatusView) -> str
def render_ring_bar(rings, slots_unlocked) -> str      # ◆◇
def render_bag(v: BagView) -> str
def render_help(entries, limit) -> str
def render_meditate(v: MeditateView) -> str
def render_hunt(v: HuntView) -> str
```

全部 `View -> str` 纯函数，无 `ctx`、无 DB。因此 `render_status` / `render_help` 可安全声明进 `business`。

### 3.7 插件入口

```python
class Plugin:
    def on_load(self, ctx) -> None:
        """建 Store(<project_root>/.runtime/douluo/douluo.sqlite3)、open()、建命令表、读配置。"""
    def on_message(self, message, ctx) -> None:
        """1) type==1 且 is_group，否则 return（一期只做群）
           2) normalize；首字符不在 PREFIXES 立即 return（绝不吃普通聊天）
           3) parse；None → return
           4) with store.tx(world_id) as tx: claim_op(...) → 重复则 return
           5) outcome = COMMANDS[parsed.name](...)
           6) 提交后再 ctx.reply(message, outcome.text)"""
    def on_unload(self, ctx) -> None:
        """store.close()（plugins.py:113-122）。"""
```

`on_message` 签名与 `Plugin()` 无参构造由 `plugins.py:217-219` 固定；`on_load`/`on_unload` 可选。`Message` 字段见 `sdk.py:24-37`（`text` / `chat_id` / `sender_id` / `type` / `is_group` / `is_self` / `sequence` / `session_id` / `received_at`）。

`plugin.json` 定稿：

```json
{
  "api": "v1",
  "description": "斗罗大陆文字修仙：觉醒、冥想、猎环、签到，一个群一个世界",
  "message_types": [1],
  "requires": ["text.receive", "text.send"],
  "default_enabled": false,
  "business": [
    {"function": "Plugin.on_message", "label": "命令入口"},
    {"file": "render.py", "function": "render_status", "label": "状态面板文案"},
    {"file": "render.py", "function": "render_help", "label": "帮助文案"}
  ]
}
```

`default_enabled: false`：该插件会往群里发大量文本，必须显式 `enable douluo`。

---

## 4. 一期 DDL 草案（18 张表）

### 4.1 PRAGMA（对齐 `members.py`）

```sql
PRAGMA journal_mode = WAL;      -- 读写不互斥
PRAGMA synchronous = NORMAL;    -- WAL 下的合理折中
PRAGMA foreign_keys = ON;       -- 每个连接都要设，见 members.py:186
PRAGMA busy_timeout = 5000;     -- 并发下排队 5 秒而不是立刻报错
```

现有成员库即 `sqlite3.connect(path, timeout=5, check_same_thread=False)` + `row_factory = sqlite3.Row` + `foreign_keys=ON` + `WAL`（`members.py:183-187`），保持一致。

### 4.2 两条贯穿全库的约定

- **字典表用 `world_id = 0` 表示「全局默认行」，`world_id > 0` 表示「该世界覆写行」**。读取按 `world_id IN (0, :world_id)` + 覆写优先。
- **由此产生一个限制：玩家表不能对字典表建外键**。SQLite 复合外键要求父键完全相等，`player_ring(world_id=7, year_code='nian')` 无法指向 `dict_ring_year(world_id=0, …)`。字典引用的正确性由 **CHECK 约束 + 应用层校验 + 字典种子随 schema 发布** 保证。玩家表之间（同 world_id）外键全开。

### 4.3 DDL

```sql
-- 1. schema 版本（world_id=0 为库级）
CREATE TABLE IF NOT EXISTS schema_version (
    world_id   INTEGER NOT NULL,
    version    INTEGER NOT NULL CHECK (version >= 1),
    applied_at INTEGER NOT NULL,
    note       TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (world_id, version)
);

-- 2. 世界：一个微信群一个世界；游标列也在这里
CREATE TABLE IF NOT EXISTS world (
    world_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id        TEXT    NOT NULL UNIQUE,          -- 群 ID，路由入口
    name           TEXT    NOT NULL DEFAULT '',
    day_key        TEXT    NOT NULL DEFAULT '',      -- 游标：当前自然日 YYYY-MM-DD
    last_event_seq INTEGER NOT NULL DEFAULT 0,       -- 游标：world_event 最新序号
    last_active_at INTEGER NOT NULL DEFAULT 0,
    status         INTEGER NOT NULL DEFAULT 1 CHECK (status IN (0,1,2)),
    rules_version  INTEGER NOT NULL DEFAULT 1,       -- 数值公式版本，二期调平衡用
    config_json    TEXT    NOT NULL DEFAULT '{}',
    created_at     INTEGER NOT NULL
);
-- world(chat_id) 的 UNIQUE 是唯一不以 world_id 打头的索引：收消息时只拿得到 chat_id。

-- 3. 世界事件流
CREATE TABLE IF NOT EXISTS world_event (
    world_id     INTEGER NOT NULL,
    seq          INTEGER NOT NULL,
    kind         TEXT    NOT NULL,        -- 'awaken'|'ring'|'hunt'|'signin'|'tick'
    actor_wxid   TEXT    NOT NULL DEFAULT '',
    payload_json TEXT    NOT NULL DEFAULT '{}',
    created_at   INTEGER NOT NULL,
    PRIMARY KEY (world_id, seq),
    FOREIGN KEY (world_id) REFERENCES world(world_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS world_event_by_kind ON world_event(world_id, kind, seq DESC);

-- 4. 玩家（属性不落库，由 level+wuhun+rings 推导；见 §6.2）
CREATE TABLE IF NOT EXISTS player (
    world_id    INTEGER NOT NULL,
    player_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    wxid        TEXT    NOT NULL,
    nickname    TEXT    NOT NULL DEFAULT '',
    wuhun_code  TEXT    NOT NULL,
    level       INTEGER NOT NULL DEFAULT 1  CHECK (level BETWEEN 1 AND 100),
    exp         INTEGER NOT NULL DEFAULT 0  CHECK (exp >= 0),
    hp_cur      INTEGER NOT NULL DEFAULT 0  CHECK (hp_cur >= 0),
    hunli_cur   INTEGER NOT NULL DEFAULT 0  CHECK (hunli_cur >= 0),
    money       INTEGER NOT NULL DEFAULT 0  CHECK (money >= 0),
    hunt_streak_fail INTEGER NOT NULL DEFAULT 0 CHECK (hunt_streak_fail >= 0),
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL,
    UNIQUE (world_id, wxid),                 -- 一个群一个人一个角色
    UNIQUE (world_id, player_id),            -- 供子表复合外键使用
    FOREIGN KEY (world_id) REFERENCES world(world_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS player_by_level  ON player(world_id, level DESC, exp DESC);
CREATE INDEX IF NOT EXISTS player_by_active ON player(world_id, updated_at DESC);

-- 5. 玩家状态 buff
CREATE TABLE IF NOT EXISTS player_buff (
    world_id   INTEGER NOT NULL,
    player_id  INTEGER NOT NULL,
    buff_code  TEXT    NOT NULL,             -- 'rujing'|'xuanzhong'|'lengque'…
    stacks     INTEGER NOT NULL DEFAULT 1 CHECK (stacks >= 0),
    value_json TEXT    NOT NULL DEFAULT '{}',
    source     TEXT    NOT NULL DEFAULT '',
    expires_at INTEGER NOT NULL DEFAULT 0,   -- 0 = 永不过期
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (world_id, player_id, buff_code),
    FOREIGN KEY (world_id, player_id) REFERENCES player(world_id, player_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS player_buff_by_expire ON player_buff(world_id, expires_at);

-- 6. 玩家每日计数（一行一天）
CREATE TABLE IF NOT EXISTS player_daily (
    world_id         INTEGER NOT NULL,
    player_id        INTEGER NOT NULL,
    day_key          TEXT    NOT NULL,
    meditate_used    INTEGER NOT NULL DEFAULT 0 CHECK (meditate_used >= 0),
    meditate_last_at INTEGER NOT NULL DEFAULT 0,
    hunt_used        INTEGER NOT NULL DEFAULT 0 CHECK (hunt_used >= 0),
    hunt_fail        INTEGER NOT NULL DEFAULT 0 CHECK (hunt_fail >= 0),
    signin_at        INTEGER NOT NULL DEFAULT 0,
    signin_streak    INTEGER NOT NULL DEFAULT 0 CHECK (signin_streak >= 0),
    extra_json       TEXT    NOT NULL DEFAULT '{}',
    PRIMARY KEY (world_id, player_id, day_key),
    FOREIGN KEY (world_id, player_id) REFERENCES player(world_id, player_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS player_daily_by_day ON player_daily(world_id, day_key);

-- 7. 玩家魂环（9 魂环 + 1 神环槽）
CREATE TABLE IF NOT EXISTS player_ring (
    world_id    INTEGER NOT NULL,
    player_id   INTEGER NOT NULL,
    slot        INTEGER NOT NULL CHECK (slot BETWEEN 1 AND 10),   -- 10 = 神环
    year_code   TEXT    NOT NULL,
    beast_code  TEXT    NOT NULL DEFAULT '',
    skill_code  TEXT    NOT NULL,
    power_mult  REAL    NOT NULL CHECK (power_mult > 0),          -- 吸收瞬间的年限加成快照
    absorbed_at INTEGER NOT NULL,
    PRIMARY KEY (world_id, player_id, slot),
    FOREIGN KEY (world_id, player_id) REFERENCES player(world_id, player_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS player_ring_by_year ON player_ring(world_id, year_code);

-- 8. 玩家魂技
CREATE TABLE IF NOT EXISTS player_skill (
    world_id   INTEGER NOT NULL,
    player_id  INTEGER NOT NULL,
    skill_code TEXT    NOT NULL,
    slot       INTEGER NOT NULL CHECK (slot BETWEEN 1 AND 10),
    mastery    INTEGER NOT NULL DEFAULT 0 CHECK (mastery >= 0),
    learned_at INTEGER NOT NULL,
    PRIMARY KEY (world_id, player_id, skill_code),
    FOREIGN KEY (world_id, player_id) REFERENCES player(world_id, player_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS player_skill_by_slot ON player_skill(world_id, player_id, slot);

-- 9. 字典：等级境界
CREATE TABLE IF NOT EXISTS dict_rank (
    world_id  INTEGER NOT NULL DEFAULT 0,
    rank_code TEXT    NOT NULL,
    name      TEXT    NOT NULL,          -- 魂士/魂师/大魂师/…
    min_level INTEGER NOT NULL CHECK (min_level BETWEEN 1 AND 100),
    max_level INTEGER NOT NULL CHECK (max_level BETWEEN 1 AND 100),
    tier      INTEGER NOT NULL CHECK (tier BETWEEN 0 AND 10),
    title     TEXT    NOT NULL DEFAULT '',   -- 封号斗罗的封号
    PRIMARY KEY (world_id, rank_code),
    CHECK (min_level <= max_level)
);
CREATE INDEX IF NOT EXISTS dict_rank_by_level ON dict_rank(world_id, min_level);

-- 10. 字典：武魂（category = 武魂类别，role = 职业系别，两列分开）
CREATE TABLE IF NOT EXISTS dict_wuhun (
    world_id   INTEGER NOT NULL DEFAULT 0,
    wuhun_code TEXT    NOT NULL,
    name       TEXT    NOT NULL,
    category   TEXT    NOT NULL CHECK (category IN ('兽武魂','器武魂','本体武魂')),
    role       TEXT    NOT NULL CHECK (role IN ('强攻','敏攻','控制','防御','辅助','食物','治疗')),
    rarity     INTEGER NOT NULL DEFAULT 1 CHECK (rarity BETWEEN 1 AND 5),
    atk_base   INTEGER NOT NULL CHECK (atk_base > 0),
    def_base   INTEGER NOT NULL CHECK (def_base > 0),
    hp_base    INTEGER NOT NULL CHECK (hp_base > 0),
    spd_base   INTEGER NOT NULL CHECK (spd_base > 0),
    crit_base  INTEGER NOT NULL DEFAULT 5 CHECK (crit_base BETWEEN 0 AND 100),
    atk_grow   REAL    NOT NULL DEFAULT 1.0 CHECK (atk_grow > 0),
    def_grow   REAL    NOT NULL DEFAULT 1.0 CHECK (def_grow > 0),
    hp_grow    REAL    NOT NULL DEFAULT 1.0 CHECK (hp_grow > 0),
    spd_grow   REAL    NOT NULL DEFAULT 1.0 CHECK (spd_grow > 0),
    weight     INTEGER NOT NULL DEFAULT 10 CHECK (weight >= 0),   -- 觉醒随机权重
    desc       TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (world_id, wuhun_code)
);

-- 11. 字典：魂环年限档位（min_slot = 第几槽起才可能出这一档）
CREATE TABLE IF NOT EXISTS dict_ring_year (
    world_id   INTEGER NOT NULL DEFAULT 0,
    year_code  TEXT    NOT NULL,          -- 'shi'|'bai'|'qian'|'wan'|'swan'|'shen'
    name       TEXT    NOT NULL,          -- 十年/百年/千年/万年/十万年/神级
    color      TEXT    NOT NULL CHECK (color IN ('白','黄','紫','黑','红','金')),
    min_slot   INTEGER NOT NULL CHECK (min_slot BETWEEN 1 AND 10),
    power_mult REAL    NOT NULL CHECK (power_mult >= 1.0),
    weight     INTEGER NOT NULL DEFAULT 0 CHECK (weight >= 0),
    exp_bonus  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (world_id, year_code)
);
CREATE INDEX IF NOT EXISTS dict_ring_year_by_slot ON dict_ring_year(world_id, min_slot, weight DESC);

-- 12. 字典：魂技
CREATE TABLE IF NOT EXISTS dict_skill (
    world_id    INTEGER NOT NULL DEFAULT 0,
    skill_code  TEXT    NOT NULL,
    name        TEXT    NOT NULL,
    kind        TEXT    NOT NULL CHECK (kind IN ('主动','被动')),
    year_code   TEXT    NOT NULL,                 -- 出自哪一档年限
    role_hint   TEXT    NOT NULL DEFAULT '',      -- 职业系别偏好，空 = 通用
    power_ratio REAL    NOT NULL DEFAULT 1.0 CHECK (power_ratio > 0),
    mp_cost     INTEGER NOT NULL DEFAULT 0 CHECK (mp_cost >= 0),
    cooldown    INTEGER NOT NULL DEFAULT 0 CHECK (cooldown >= 0),
    effect_json TEXT    NOT NULL DEFAULT '{}',
    desc        TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (world_id, skill_code)
);
CREATE INDEX IF NOT EXISTS dict_skill_by_year ON dict_skill(world_id, year_code, role_hint);

-- 13. 字典：魂兽（按地图分布，不是随机刷）
CREATE TABLE IF NOT EXISTS dict_beast (
    world_id   INTEGER NOT NULL DEFAULT 0,
    beast_code TEXT    NOT NULL,
    name       TEXT    NOT NULL,
    map_code   TEXT    NOT NULL,
    year_code  TEXT    NOT NULL,
    level_req  INTEGER NOT NULL DEFAULT 1 CHECK (level_req BETWEEN 1 AND 100),
    atk        INTEGER NOT NULL CHECK (atk > 0),
    def        INTEGER NOT NULL CHECK (def >= 0),
    hp         INTEGER NOT NULL CHECK (hp > 0),
    spd        INTEGER NOT NULL CHECK (spd > 0),
    exp_reward INTEGER NOT NULL DEFAULT 0 CHECK (exp_reward >= 0),
    drop_json  TEXT    NOT NULL DEFAULT '{}',
    desc       TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (world_id, beast_code)
);
CREATE INDEX IF NOT EXISTS dict_beast_by_map ON dict_beast(world_id, map_code, year_code, level_req);

-- 14. 字典：地图
CREATE TABLE IF NOT EXISTS dict_map (
    world_id   INTEGER NOT NULL DEFAULT 0,
    map_code   TEXT    NOT NULL,
    name       TEXT    NOT NULL,
    min_level  INTEGER NOT NULL DEFAULT 1 CHECK (min_level BETWEEN 1 AND 100),
    danger     INTEGER NOT NULL DEFAULT 0 CHECK (danger BETWEEN 0 AND 100),
    bias_json  TEXT    NOT NULL DEFAULT '{}',   -- 该地图对年限权重的偏向
    desc       TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (world_id, map_code)
);

-- 15. 字典：物品
CREATE TABLE IF NOT EXISTS dict_item (
    world_id    INTEGER NOT NULL DEFAULT 0,
    item_code   TEXT    NOT NULL,
    name        TEXT    NOT NULL,
    kind        TEXT    NOT NULL CHECK (kind IN ('消耗','材料','魂导器','任务','凭证')),
    stack_max   INTEGER NOT NULL DEFAULT 99 CHECK (stack_max >= 1),
    usable      INTEGER NOT NULL DEFAULT 0 CHECK (usable IN (0,1)),
    price       INTEGER NOT NULL DEFAULT 0 CHECK (price >= 0),
    effect_json TEXT    NOT NULL DEFAULT '{}',
    desc        TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (world_id, item_code)
);

-- 16. 背包
CREATE TABLE IF NOT EXISTS inventory (
    world_id   INTEGER NOT NULL,
    player_id  INTEGER NOT NULL,
    item_code  TEXT    NOT NULL,
    qty        INTEGER NOT NULL DEFAULT 0 CHECK (qty >= 0),
    bound      INTEGER NOT NULL DEFAULT 0 CHECK (bound IN (0,1)),
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (world_id, player_id, item_code),
    FOREIGN KEY (world_id, player_id) REFERENCES player(world_id, player_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS inventory_by_item ON inventory(world_id, item_code, qty);

-- 17. 经济流水（钱/经验/物品全部走这里，永不裸改数字）
CREATE TABLE IF NOT EXISTS ledger (
    world_id      INTEGER NOT NULL,
    entry_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id     INTEGER NOT NULL,
    kind          TEXT    NOT NULL CHECK (kind IN ('exp','money','item','hunli')),
    delta         INTEGER NOT NULL,          -- 有符号
    balance_after INTEGER NOT NULL,
    reason        TEXT    NOT NULL,          -- 'meditate'|'hunt'|'absorb'|'signin'|'awaken'
    ref           TEXT    NOT NULL DEFAULT '',
    created_at    INTEGER NOT NULL,
    FOREIGN KEY (world_id, player_id) REFERENCES player(world_id, player_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ledger_by_player ON ledger(world_id, player_id, entry_id DESC);
CREATE INDEX IF NOT EXISTS ledger_by_kind   ON ledger(world_id, kind, created_at DESC);

-- 18. 操作日志 —— 兼幂等账本
CREATE TABLE IF NOT EXISTS op_log (
    world_id    INTEGER NOT NULL,
    op_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key   TEXT    NOT NULL,            -- '{session_id}:{sequence}' 或 'h:{sha1(...)}'
    player_id   INTEGER,
    command     TEXT    NOT NULL,
    outcome     TEXT    NOT NULL CHECK (outcome IN ('ok','rejected','ignored','conflict')),
    detail_json TEXT    NOT NULL DEFAULT '{}',
    created_at  INTEGER NOT NULL,
    UNIQUE (world_id, dedup_key),
    FOREIGN KEY (world_id, player_id) REFERENCES player(world_id, player_id)
);
CREATE INDEX IF NOT EXISTS op_log_by_player ON op_log(world_id, player_id, op_id DESC);
```

### 4.4 表分类

| # | 表 | 类别 | 备注 |
|---|---|---|---|
| 1 | `schema_version` | 元数据 | world_id=0 为库级 |
| 2 | `world` | 玩家数据（容器） | 兼「世界游标」 |
| 3 | `world_event` | 玩家数据 | 事件流水 |
| 4 | `player` | 玩家数据 | |
| 5 | `player_buff` | 玩家数据 | |
| 6 | `player_daily` | 玩家数据 | |
| 7 | `player_ring` | 玩家数据 | |
| 8 | `player_skill` | 玩家数据 | |
| 9 | `dict_rank` | **字典（只读）** | 随 `schema.sql` 发布 |
| 10 | `dict_wuhun` | **字典** | |
| 11 | `dict_ring_year` | **字典** | |
| 12 | `dict_skill` | **字典** | |
| 13 | `dict_beast` | **字典** | |
| 14 | `dict_map` | **字典** | |
| 15 | `dict_item` | **字典** | |
| 16 | `inventory` | 玩家数据 | |
| 17 | `ledger` | 玩家数据 | 只增不改 |
| 18 | `op_log` | 玩家数据 | 幂等 + 审计 |

### 4.5 为什么第 18 张是 `op_log`

一期就必须有「微信重复投递的判据落地」。可选两个方案，选后者：

- 单开 `cmd_dedup`：干净，但同一份信息存两处（去重键 + 审计），过期清理要两边一起做。
- **`op_log` + `UNIQUE(world_id, dedup_key)`**：一条 INSERT 同时完成「抢占」和「留痕」，`claim_op()` 返回 `False` 即重复投递。代价是会随消息量增长，二期加「保留 90 天」清理任务。

另外三张必须存在但没被点名的表：`dict_map`（魂兽分布需要地图维度才有 `min_level` 门槛与文案）、`player_skill`（熟练度/替换是二期明确要做的，先建表避免改表）、`world_event`（世界播报/排行/二期世界 boss 的钩子）。

---

## 5. 七条命令的完整交互与结算

### 5.0 共用前置（写进 `__init__.py`，不重复）

| 顺序 | 检查 | 处理 |
|---|---|---|
| 1 | `type != 1` 或 `not is_group` | `return`，不回话（私聊点名也不理） |
| 2 | `is_self` | `return` |
| 3 | `normalize` 后为空 / 首字符不在 `PREFIXES` | `return`（**绝不吞普通聊天**） |
| 4 | `parse()` 返回 `None` | `return`，静默 |
| 5 | `claim_op()` 为 `False` | `return`，静默（重复投递） |
| 6 | 会话白名单 | 不用管：`runtime._emit_chat` 已按 `chat_allowlist` 过滤（`runtime.py:229-230`） |

统一时间源：`day_key = datetime.now(BEIJING).date().isoformat()`，日界用本地时区（同 `members.py:257-261` 的 `_day_bounds`），时间戳一律存 `INTEGER` Unix 秒。

### 5.1 `.觉醒`

- **校验顺序**
  1. 群里没有世界行就 `ensure_world(chat_id)`。
  2. `get_player_by_wxid` 非空 → 拒绝：`你已在【{world_name}】觉醒过武魂：{wuhun_name}。此处已是你的一世，换群才有另一世。`
  3. 拒绝也写 `op_log(outcome='rejected')`，便于排查「为什么没反应」。
- **结算**：按 `dict_wuhun.weight` 加权随机 → `insert_player`（level=1, exp=0, hp_cur/hunli_cur 满值, money=0）→ `ledger(kind='exp', delta=0, reason='awaken')` → `world_event(kind='awaken')`。
- **事务边界**：`ensure_world` + `insert_player` + `ledger` + `world_event` + `op_log` **同一 tx**，否则会出现「世界建了但人没建」的空群。
- **成功文案**
  ```
  {nickname}，{wuhun_name}已醒。
  武魂：{category}·{role}系　先天魂力：{hunli}/10
  自今日起，你就是{world_name}的魂士了。
  （.冥想 修魂力　.状态 看面板　.帮助 看全部）
  ```

### 5.2 `.冥想`

- **校验顺序（均在事务内读最新值）**
  1. 未觉醒 → `你尚未觉醒武魂。先 .觉醒。`
  2. 冷却 `now - meditate_last_at < 45s` → `气息未匀，{n} 秒后再来。`
  3. 次数 `>= 60` → `今日已入定六十次，魂力难再凝聚。明日再来，或 .签到 领些机缘。`
  4. `is_bottleneck` → 允许冥想但**不涨经验**：`魂力已至瓶颈，{exp}/{need}。再坐无用，当往 .猎环。` 计一次次数（刻意的负反馈）。
- **结算**
  1. `gain = rules.meditate_yield(level, ring_sum, rng)`
  2. `exp' = min(exp + gain, exp_needed(level))`（上限封顶，不溢出）
  3. 非瓶颈且 `exp' >= exp_needed(level)` → 升级（一期一次最多 1 级）
  4. `meditate_used += 1`、`meditate_last_at = now`（`bump_daily()` 自增）
  5. `ledger(kind='exp', delta=gain, balance_after=exp')`
- **事务边界**：读 player + 读 daily + 写 player + 写 daily + 写 ledger + op_log 同一 tx。
- **成功文案**
  ```
  {nickname} 入定一炷香，魂力 +{gain}。
  {rank}{level}级　{bar} {exp}/{need}
  今日冥想 {used}/{limit}{tail}
  ```
  `tail` ∈ {`""`, `"　（瓶颈已至，.猎环 可取环）"`}
- **升级追加一行**
  ```
  魂力冲关，{rank_old}{level_old} → {rank_new}{level_new}。
  {if level_new % 10 == 0}　第 {slot_new} 魂环位已开，只待猎环。
  ```

### 5.3 `.猎环`

一期拆成「看候选」与「开打」两种形态：

```
.猎环                    → 列出可用地图与魂兽（只读，不消耗次数）
.猎环 星斗大森林          → 列出该地图魂兽
.猎环 星斗大森林 曼陀罗蛇   → 开战
```

- **校验顺序**
  1. 未觉醒 → 同上。
  2. **瓶颈**：`level % 10 != 9` → `尚未至瓶颈。{rank}{level}级，先 .冥想 攒满魂力。`
  3. **满经验**：`exp < exp_needed(level)` → `魂力未满（{exp}/{need}），此时猎环无环可承。`
  4. **次数**：`hunt_used >= 3` → `今日三次猎环已尽。明日再入山。`
  5. **连败禁猎**：`hunt_streak_fail >= 3` → `三战三败，气血未复。明日再战。`
  6. **冷却**：有未过期 `lengque` → `伤势未愈，{n} 分钟后方可再战。`
  7. 地图/魂兽存在性与 `level_req <= level` → `此地魂兽非你此刻可敌。看看 .猎环 列出的选项。`
- **战斗判定**
  ```
  rounds = ceil(beast.hp / max(1, p_atk*mult - beast.def*0.4))
  if rounds > 12: 判负（拖成消耗战）
  taken  = rounds * max(1, beast.atk*mult - p_def*0.5)
  win    = taken < hp_cur
  ```
- **战败代价**（不掉级、不掉已得魂环）
  1. `hp_cur = max(1, hp_cur - taken)`
  2. `hunt_used += 1`、`hunt_fail += 1`、`hunt_streak_fail += 1`
  3. 挂 `player_buff('lengque', expires_at = now + 1800)`
  4. 掉 10% 当前经验（不是等级），`ledger` 记负值
- **战胜 → 吸收（同一事务内立刻做，不做二次确认）**
  1. 年限抽取：`pool = dict_years_for_slot(slot)`，权重 `year.weight * map.bias`，再按「同档 70 / 升档 22 / 降档 8」修正；升档后仍须 `min_slot <= slot`，否则压回同档。
  2. `absorb_chance(year)`；**失败**：魂环崩碎，`hunt_used += 1`，hp 掉到 1/3，给 `exp_reward * 0.5` 安慰经验。
  3. **成功**：`insert_ring(...)`（`power_mult` 存快照）→ `draw_skill()` 写 `player_skill` → `bump_daily(hunt_used)` → 升级（`exp -= exp_needed(level_old)`，余额保留）→ 若新等级是 10 的倍数则 `world_event(kind='ring')`。
  4. `ledger` + `world_event(kind='hunt')`。
- **事务边界**：`settle_hunt` 与 `settle_absorb` 必须在同一 `tx`。
- **文案**
  ```
  【胜】{map}深处，{beast_name}哀鸣倒地。
        一缕{color}光没入眉心——{year_name}魂环，第{slot}环，成。
        {skill_name}：{skill_desc}
        {rank}{level}级　战力 {cp}

  【败】{beast_name}一爪落下，你倒退百步，衣襟尽赤。
        今日猎环剩 {left} 次，气血未复（三十分钟）。

  【吸】{year_name}之力反噬，魂环在指尖炸开。
        修为未进，魂力大损，所幸性命无碍——{beast_name}之环，与你无缘。

  【表】{world_name}·可猎之地
        ◆ {map_name}（{danger}）  {beast1}·{year1}／{beast2}·{year2}
        用法：.猎环 {map_name} {beast_name}
  ```

### 5.4 `.状态`

- 只读，走 `store.read()`，不占写锁。未觉醒 → 同上。
- 事务边界：单条只读快照，事务内不做任何写（不清过期 buff，清理放到下一次写事务）。
- 文案
  ```
  {nickname}　{rank}{level}级
  武魂　{wuhun_name}（{category}·{role}）
  魂力　{bar} {exp}/{need}{"　※瓶颈" if bottleneck}
  气血　{hp_cur}/{hp_max}　战力 {cp}
  魂环　{ring_bar}   （◆已凝 ◇空缺）
  　一环　{year1}{color}·{skill1}
  ```

### 5.5 `.背包`

- 只读。未觉醒 → 同上。空 → `{nickname} 的行囊空空。金魂币 {money}。`
- 文案：`{nickname} 的行囊　金魂币 {money}` / `　{name} ×{qty}　{desc 前 12 字}`

### 5.6 `.签到`

- 校验：未觉醒 → 同上；`signin_at > 0` → `今日已签到（连续 {streak} 日）。`
- 结算
  1. 连续天数：读昨日行 `signin_streak`，有则 `+1`，无则 `1`（一期只重置 streak，不设惩罚）。
  2. `exp_gain = 40 + 20*min(streak,7)`，`money_gain = 30 + 10*min(streak,7)`。
  3. 经验按 `.冥想` 同样封顶与升级规则入账（同样被瓶颈卡住）。
  4. `ledger` 两条（exp / money）+ `world_event(kind='signin')`。
- 事务边界：读昨日行 + 写今日行 + 写 player + 两条 ledger + op_log，同一 tx。

### 5.7 `.帮助`

- 纯静态渲染，不读 player。
  ```
  斗罗·{world_name}　可用命令
  .觉醒　第一次踏入此界
  .冥想　凝练魂力（每日 60 次）
  .猎环　瓶颈时搏命取环（每日 3 次）
  .状态　查看面板
  .背包　查看行囊
  .签到　每日一次
  .帮助　就是这条
  ```
- 与现有 `/help`（`idiom_chain/__init__.py:13-15` 的 HELP 常量）前缀不同，不冲突。

### 5.8 同群多人并发

1. **一期不会并发**：框架给每个插件独立队列 + 单工作线程，回调串行（`plugins.py:86-111`）；定时动作也抢同一把 `callback_lock`（`plugins.py:313-336`）。
2. **串行意味着「一个人卡住全群」**：所以 `tx` 内绝不做发送、绝不打 LLM、绝不做文件 IO。发送统一放提交之后。
3. **多进程靠 SQLite 自守**：每账号一个 DB 文件；同库多连接用 `BEGIN IMMEDIATE + busy_timeout=5000`；`save_player` 的 `WHERE level=? AND exp=?` 乐观校验把「两进程同时结算同一玩家」变成可见错误而不是静默覆盖。

### 5.9 微信重复投递

三道保险：

1. **幂等键** `dedup_key = f"{session_id}:{sequence}"`（`sdk.py:34`）；`sequence == 0` 时退化为 `h:{sha1(chat_id|sender_id|text|int(received_at))}`。
2. **DB 唯一约束** `op_log UNIQUE(world_id, dedup_key)`，`claim_op()` 撞键返回 `False` → 静默返回。
3. **业务约束兜底**：`player UNIQUE(world_id, wxid)`；`player_ring PK(world_id, player_id, slot)`；`player_daily` 计数用 `ON CONFLICT DO UPDATE`。

先例：成员库同样做法（`members.py:202-203` 建 `UNIQUE INDEX ON messages(account, session_id, sequence) WHERE sequence > 0`）。

---

## 6. 数值公式草案

### 6.1 经验曲线与冥想产出

```python
def T(L: int) -> int:                   # 升一级需要的「标准冥想次数」，唯一手感旋钮
    n = L - 1
    return round(3 + 0.9*n + 0.012*n*n)

def yield_base(L: int) -> int:          # 单次冥想基准产出
    return 6 + 3 * L

def exp_needed(L: int) -> int:          # 升到 L+1 所需
    return round(T(L) * yield_base(L))

def meditate_yield(L, ring_sum, rng):
    roll = rng.uniform(0.95, 1.05)
    return round(yield_base(L) * (1 + 0.05 * ring_sum) * roll)
```

- `T(L)` 单调非降，前期 3 次/级、后期 200 次/级；改平衡只改三个系数。
- `exp_needed` 用「基准产出 × 次数」表达，可读性高于纯幂律。
- **瓶颈**：`L % 10 == 9` 时 `exp` 封顶 `exp_needed(L)`，必须 `.猎环` 成功才进下一档，升级时 `exp -= exp_needed(L)` 保留余额。
- 每日 60 次、冷却 45 秒（可配置），满勤约 45 分钟吃完。

| L | 境界 | 产出/次 | need(L+1) | 次数 | 满勤天数 |
|---|---|---|---|---|---|
| 1 | 魂士 | 9 | 27 | 3 | 0.05 |
| 9 | 魂士（瓶颈） | 33 | 363 | 11 | 0.18 |
| 10 | 魂师 | 36 | 432 | 12 | 0.20 |
| 20 | 大魂师 | 66 | 1584 | 24 | 0.40 |
| 50 | 魂宗 | 156 | 11856 | 76 | 1.3 |
| 90 | 魂斗罗 | 276 | 49128 | 178 | 3.0 |
| 99 | 封号斗罗（神环瓶颈） | 303 | 62115 | 205 | 3.4 |

累计：**1→10 级 1503 exp / 63 次 / 约 1 天**；**1→20 级 10941 / 236 次 / 约 4 天**；**1→30 级 35961 / 538 次 / 约 9 天**。一期目标是「活跃玩家两周摸到 30 级、见到第三环」。

### 6.2 属性成长与战力

`ring_sum = Σ player_ring.power_mult`（0 环为 0）：

```python
hp  = round(hp_base  * hp_grow  * (1 + 0.55*(L-1)) * (1 + 0.060*ring_sum))
atk = round(atk_base * atk_grow * (1 + 0.42*(L-1)) * (1 + 0.080*ring_sum))
def_= round(def_base * def_grow * (1 + 0.30*(L-1)) * (1 + 0.050*ring_sum))
spd = round(spd_base * spd_grow * (1 + 0.15*(L-1)))
crit= min(60, crit_base + L//2)
cp  = round(atk*2.4 + def_*1.8 + hp*0.35 + spd*1.5 + crit*6)
```

**属性不落 `player` 表**：`level + wuhun + rings` 是唯一真相，属性是纯函数输出。调平衡不用洗数据；代价是每次面板重算（几十次浮点，可忽略）。

模板武魂 `atk_base=10, def_base=6, hp_base=100, spd_base=8, crit_base=5, grow=1.2`：

| L | 环 | Σmult | atk | def | hp | cp |
|---|---|---|---|---|---|---|
| 1 | 0 | 0 | 12 | 6 | 100 | 130 |
| 9 | 0 | 0 | 52 | 20 | 540 | 395 |
| 10 | 1 | 1.35 | 68 | 24 | 787 | 512 |
| 20 | 2 | 3.15 | 133 | 45 | 1350 | 1017 |
| 50 | 4 | 7.00 | 421 | 115 | 3630 | 2873 |
| 90 | 8 | 16.0 | 1054 | 291 | 9400 | 6908 |
| 99 | 9 | 18.0 | 1178 | 321 | 10330 | 7703 |

### 6.3 魂兽强度表（一期只标定 1–30）

| 年限 | 示例地图 | level_req | atk | def | hp | spd | exp_reward |
|---|---|---|---|---|---|---|---|
| 十年 | 星斗大森林·外围 | 9 | 12 | 4 | 130 | 6 | 40 |
| 百年 | 星斗大森林 | 9 | 24 | 9 | 340 | 8 | 120 |
| 千年 | 猎魂森林 | 19 | 62 | 24 | 900 | 12 | 400 |
| 万年 | 冰封森林 | 29 | 130 | 52 | 1500 | 16 | 1200 |
| 十万年 | 深海 | 79 | 320 | 130 | 4200 | 22 | 4000 |
| 神级 | 神界裂隙 | 99 | 900 | 400 | 15000 | 32 | 12000 |

战斗解算校验：

- **L=9 打百年**：`rounds=ceil(340/48.4)=7`，`taken=7*(24-10)=98 < 540` → 胜，余 442。
- **L=9 打千年**（越级）：`rounds=ceil(900/42)=22 > 12` → 判负。等级门槛即由此落地。
- **L=19 打千年**：`rounds=ceil(900/118.4)=8`，`taken=8*(62-22)=320 < 1297` → 胜。
- **L=29 打万年**：`rounds=ceil(1500/183)=9`，`taken=9*(130-34)=864 < 2048` → 胜。

结论：同档能赢但不再无脑（余血 60–70%），越一档必输，越两档反而可能「捡漏」——为二期组队越级留空间。

### 6.4 魂环年限概率表

候选池：`dict_ring_year` 中 `min_slot <= 当前槽位` 的档位。

| 年限 | 色 | min_slot | power_mult | 基准权重 | 1 槽实际 | 9 槽实际 |
|---|---|---|---|---|---|---|
| 十年 | 白 | 1 | 1.00 | 45 | 60.0% | 45.0% |
| 百年 | 黄 | 1 | 1.35 | 30 | 40.0% | 30.0% |
| 千年 | 紫 | 2 | 1.80 | 16 | — | 16.0% |
| 万年 | 黑 | 4 | 2.40 | 7 | — | 7.0% |
| 十万年 | 红 | 7 | 3.20 | 2 | — | 2.0% |
| 神级 | 金 | 10 | 4.50 | 0 | — | 仅第 10 槽 |

叠加修正：同档 70% / 升一档 22% / 降一档 8%（升档后须满足 `min_slot <= slot`，否则压回同档；已在最低档则按同档）。

吸收成功率：

```python
absorb_chance = clamp(0.35, 0.98, 0.98 - 0.18 * (power_mult - 1.0))
```

→ 十年 0.98 / 百年 0.917 / 千年 0.836 / 万年 0.728 / 十万年 0.584 / 神级 0.35。代价是**吸收失败**（环碎、掉血、白费次数），不是掉级。

魂技抽取：`dict_skill` 中 `year_code` 匹配且 `role_hint ∈ ('', 玩家 role)` 的集合均匀抽；同档已有同技能则改抽「未拥有的最高 power_ratio」。

### 6.5 通胀控制

- `power_mult` 在吸收时**快照**进 `player_ring`：以后调字典不影响老玩家。
- `world.rules_version` 记录建档公式版本，二期调平衡可按版本补偿，不全库洗数据。
- 经验只经 `meditate` / `signin` / `hunt` 三个入口，**全部走 `ledger`**；`ledger` 只增不改，数字对不上能复算。

---

## 7. 分期路线

| 期 | 内容 | 新增表 | 新增命令 | 估算 |
|---|---|---|---|---|
| **一期（本设计）** | 存档骨架、觉醒、冥想、猎环（含吸收）、面板、背包、签到、帮助、幂等与审计 | 18 张全建（含字典种子） | 7 | **12–15 人日** |
| **二期** | 组队猎环（分摊伤害）、魂兽巢穴（世界 boss）、商店与买卖、物品使用、魂技升级 | +4：`team`、`team_member`、`shop_stock`、`market_order` | +5：`.组队` `.巢穴` `.商店` `.买` `.用` | 15–20 人日 |
| **三期** | 切磋/PvP 排行、宗门、魂导器与附魔、封号系统、赛季重置 | +6：`arena_match`、`arena_rank`、`sect`、`sect_member`、`equip`、`season` | +6：`.切磋` `.排行` `.宗门` `.打造` `.封号` `.赛季` | 20–25 人日 |

一期内部拆分：

| 步骤 | 人日 |
|---|---|
| `schema.sql` + `store.py`（连接/迁移/tx/DAO）+ DAO 单测 | 4 |
| `rules.py`（曲线/属性/战斗/抽取）+ 公式单测与手感验证 | 2.5 |
| `commands.py` + `__init__.py`（前缀、最长匹配、幂等、事务编排） | 1.5 |
| `render.py` + 七条命令文案 | 2 |
| `settle.py`（七条结算） | 3 |
| 字典种子（武魂 12 / 魂兽 12 / 魂技 18 / 地图 4 / 物品 6） | 1.5 |
| 实机联调（4.1.15.56 / 4.2.0.11 双版本、群聊投递、重复投递） | 1.5 |

---

## 8. 风险

### 8.1 与现有插件的命令前缀冲突（最高优先级）

| 插件 | 行为 | 对 `.冥想` 的影响 | 证据 |
|---|---|---|---|
| `idiom_chain` | 接龙进行中，**任何不以 `/` 开头的非空文本都送去 LLM 判定**并回一句「不是成语」 | 严重：接龙一开，`.冥想` 被吃掉 | `idiom_chain/__init__.py:110`（`if not game or not text or text.startswith("/"): return`） |
| `picture_idiom` | 同上（猜成语进行中） | 同类问题 | `text.py:8-17` 的 `GROUP_CONTROL_COMMANDS` 白名单不含点命令 |
| `group_memory` | 不拦消息，但把 ≥2 字文本喂进记忆/面具 | 轻：`.冥想` `.猎环` 污染人格与亲和度统计 | `group_memory/__init__.py:44-46` |
| `mention_chat` | 只在 `is_at_self` 响应 | 无影响 | `mention_chat/__init__.py:40-43` |

**结论**：彻底解法是在 `idiom_chain` / `picture_idiom` 的文本入口各加一行「点前缀豁免」（`if text[:1] in '.。．·': return`）。这要改现有文件，需用户点头（§9 第 4 条）。

### 8.2 SQLite 并发

单进程内不会并发（插件单线程）；风险集中在多进程共享同一 DB 文件（虽有 `locking.py:10-25` 的 `msvcrt` 单实例锁）。缓解：`BEGIN IMMEDIATE` + `busy_timeout=5000` + 每账号独立 DB + `save_player` 乐观校验。残余风险：WAL 文件在 Windows 被占用时无法备份/删除，需 `on_unload` 可靠 `close()`。

### 8.3 微信消息重复投递

`sequence` 在合并转发等场景可能为 0（`sdk.py:34` 默认值），退化为内容哈希 + 时间桶。极端情况：同一玩家连发两条完全相同的 `.冥想` 且落在同一秒会被判重复，少给一次经验。方向是「少给」不是「多给」，可接受；根治需后端提供稳定消息 ID（框架改动）。

### 8.4 数值通胀

`power_mult` 快照 + `rules_version` 已处理「改字典影响老档」。真正通胀源是二期商店/交易：一旦物品可交易，`ledger` 必须是**唯一**数值变更入口，禁止 `UPDATE player SET money = money + ?` 绕过账本。赛季重置（三期）需 `season` 表 + 快照。

### 8.5 IP 名词公开分发

「斗罗大陆」及魂师/魂环/封号斗罗等术语属第三方 IP。自有群内使用风险低；一旦随仓库或插件市场分发，建议武魂/魂兽/境界全部走字典表 + 一套中性皮肤别名（`dict_*` 已数据驱动，换名零代码），插件名 `douluo` 仅作内部代号。是否公开分发需拍板。

### 8.6 参考资料许可证

`.runtime/research/dldlol-Bot/` 是 **LGPL-3.0**，本设计只取了四个玩法维度：`wuhun.category`（且发现它其实是系别）、`hunji.yearbuff`、`hunshou.map`、`weapons.grade_limit`（`dldlol_bot.sql:322-336 / 120-134 / 151-165 / 298-307`）。**没有任何 SQL 文本、字段名、代码进入本设计**——表名列名约束全部重新设计。它用 `hh1..hh10` 十列存十槽（`dldlol_bot.sql:92-105`）、把角色塞进 JSON 列（`:230-246`），这两点正是反面教材，已用行式 + 列式正规化解决。

### 8.7 其它

- `store.py` 单文件会膨胀（一期 600–800 行），二期拆包需放宽「8 个文件」上限。
- `ctx.project_root` 单账号模式 = 仓库根（`main.py:62`），存档落 `D:\project\helper-main\.runtime\douluo\`；`.gitignore` 已覆盖，需提醒用户别手工清理。
- 一期没有「离线结算」：玩家不发言就不长经验。要做每日自动结算需声明 `scheduled_actions`（`docs/development/plugins.md:63-94`），且只在机器人运行时生效。

---

## 9. 需要拍板的问题

1. `.猎环` 每日 3 次 / 战败冷却 30 分钟，接受还是改成「每天 1 次、赌一把」的高压版？
2. 一期是否覆盖 99→100 的神环（第 10 槽），还是先封顶 99 级、把「神」留到三期？
3. 觉醒武魂是纯随机，还是给 3 选 1 让玩家有掌控感？
4. 是否允许改动 `idiom_chain` / `picture_idiom` 各加一行「点前缀豁免」？这是解决命令冲突的唯一彻底办法，但会动现有文件。
5. 存档是否支持「转世/迁移」（换群保留角色）？现设计是严格隔离、绝不互通。
6. 插件是否准备公开分发？决定要不要做 IP 名词中性皮肤。
7. 每日冥想 60 次 / 冷却 45 秒是否接受？它决定「1→30 级约 9 天」的节奏。
8. `store.py` 单文件承载全部 DAO（约 700 行）可接受，还是现在就放宽到 9–10 个文件、拆成 `store/` 包？
