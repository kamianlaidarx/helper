-- plugins/playhub/core/schema.sql
-- 说明：.sql 不在网页可编辑集合内（plugin_sources.py:14 的 EDITABLE 仅 .py/.json/.md/.txt），
--       因此 DDL 只能随版本发布，改表必须走 meta.schema_version 迁移。

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- ---------- 玩家档案 ----------
-- player 只留资料与等级经验；货币全部在 wallet（理由见 3.2.3）
CREATE TABLE IF NOT EXISTS player (
  room       TEXT    NOT NULL,
  uid        TEXT    NOT NULL,
  nickname   TEXT    NOT NULL DEFAULT '',
  level      INTEGER NOT NULL DEFAULT 1 CHECK (level >= 1),
  exp        INTEGER NOT NULL DEFAULT 0 CHECK (exp >= 0),
  created_at INTEGER NOT NULL,
  last_seen  INTEGER NOT NULL,
  flags      INTEGER NOT NULL DEFAULT 0,   -- 位标记：1=新人已引导 2=禁用游戏
  PRIMARY KEY (room, uid)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS player_by_exp ON player(room, exp DESC, uid);

-- ---------- 两币钱包 ----------
-- 一个玩家每个币种一行；建立玩家时初始化 coin 与 diamond 两行（均为 0）
CREATE TABLE IF NOT EXISTS wallet (
  room       TEXT    NOT NULL,
  uid        TEXT    NOT NULL,
  currency   TEXT    NOT NULL CHECK (currency IN ('coin', 'diamond')),
  balance    INTEGER NOT NULL DEFAULT 0
             CHECK (typeof(balance) = 'integer' AND balance >= 0),
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (room, uid, currency),
  FOREIGN KEY (room, uid) REFERENCES player(room, uid) ON DELETE RESTRICT
) WITHOUT ROWID;

-- 金币榜 / 钻石榜的驱动索引
CREATE INDEX IF NOT EXISTS wallet_by_rank ON wallet(room, currency, balance DESC, uid);

-- 单事务分录：一次业务操作 = 一个 operation_key + 若干 posting_no
CREATE TABLE IF NOT EXISTS wallet_ledger (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  room          TEXT    NOT NULL,
  uid           TEXT    NOT NULL,
  currency      TEXT    NOT NULL CHECK (currency IN ('coin', 'diamond')),
  delta         INTEGER NOT NULL CHECK (typeof(delta) = 'integer' AND delta <> 0),
  balance_after INTEGER NOT NULL
                CHECK (typeof(balance_after) = 'integer' AND balance_after >= 0),
  reason        TEXT    NOT NULL,            -- 代码枚举，不接受聊天文本
  ref           TEXT,
  operation_key TEXT    NOT NULL,
  posting_no    INTEGER NOT NULL DEFAULT 0 CHECK (posting_no >= 0),
  at            INTEGER NOT NULL,
  FOREIGN KEY (room, uid, currency) REFERENCES wallet(room, uid, currency) ON DELETE RESTRICT,
  UNIQUE (room, operation_key, posting_no)
);
CREATE INDEX IF NOT EXISTS wallet_ledger_user
  ON wallet_ledger(room, uid, currency, at DESC, id DESC);
CREATE INDEX IF NOT EXISTS wallet_ledger_reason
  ON wallet_ledger(room, currency, reason, at DESC);

-- 旧代码兼容视图：只读；新代码**禁止**向视图 INSERT，一律走 core/wallet.py
CREATE VIEW IF NOT EXISTS coin_ledger AS
  SELECT id, room, uid, delta, balance_after, reason, ref, at
    FROM wallet_ledger WHERE currency = 'coin';

-- 金币榜：视图本身不能建索引，靠 wallet_by_rank 支撑
CREATE VIEW IF NOT EXISTS v_rank_coin AS
  SELECT room, uid, balance AS score FROM wallet WHERE currency = 'coin';

-- ---------- 每日次数 / 冷却 ----------
CREATE TABLE IF NOT EXISTS daily_counter (
  room       TEXT    NOT NULL,
  uid        TEXT    NOT NULL,
  day        TEXT    NOT NULL,
  key        TEXT    NOT NULL,               -- signin / quiz / blind_box / wealth ...
  used       INTEGER NOT NULL DEFAULT 0,
  cap        INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (room, uid, day, key)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS cooldown (
  room     TEXT    NOT NULL,
  uid      TEXT    NOT NULL,
  key      TEXT    NOT NULL,
  ready_at INTEGER NOT NULL,                 -- unix 秒（墙上时钟，故意用时间而非单调时钟：重启后仍然生效）
  PRIMARY KEY (room, uid, key)
) WITHOUT ROWID;

-- ---------- 物品字典 / 背包 ----------
CREATE TABLE IF NOT EXISTS item_def (
  world     TEXT    NOT NULL DEFAULT '*',    -- '*' = 全局默认行；否则为 room 覆盖行
  item_id   TEXT    NOT NULL,
  kind      TEXT    NOT NULL,                -- gift/box/material/badge/baby_item
  name      TEXT    NOT NULL,
  rarity    INTEGER NOT NULL DEFAULT 0,      -- 0..5
  price     INTEGER,                         -- NULL = 非卖品
  stackable INTEGER NOT NULL DEFAULT 1,
  weight    INTEGER NOT NULL DEFAULT 1,      -- 抽奖权重
  enabled   INTEGER NOT NULL DEFAULT 1,
  attrs     TEXT,                            -- JSON：加成、文案、礼盒内容指向
  PRIMARY KEY (world, item_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS inventory (
  room    TEXT    NOT NULL,
  uid     TEXT    NOT NULL,
  item_id TEXT    NOT NULL,
  qty     INTEGER NOT NULL CHECK (qty >= 0),
  PRIMARY KEY (room, uid, item_id)
) WITHOUT ROWID;
-- 注意：故意不给 inventory.item_id 建外键指向 item_def，
-- 原因见 3.3「复合外键必须完全相等，字典表有 world='*' 兜底行」。

-- ---------- 关系（结婚/闺蜜/兄弟/雇佣/纳妾/宝宝双亲）----------
CREATE TABLE IF NOT EXISTS pair (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  room     TEXT    NOT NULL,
  kind     TEXT    NOT NULL CHECK (kind IN ('marriage','concubine','sister','brother','employ','baby_parent')),
  low_uid  TEXT    NOT NULL,
  high_uid TEXT    NOT NULL,
  state    TEXT    NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','active','ended')),
  since    INTEGER,
  ended_at INTEGER,
  by_uid   TEXT,                              -- 发起人
  meta     TEXT,                              -- JSON：彩礼、工资、备注
  created_at INTEGER NOT NULL,
  CHECK (low_uid <= high_uid),
  CHECK (low_uid <> high_uid)
);
-- 同一对同类关系只允许一条未结束记录（pending 与 active 都算占用，避免反复刷请求）
CREATE UNIQUE INDEX IF NOT EXISTS pair_active_pair
  ON pair(room, kind, low_uid, high_uid) WHERE state IN ('pending','active');
CREATE INDEX IF NOT EXISTS pair_by_low  ON pair(room, low_uid, kind, state);
CREATE INDEX IF NOT EXISTS pair_by_high ON pair(room, high_uid, kind, state);
-- 一人一婚（只约束 marriage）：两侧各一个部分唯一索引；
-- 注意这两个索引只能各自兜住"处于低位/高位"的重复，
-- "A 在甲记录里是 low、在乙记录里是 high"的重复靠事务内 SELECT 校验（BEGIN IMMEDIATE 下安全）。
CREATE UNIQUE INDEX IF NOT EXISTS marriage_one_low
  ON pair(room, low_uid)  WHERE kind = 'marriage'  AND state = 'active';
CREATE UNIQUE INDEX IF NOT EXISTS marriage_one_high
  ON pair(room, high_uid) WHERE kind = 'marriage'  AND state = 'active';

-- ---------- 宝宝 ----------
CREATE TABLE IF NOT EXISTS baby (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  room        TEXT    NOT NULL,
  name        TEXT    NOT NULL,
  owner_uid   TEXT    NOT NULL,              -- 主养育人（父母双方都记在 parent_*）
  parent_low  TEXT    NOT NULL,
  parent_high TEXT,
  born_at     INTEGER NOT NULL,
  stage       INTEGER NOT NULL DEFAULT 0,    -- 0胎儿/1婴儿/2幼儿/3儿童/4少年/5超极
  exp         INTEGER NOT NULL DEFAULT 0,
  care        INTEGER NOT NULL DEFAULT 100 CHECK (care BETWEEN 0 AND 100),
  is_super    INTEGER NOT NULL DEFAULT 0,
  last_care_at INTEGER,
  attrs       TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS baby_name_in_room ON baby(room, name);
CREATE INDEX IF NOT EXISTS baby_by_owner ON baby(room, owner_uid);
CREATE INDEX IF NOT EXISTS baby_by_parent ON baby(room, parent_low, parent_high);

-- ---------- 竞猜对局 ----------
CREATE TABLE IF NOT EXISTS quiz_round (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  room        TEXT    NOT NULL,
  category    TEXT    NOT NULL,              -- chengyu/song/.../guoman/diff/trivia
  question_id TEXT    NOT NULL,
  answer_norm TEXT    NOT NULL,              -- 归一化答案的 sha256（不存明文，避免题库被窥屏泄露）
  hint_level  INTEGER NOT NULL DEFAULT 0,
  started_at  INTEGER NOT NULL,
  expires_at  INTEGER NOT NULL,
  closed_at   INTEGER,
  winner_uid  TEXT,
  attempts    INTEGER NOT NULL DEFAULT 0,
  msg_seq     INTEGER,                       -- 题面消息 sequence，便于关联/撤回
  by_uid      TEXT                           -- 出题人（用户出题玩法预留）
);
-- 每群同一时刻只允许一个未结束对局
CREATE UNIQUE INDEX IF NOT EXISTS quiz_one_open ON quiz_round(room) WHERE closed_at IS NULL;
CREATE INDEX IF NOT EXISTS quiz_round_recent ON quiz_round(room, category, closed_at DESC);
CREATE INDEX IF NOT EXISTS quiz_round_by_q ON quiz_round(question_id, started_at);

CREATE TABLE IF NOT EXISTS quiz_stat (
  question_id TEXT    PRIMARY KEY,
  category    TEXT    NOT NULL,
  shown       INTEGER NOT NULL DEFAULT 0,    -- 出题次数
  correct     INTEGER NOT NULL DEFAULT 0,    -- 被答对次数
  last_at     INTEGER
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS quiz_stat_by_cat ON quiz_stat(category, last_at);

-- 单个对局的作答流水（防刷屏、可回放）
CREATE TABLE IF NOT EXISTS quiz_attempt (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  round_id INTEGER NOT NULL REFERENCES quiz_round(id) ON DELETE CASCADE,
  uid      TEXT    NOT NULL,
  text     TEXT    NOT NULL,                 -- 已截断到 64 字符
  hit      INTEGER NOT NULL DEFAULT 0,
  at       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS quiz_attempt_by_round ON quiz_attempt(round_id, id);

-- ---------- 抽奖 / 开箱 ----------
CREATE TABLE IF NOT EXISTS lottery_draw (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  room    TEXT    NOT NULL,
  uid     TEXT    NOT NULL,
  source  TEXT    NOT NULL CHECK (source IN ('blind_box','chest','gift','reward','signin')),
  box_id  TEXT    NOT NULL,
  cost    INTEGER NOT NULL DEFAULT 0,
  cost_currency TEXT CHECK (cost_currency IN ('coin','diamond')),
            -- 免费抽取：cost=0 且 cost_currency IS NULL；
            -- 付费抽取：必须同时记币种与金额（只记金额无法区分金币/钻石）
  item_id TEXT    NOT NULL,
  qty     INTEGER NOT NULL DEFAULT 1,
  at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS lottery_by_user ON lottery_draw(room, uid, id DESC);
CREATE INDEX IF NOT EXISTS lottery_by_box  ON lottery_draw(box_id, at);   -- 爆率审计

-- ---------- 群活跃 / 成员快照 / 改名 / 邀请 ----------
CREATE TABLE IF NOT EXISTS activity_daily (
  room     TEXT    NOT NULL,
  uid      TEXT    NOT NULL,
  day      TEXT    NOT NULL,
  messages INTEGER NOT NULL DEFAULT 0,
  chars    INTEGER NOT NULL DEFAULT 0,
  first_at INTEGER NOT NULL,
  last_at  INTEGER NOT NULL,
  PRIMARY KEY (room, uid, day)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS activity_by_day ON activity_daily(room, day, messages DESC);
-- 活跃榜按期间聚合（日/周/月）用；主键是 (room, uid, day)，方向相反，需单建
CREATE INDEX IF NOT EXISTS activity_by_room_day ON activity_daily(room, day, uid);

CREATE TABLE IF NOT EXISTS member_seen (
  room       TEXT    NOT NULL,
  uid        TEXT    NOT NULL,
  nickname   TEXT,
  role       TEXT,
  first_seen INTEGER NOT NULL,
  last_seen  INTEGER NOT NULL,
  left_at    INTEGER,
  PRIMARY KEY (room, uid)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS nickname_log (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  room     TEXT    NOT NULL,
  uid      TEXT    NOT NULL,
  old_name TEXT,
  new_name TEXT,
  at       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS nickname_by_room ON nickname_log(room, at DESC);

CREATE TABLE IF NOT EXISTS invite_daily (
  room         TEXT    NOT NULL,
  day          TEXT    NOT NULL,
  joins        INTEGER NOT NULL DEFAULT 0,
  leaves       INTEGER NOT NULL DEFAULT 0,
  kicked       INTEGER NOT NULL DEFAULT 0,   -- 被移出；当前恒 0（group_events 把「退出」和「被移出」都归为 leave）
  invite_joins INTEGER NOT NULL DEFAULT 0,   -- 带"邀请"语义来源的入群数
  PRIMARY KEY (room, day)
) WITHOUT ROWID;

-- 按人归属的邀请记录（§8.7 C1 起有生产者：core/invite.py，由 app.py:_feed_members 调用）
CREATE TABLE IF NOT EXISTS invite_credit (
  room     TEXT    NOT NULL,
  inviter  TEXT    NOT NULL,
  invitee  TEXT    NOT NULL,
  day      TEXT    NOT NULL,
  at       INTEGER NOT NULL,
  PRIMARY KEY (room, inviter, invitee, day)
) WITHOUT ROWID;

-- ---------- 小黑屋 / 黑名单 ----------
CREATE TABLE IF NOT EXISTS jail (
  room     TEXT    NOT NULL,
  uid      TEXT    NOT NULL,
  until    INTEGER NOT NULL,
  reason   TEXT,
  by_uid   TEXT,
  at       INTEGER NOT NULL,
  PRIMARY KEY (room, uid)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS jail_by_expiry ON jail(until);   -- 到期解禁巡检

CREATE TABLE IF NOT EXISTS blocklist (
  room   TEXT NOT NULL,
  uid    TEXT NOT NULL,
  scope  TEXT NOT NULL DEFAULT 'play' CHECK (scope IN ('play','all')),
  reason TEXT,
  by_uid TEXT,
  at     INTEGER NOT NULL,
  PRIMARY KEY (room, uid, scope)
) WITHOUT ROWID;

-- ---------- 引擎临时态 / 幂等 / 审计 / 小状态 ----------
CREATE TABLE IF NOT EXISTS game_state (
  room       TEXT    NOT NULL,
  slot       TEXT    NOT NULL,               -- 'duel' / 'diff' / 'space_cache'
  state      TEXT    NOT NULL,               -- JSON
  expires_at INTEGER,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (room, slot)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS idem (
  key   TEXT PRIMARY KEY,                    -- '<session_id>:<sequence>:<cmd>' / 'evt:<room>:<kind>:<uid>:<day>'
  scope TEXT NOT NULL,
  at    INTEGER NOT NULL
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idem_by_age ON idem(at);

CREATE TABLE IF NOT EXISTS audit (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  at     INTEGER NOT NULL,
  room   TEXT,
  actor  TEXT,
  action TEXT NOT NULL,                      -- cmd:结婚 / evt:join / sched:daily_quiz / err:quiz
  target TEXT,
  result TEXT NOT NULL DEFAULT 'ok',         -- ok/denied/failed
  detail TEXT                                -- JSON，禁止写消息正文
);
CREATE INDEX IF NOT EXISTS audit_by_room ON audit(room, at DESC);
CREATE INDEX IF NOT EXISTS audit_by_action ON audit(action, at DESC);

CREATE TABLE IF NOT EXISTS kv (
  room       TEXT    NOT NULL,
  key        TEXT    NOT NULL,
  value      TEXT    NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (room, key)
) WITHOUT ROWID;

-- ---------- 榜单（引擎只读，不写）----------
-- v1 只做四榜：金币 / 活跃 / 婚姻 / 邀请。v_rank_coin 已在钱包段定义。
-- 活跃榜：日/周/月 直接查 activity_daily（按 day 范围），总榜走本视图；
--          不建 rank_period 快照表，也不做快照维护任务（见 3.2.3 决定二）。
CREATE VIEW IF NOT EXISTS v_rank_active AS
  SELECT room, uid, SUM(messages) AS score, SUM(chars) AS chars
  FROM activity_daily GROUP BY room, uid;

-- 婚姻榜 / 邀请榜由 core/rank.py 直接查询 pair / invite_credit 聚合，不建视图
--（婚姻榜要按婚龄合并夫妻成一行，邀请榜要按 inviter 去重，都不适合简单视图）
