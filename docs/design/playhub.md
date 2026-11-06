# 群聊游戏系统 playhub 设计文档

> 状态：设计草案 **v3**（评审中）。v1 的「`.` 前缀触发」已整体下线，改为**纯关键词触发**；分层、内核、DDL、分批与 v1 结论全部保留。
> 范围：47 项群聊游戏/群管功能，一次性设计、分 4 批交付
> 前置：`docs/design/douluo.md`（斗罗大陆 RPG）已封存，不再修改；本设计是**中性游戏内核 + 多引擎**，不绑定任何单一玩法世界观
> 本文所有「已验证」结论均给出 `文件:行号`；未读到的内容标注「未核实」，不做推测
> **v2 修订范围**：§0、§1.1、§1.2、§1.3、§2.3、§2.5、§2.6、§2.7、§4、§7、§8.6、§9。触发方式的**唯一事实源是 §2.6（解析规则）+ §4（触发词总表）+ §4.3（误触治理）**；正文其它位置若与这三处冲突，以这三处为准。
> **v3 修订（2026-11-05）**：`docs/design/playhub-delta.md` 的修订增量已**全部并入本文**，该文件与本文不再并行维护，本文是唯一权威方案。本次落地：货币收敛为金币 / 钻石两币（§2.4、§3.2）、一层角色门禁（§2.4、§4、§7、§8）、`#` 只在命令首部拆第一个（§2.6.2）、四榜契约与数值表（§3.4、§4.4）、玩法范围收口（§4）、邀请人修复 C1（§8.7）。**v3 修订范围**：§0、§1.1—§1.3、§2.1—§2.4、§2.6.1、§2.6.2、§3.2—§3.4、§4、§6.3、§7、§8.1、§8.2、§8.5、§8.6、§8.7、§9。

## 目录

- [0 结论摘要](#0-结论摘要)
- [1 模块与文件预算](#1-模块与文件预算)
  - [1.1 一个插件 vs 多个插件](#11-一个插件-vs-多个插件)
  - [1.2 目录骨架](#12-目录骨架)
  - [1.3 文件预算表](#13-文件预算表)
  - [1.4 为什么不是另一种切法](#14-为什么不是另一种切法)
- [2 分层与契约](#2-分层与契约)
  - [2.1 依赖方向](#21-依赖方向)
  - [2.2 三层职责与硬约束](#22-三层职责与硬约束)
  - [2.3 引擎统一协议](#23-引擎统一协议)
  - [2.4 core 对外契约（函数签名）](#24-core-对外契约函数签名)
  - [2.5 串行队列下的执行模型](#25-串行队列下的执行模型)
  - [2.6 关键词路由：整词精确匹配、别名唯一、归一化](#26-关键词路由整词精确匹配别名唯一归一化)
  - [2.7 与现有插件的冲突及解法](#27-与现有插件的冲突及解法)
- [3 数据库设计](#3-数据库设计)
  - [3.1 作用域与主键约定](#31-作用域与主键约定)
  - [3.2 DDL 草案](#32-ddl-草案)
  - [3.3 SQLite 限制与规避](#33-sqlite-限制与规避)
- [4 玩法与触发词总表（47 项）](#4-玩法与触发词总表47-项)
  - [4.1 复用 / 重写判定](#41-复用--重写判定)
  - [4.2 定时动作清单](#42-定时动作清单)
  - [4.3 高误触词治理](#43-高误触词治理)
- [5 题库与文案数据](#5-题库与文案数据)
- [6 多账号 / 并发 / 幂等](#6-多账号--并发--幂等)
- [7 分批交付](#7-分批交付)
- [8 风险清单](#8-风险清单)
- [9 待用户拍板的问题](#9-待用户拍板的问题)

---

## 0 结论摘要

1. **触发方式：纯关键词，无前缀。** 整条消息归一化后**整词精确等于**某触发词才触发；允许「关键词 + 分隔符（空白或 `@`）+ 参数」。**不做包含匹配**（62 个词里含「天气」「礼物」「宝宝」这类常用词，包含匹配会让正常聊天频繁误触），**也不做最长匹配**（精确匹配下不存在前缀歧义，理由见 §2.6）。
2. **触发词账**：playhub **自有 62 个词**（承载 44 个动作，含别名），**复用框架既有 4 个词**（`提示`/`换一题`/`公布答案`/`结束`，来自 `wxbot_framework/text.py:8-9`；`退出猜成语` 为兼容别名）。启动时做全局唯一性校验，校验域 = 62 + 框架 8 个中文控制词 = **70 个词**；冲突则记 `logger.error` 并**禁用冲突的两侧引擎之一**（不整插件下线，见 §2.6 第 4 条）。
   **第 4 批增补**：新增第 62 个触发词 `解签`（动作 `daily.fortune_explain`，`risk=low`），归入第 3 项抽签（小V 抽签卡本身带「欲知详情可发送【解签】」），不另占 47 项编号。
   **本批增补**：新增第 61 个触发词 `帮助`（动作 `business.help`，`risk=low`）；`帮助` 是本批唯一新增词，其余 60 词口径不变。
3. **一个插件**：`plugins/playhub/`，内部用子包分层；**42 个文件**（上限 64），约 6.2k 行、~323 KiB（上限 1 MiB）。新增 `keywords.py` 与 `core/permissions.py`，`router.py` 因删掉 trie 减 30 行、因 `#`/艾特范围解析增 2 KiB。
4. 不拆成多个插件：跨插件共享 core 只能靠 `import _wxbot_plugins.<共享目录>`（`wxbot_framework/plugins.py:205-208`），共享目录不进任何插件的源码快照（`wxbot_framework/plugin_sources.py:82-97`），回滚不可复现且热更新失效——脆弱，不采用。
5. 分层：`入口 → 关键词路由 → 引擎(engines) → 内核(core) → 唯一 SQL 出口(core/store.py)`；**只有 core 能出现 SQL**，引擎只调用函数契约，返回 `Reply` 对象，由入口在事务外统一发送。
6. 数据落盘按账号隔离：`<每账号工作区>/.runtime/playhub/playhub.sqlite3`，不再沿用 douluo 的仓库根 `.runtime` 写法（`docs/design/douluo.md:44`）。
7. 唯一 SQLite 库，`WAL + busy_timeout + BEGIN IMMEDIATE`；所有写入按 `(room, uid)` 作用域，`room` = 群 `chat_id`，`uid` = 群成员 wxid。
8. 47 项中 **43 项新写**、**4 项复用**（第 37/43/44/47 项）；其中 **5 项不占 playhub 触发词**（第 37/40/43/44/46 项）：成语接龙（`idiom_chain` 自带框架词）、进群欢迎 / 退群监控（`group_notice`，事件无命令）、智能聊天（`mention_chat` + `personality` + `chat_summary`，对话无命令）、面板定时任务（控制台，无群内命令）。其中「超极宝宝」并入 `宝宝 超极` 子命令，不单独占词。逐行口径见 §4 总表。
9. 题库/文案**不放插件目录**（会吃掉 1 MiB 与 64 文件预算，且网页保存会做快照校验）：放仓库 `content/playhub/*.jsonl`，运行时可用 `<工作区>/.runtime/playhub/content/` 覆盖。
10. 竞猜（11 类目）+ 文字找茬 + 趣味问答共用一个 `quiz` 引擎、一张 `quiz_round` 表，类目只是题库字段。竞猜的子命令**复用框架已有的四个词**（`提示`/`换一题`/`公布答案`/`结束`；`退出猜成语` 自 2026-11-05 起只作兼容别名，见 §2.7 冲突 3），不另造 `收题` 之类的同义词。
11. **必须改一处现有代码才能完整可用**：`plugins/idiom_chain/__init__.py:110` 在接龙开局后把**任何非 `/` 开头文本**送判定，而判定函数对非四字输入本地短路后仍会**回一句「请输入一个完整的四字成语」**（`plugins/idiom_chain/judge.py:39-40`）。前缀方案下线后，v1 的"放行 `.` 前缀"解法彻底失效。推荐把判据改为「`FOUR_CHINESE` 四字中文 **且** 不在 `GROUP_CONTROL_COMMANDS` 内才送判定」（§2.7 方案 A）。**附带收益是消除接龙期间的刷屏误回复；不省 LLM 调用——`judge.py:39-40` 本来就已短路，v1 的这个理由不成立，此处更正。需改现有插件，用户未批准。**
12. 工作量：**49 人日**（含每批测试；比 v2 基线多 4 人日净增：两币钱包与迁移 +1.25、一层权限基础 +0.5、`#`/艾特解析 +0.25、婚姻四子命令 +1.25、钻石买盲盒次数与清求婚次数 +0.5、四榜及活跃月/总 +0.5、C1 与群通知回归 +0.75、后台定时线程（§4.2，原「定时配置批准契约」作废）+0.5、跨模块一致性验收 +0.25，裁掉超级宝宝扩展 −0.75、裁掉切磋监狱/医院联动 −1.0）。加 15% 风险缓冲 7.35 人日 = **56.35 人日，排期取 57 人日**；4 个批次（11 / 12 / 14 / 12），第 1 批 11 人日可上线。
13. **货币只做两种：金币 + 钻石。** 银币 / 魅力 / 身价 / 积分全部 v2。钻石是稀缺货币，**正向产出只有三处**：`签到（连签第 7 天）`、`开宝箱（金箱）`、`抽签（上上签）`，均为小概率产出；其他玩法（含群内发言、礼物、切磋、排行榜）不产钻石。数值锚见 §4.4。
14. **权限只做一层门禁**：统一用 `ctx.group_member_role()`，只放行 `owner` / `admin`；`unknown` **失败关闭**，不回退到自建 owner 白名单。普通成员的个人玩法（`宝宝 看`、`宝宝 改名 X`）不套用该门禁（§2.4、§7）。

---

## 1 模块与文件预算

### 1.1 一个插件 vs 多个插件

**结论：一个插件。**

硬约束来源：

| 约束 | 证据 | 影响 |
|---|---|---|
| 插件发现只看 `plugins/` 下一层目录，且要求该层有 `__init__.py`，`_` 开头跳过 | `wxbot_framework/plugins.py:163-193` | 插件内部可以有任意子包，子包不会被当成插件 |
| 加载用 `spec_from_file_location(..., submodule_search_locations=[插件目录])`，模块名 `_wxbot_plugins.<name>_<uuid>`，且 `_wxbot_plugins.__path__` = plugins 目录 | `plugins.py:205-211` | 插件内相对导入可用；理论上也能 `import _wxbot_plugins.core` |
| 源码收集递归插件目录，单文件 ≤256 KiB、合计 ≤1 MiB、≤64 文件 | `plugin_sources.py:82-97` | 纯代码很宽松；**题库一定超标** |
| 回调单线程串行 | `plugins.py:86-111` | 一个插件内所有玩法共用一条队列 |

拆分方案的代价（为什么否决）：

1. **共享内核的两种做法都不可接受**
   - 做法 A：共享目录 `plugins/_playhub_core/`，各插件 `import _wxbot_plugins._playhub_core`。技术上可行（`_wxbot_plugins.__path__` 指向 plugins 目录，`plugins.py:205-208`），但 `plugin_sources.files()` 只递归**该插件自己的目录**（`plugin_sources.py:82-97`），共享目录永远不进版本快照 → 网页上"保存/回滚某个插件"后，磁盘上是「新插件代码 + 未版本化的共享内核」，`snapshot` 的 sha256（`plugin_sources.py:99-113`）无法覆盖内核，出问题查不回去。且重新加载只清理该插件模块名前缀，共享模块留在内存里 → 改内核必须重启进程。
   - 做法 B：每个插件复制一份 core。47 项 × 多份 = 一致性灾难（改一次发币规则要改 N 处）。
2. **跨玩法写入会变成跨插件写入**：签到写货币+次数+排行；结婚写关系+货币+审计+排行。拆开后多个插件线程/连接同写一批表，SQLite 单写者，`busy` 与死锁概率随插件数上升。单插件单线程（`plugins.py:86-111`）天然串行，一条连接足够。
3. **预算够用**：单插件 42 文件（含子包），余 22 个文件余量；1 MiB 余 ~701 KiB（约 31% 已用）。拆分的收益（多一条队列）换不来一致性损失。

拆分的唯一收益是**故障与延迟隔离**，用插件内部手段补偿（§2.5）：每命令 `try/except` + 命令级软超时 + 慢活走有界后台执行器 + 每个引擎有独立功能开关（`kv` 表）。文档同时给出**触发拆分的量化条件**（不属于本设计实施范围，只做预警）：

- 单条命令 p95 > 800 ms，或
- 需要网络/LLM 的命令 > 5 次/分钟/群，或
- 单群成员 > 5000 导致排行/聚合查询 > 200 ms

满足任一条件时，把 `music` + `outbound`、`quiz`、`groupstat` 三个模块分别升格为独立插件，core 改为「代码复制 + 版本号对齐」，而不是共享 import。

### 1.2 目录骨架

```
plugins/playhub/                 ← 唯一插件目录（发现层，plugins.py:163-193）
├── plugin.json                  ← 只允许 api/description/requires/message_types/default_enabled/business/scheduled_actions
├── README.md
├── __init__.py                  ← Plugin 类：on_load / on_message / on_unload，事件与命令总入口
├── app.py                       ← 装配：路径解析、Store 打开、引擎注册、定时动作表
├── router.py                    ← 关键词路由：归一化 → 整词精确匹配 → 裸触发策略 → 静默忽略 → 限流
├── keywords.py                  ← 关键词表（单一事实源）：62 个词 + 别名 + needs_arg/bare_behavior/risk/owner
├── registry.py                  ← 动作 id → 引擎的注册表（帮助文案由 keywords.py + registry.py 共同生成）
├── business.py                  ← business 函数（网页可编辑：帮助文案、签到文案、公告模板）
├── core/                        ← 内核：唯一 SQL 层
│   ├── __init__.py
│   ├── clock.py
│   ├── store.py                 ← 唯一持有 sqlite3 连接与 SQL 的入口
│   ├── schema.sql               ← DDL（注意：.sql 不在网页可编辑集合内，plugin_sources.py:14）
│   ├── wallet.py
│   ├── permissions.py           ← 一层角色门禁：GroupRole / GateDecision 类型与纯判定函数（不读 ctx、DB、网络）
│   ├── quota.py
│   ├── items.py
│   ├── idem.py
│   ├── audit.py
│   ├── rank.py
│   ├── kv.py
│   ├── errors.py
│   └── textkit.py               ← 文案渲染、截断、占位符、关键词同形检查（供自触发闸调用）
├── content/                     ← 题库加载（数据本身不在这里）
│   ├── __init__.py
│   ├── loader.py
│   ├── banks.py
│   └── validate.py
├── engines/                     ← 玩法族
│   ├── __init__.py
│   ├── base.py
│   ├── daily.py
│   ├── quiz.py
│   ├── social.py
│   ├── lottery.py
│   ├── duel.py
│   ├── music.py
│   ├── ranking.py
│   ├── groupstat.py
│   ├── moderation.py
│   └── lounge.py
└── adapters/                    ← 对外部世界的唯一出口
    ├── __init__.py
    ├── memberfeed.py            ← 只读成员库 + 群角色读取（昵称/绑定；封装 ctx.group_member_role()）
    ├── outbound.py              ← HTTP：天气/点歌源/内容源，带超时与熔断
    └── executor.py              ← 有界后台执行器（慢活离开插件队列）

content/playhub/                 ← 仓库级题库/文案（不在 plugins/ 内，不受 1 MiB / 64 文件限制）
├── chengyu.jsonl   song.jsonl   riddle.jsonl   movie.jsonl   star.jsonl
├── animal.jsonl    plant.jsonl  food.jsonl     honor.jsonl   anime.jsonl
├── guoman.jsonl    diff.jsonl   trivia.jsonl   copy.jsonl
├── fortune.jsonl（每行 id/no/tier/text/jie）   wealth.jsonl   almanac.jsonl horoscope.jsonl sign.jsonl
└── items.json
```

### 1.3 文件预算表

「行数」为该文件预估规模（含注释），「KiB」为保守估算。合计 **42 文件 / ≈6240 行 / ≈323 KiB**（v3 相对 v2 基线：+ `core/permissions.py` 1 文件 / 60 行 / 2 KiB；`wallet.py` +3、`schema.sql` +3、`router.py` +2、`rank.py` +2、`social.py` +2、`memberfeed.py` +1、`app.py` +1、`groupstat.py` +1、`README.md` +1 KiB，增量合计 +18 KiB → 305 + 18 = 323）。逐行 KiB 是单文件保守估算，与合计 323 KiB 的差额属设计余量，**不逐项加总对齐**；每批交付验收重新计量实测值。

| # | 文件 | 行数 | KiB | 职责 |
|---|---|---|---|---|
| 1 | `plugin.json` | 40 | 1.5 | manifest：`requires`、`message_types`、`business`、`scheduled_actions` |
| 2 | `__init__.py` | 190 | 8.5 | `Plugin`：路由与事件分发、Reply 发送（**含自触发闸**）、`is_self` 免疫、异常兜底 |
| 3 | `app.py` | 220 | 11 | 路径解析、Store 生命周期、引擎注册、动作表、**关键词表构建与冲突处置**、**权限审计与执行编排** |
| 4 | `router.py` | 110 | 7 | 归一化（含艾特范围重映射）、整词精确匹配、**只拆首部第一个 `#`**、裸触发策略、限流、静默忽略 |
| 5 | `keywords.py` | 110 | 6 | **62 个触发词 + 别名 + 元数据（needs_arg/bare_behavior/risk/owner）+ 启动期冲突检测** |
| 6 | `registry.py` | 100 | 5 | 动作 id → 引擎绑定、帮助文案生成（词条来自 `keywords.py`） |
| 7 | `business.py` | 120 | 6 | 网页可编辑函数（帮助、签到、公告 3–6 个，≤8 上限） |
| 8 | `README.md` | 60 | 4 | 使用与运维说明（含触发词速查表、v1 范围与「只统计不清人」等口径） |
| 9 | `core/__init__.py` | 20 | 1 | 导出 `Game` 门面 |
| 10 | `core/clock.py` | 60 | 2 | 北京时区、日切、冷却换算 |
| 11 | `core/store.py` | 260 | 11 | 连接、PRAGMA、迁移、`tx()` |
| 12 | `core/schema.sql` | 320 | 16 | DDL（§3.2：`player` / `wallet` / `wallet_ledger` / 只读兼容视图） |
| 13 | `core/wallet.py` | 130 | 9 | 玩家档案、**两币钱包（balance/credit/debit）**、分录、幂等 |
| 14 | `core/permissions.py` | 60 | 2 | 一层角色门禁：`GroupRole` / `GateDecision` 类型与纯判定函数（不读 `ctx`、DB、网络） |
| 15 | `core/quota.py` | 110 | 5 | 每日次数、冷却 |
| 16 | `core/items.py` | 140 | 6 | 物品字典、背包、开箱 |
| 17 | `core/idem.py` | 70 | 3 | 幂等键 |
| 18 | `core/audit.py` | 80 | 3 | 审计日志 |
| 19 | `core/rank.py` | 120 | 7 | **四榜**（金币/活跃/婚姻/邀请）及活跃期间查询 |
| 20 | `core/kv.py` | 60 | 2 | 房间级小状态（含后台定时动作的 `sched.last.*` 游标与改名快照，§4.2） |
| 21 | `core/errors.py` | 40 | 1 | 异常类型（含 `TriggerConflict`） |
| 22 | `core/textkit.py` | 120 | 5.5 | 文案渲染、截断、占位符、**关键词同形检查**（自触发闸调用） |
| 23 | `content/__init__.py` | 10 | 0.5 | 导出加载器 |
| 24 | `content/loader.py` | 200 | 9 | JSONL 读取、校验、去重、坏行容错、缓存 |
| 25 | `content/banks.py` | 120 | 5 | 类目索引、轮转、配额检查 |
| 26 | `content/validate.py` | 110 | 5 | 发布前校验（也可被 `tools/` 调用） |
| 27 | `engines/__init__.py` | 30 | 1 | 引擎注册表 |
| 28 | `engines/base.py` | 110 | 5 | `Engine` 协议、`Command`、`Reply` |
| 29 | `engines/daily.py` | 260 | 12 | 签到、抽签、解签、黄历、星座、求财神 |
| 30 | `engines/quiz.py` | 420 | 20 | 11 类竞猜、找茬、趣味问答、收题；**复用框架四个子命令词** |
| 31 | `engines/social.py` | 520 | 26 | 结婚（含新增四子命令与清求婚次数）、宝宝（**v1 基础版**）、闺蜜、纳妾、结拜、雇佣、配对、关系卡、礼物 |
| 32 | `engines/lottery.py` | 220 | 10 | 盲盒、开宝箱 |
| 33 | `engines/duel.py` | 160 | 7 | 切磋（**不联动监狱/医院**） |
| 34 | `engines/music.py` | 200 | 9 | 点歌 |
| 35 | `engines/ranking.py` | 180 | 8 | 四榜、活跃榜（日/周/月/总） |
| 36 | `engines/groupstat.py` | 300 | 15 | 潜水统计（只统计不清人）、邀请统计（按 `actor_id`，缺失降级）、改名提醒、进/退群计数 |
| 37 | `engines/moderation.py` | 200 | 9 | 小黑屋（软禁言）、黑名单 |
| 38 | `engines/lounge.py` | 240 | 11 | 文案馆、群空间卡片、聊天/接龙转发 |
| 39 | `adapters/__init__.py` | 10 | 0.5 | — |
| 40 | `adapters/memberfeed.py` | 110 | 6 | 只读 `ctx.members` + `ctx.group_member_role()` 角色读取（`sdk.py:474-641` 暴露的方法） |
| 41 | `adapters/outbound.py` | 180 | 8 | HTTP + 超时 + 熔断 + 降级 |
| 42 | `adapters/executor.py` | 120 | 5 | 有界线程池、结果待发包 |
| — | **合计** | **≈6240** | **≈323** | 文件 42 / 64，体积 ≈31% / 1 MiB |

余量：22 个文件、~701 KiB。**不顶上限**。可用于后续加 `engines/shop.py`、`engines/achievement.py`、DDL 增量迁移文件。单文件仍须低于 256 KiB，本轮没有接近上限的文件。测试文件与题库都不在插件目录内，不计入本表（§5.2）。

**执行编排分批落地**：第 1 批由 `app.py` **直连发送**，不引入 `adapters/executor.py`（表中 42 号的预算留到第 3 批接入）；本批**禁止 HTTP / LLM / 图片任务进入回调**，进度边界进 §7 第 1 批验收第 6 条。

**2026-11-06：`adapters/executor.py` 已落地**（221 行，`DEFAULT_WORKERS=4` / `DEFAULT_QUEUE_SIZE=32` / `DEFAULT_TIMEOUT=3 s` / 延迟队列 256 条）。分工按 §2.5 第 3、4 条固定下来：
- **worker 只算不写**：天气的两跳 HTTP 与渲染跑在 worker，发消息直接调 `ctx.reply`（发送路径本就有 `sdk._send_lock`，`scheduler.run_rename` 也早已在 daemon 线程发群消息），**不碰 `Store`**。
- **落库回到插件线程**：worker 需要写缓存时 `executor.defer(...)` 入队，由 `App._flush()` 在**下一条消息开头**执行。代价是「刚查完立刻再问一次」可能还没写进缓存（多花一次额度），换来的是不再有跨线程事务。
- **队列满 → 退化成同步**：`submit()` 返回 `False` 时 `OutboundEngine` 当场同步查，宁可慢几秒也不丢查询。
- **超时只记账**：线程无法中断，`timeout` 只在任务结束后量一下、超了记警告；真正的超时保证来自 transport（天气是 `weather_timeout_seconds`）。

关键词表放 `keywords.py` 的代码常量里，**不放 `content/`**：它是路由契约（改一个词会改变谁响应什么），必须与引擎代码同版本、同快照；题库/文案才是可以在运行期热改的内容。§4 的表格由 `keywords.py` 直出（有一个测试断言"文档表格里的词数 == 代码里的词数"，允许文档手写但跑测试校验）。

### 1.4 为什么不是另一种切法

| 备选切法 | 否决理由 |
|---|---|
| **按功能拆 47 个插件** | 每个插件都要重复 core；`members`/`db` 多连接；47 条串行队列反而更难诊断；文件数 47×3 ≈ 140 > 但每个插件独立限额所以能过——代价全在一致性上 |
| **按玩法族拆 8 个插件** | 关系与货币、排行与活跃度天然跨族写；共享 core 走 `_wxbot_plugins` import 的脆弱性见 §1.1 |
| **单文件 3000 行** | 网页编辑器是纯文本框（`webui/app.js:393-501`），单文件过大不可维护；且单文件 256 KiB 上限（`plugin_sources.py:82-97`）虽够，但审阅/回滚粒度太粗 |
| **把数据塞进插件目录** | 11 类目 × 300 题 ≈ 400 KB，直接吃掉 40% 体积预算；且题库变动会污染插件源码版本与 sha256 快照（`plugin_sources.py:99-113`） |
| **另起一个 Web 服务做后台** | 控制台路由是硬编码表（`panel_http.py:87-234`），没有插件注册页面的机制；另起服务等于第二套鉴权/端口/部署 |

---

## 2 分层与契约

### 2.1 依赖方向

```
__init__.py (Plugin)
   │  只做四件事：拿 Message → is_self 免疫 → router.parse → 调引擎 → 事务外发 Reply（发前过自触发闸）
   ├──> router.py ──> keywords.py（关键词表，无依赖，纯数据+校验）
   │        └──> registry.py ──> engines/*  ──> core/*  ──> core/store.py ──> sqlite
   │                                     │
   │                                     ├──> adapters/*（HTTP / 成员库与群角色 / 执行器）
   │                                     └──> core/permissions.py（纯判定，无副作用）
   └──> app.py（装配，不参与业务）──> core/permissions.py + adapters/memberfeed.py（权限审计与执行编排）
content/* 只被 engines/quiz、engines/daily、engines/lounge 使用；core 不依赖 content
```

**单向**：`engines` 不 import 其它 `engines`；`core` 不 import `engines`/`adapters`；`adapters` 不 import `core`；`keywords.py` 不 import 任何 playhub 模块（可被测试独立导入）。

**权限链路的三个落点**（v3 新增，职责严格分离）：

1. `adapters/memberfeed.py` 只负责**读取**微信群角色：调用 `ctx.group_member_role()`，把查询异常、异常返回值、缺失会话统一映射为 `unknown`，不写库、不发消息。
2. `core/permissions.py` 只负责**判断**：接收 `GroupRole`，返回 `GateDecision`，不访问 `ctx`、数据库或网络（纯函数，可单测）。
3. `app.py` 负责**编排**：按动作元数据决定是否需要门禁 → 事务外查角色 → 判定 → 需要时提交 `audit.result='denied'` → 放行后进业务事务。

引擎与适配器都不直接写 SQL；权限判定同样不写 SQL，审计写入仍只经 `core/audit.py`。

### 2.2 三层职责与硬约束

| 层 | 职责 | 禁止 | 强制手段 |
|---|---|---|---|
| `core` | 事务边界、SQL、不变量（余额不能为负、一人一婚、次数上限） | 禁止发消息、禁止 HTTP/LLM/文件 IO、禁止读 `ctx` | 所有函数首参是 `conn`（除了 `Store`），不接收 `ctx` |
| `engines` | 玩法规则、文案选择、状态机、参数解析 | 禁止出现 SQL 字符串、禁止 `import sqlite3`、禁止直接 `ctx.reply` | 单元测试用 AST 扫描 `engines/` 下源码里是否出现 `sqlite3`、`SELECT`、`INSERT`、`UPDATE`、`DELETE` 字面量（借鉴 douluo 的"store 是唯一 SQL 出口"约定，`docs/design/douluo.md:20-40`） |
| `adapters` | 外部世界：HTTP、成员库与群角色、执行器 | 禁止写 SQL、禁止持有事务 | 只被 engines 调用，一律带超时参数 |

`core/permissions.py` 是这一层的**唯一例外**：它不碰 `conn`，也不碰 `ctx`，只做纯函数判定（`GroupRole` → `GateDecision`），因此可以被单测直接导入，不受「首参是 conn」约束；角色读取由 `adapters/memberfeed.py` 完成，编排与审计由 `app.py` 完成（§2.4）。

「只有一层能写 SQL」的落地判定：`grep -n "SELECT\|INSERT\|UPDATE\|DELETE" plugins/playhub/` 只允许命中 `core/*.py` 与 `core/schema.sql`。这条写进 `tests/test_playhub_layers.py`。

### 2.3 引擎统一协议

```python
# engines/base.py
@dataclass(frozen=True)
class MentionSpan:
    start: int        # 原始 text 中的起始下标
    end: int          # 结束下标（左闭右开）
    uid: str | None   # 已确认的 wxid；结构不完整时为 None

@dataclass(frozen=True)
class Command:
    name: str        # 命中的触发词（归一化后原词，如 "签到" / "打卡" / "宝宝"）
    action: str      # 引擎内动作 id，如 "signin" / "baby.view"，由 keywords.py 绑定
    raw: str         # 原始整行（发送前的原文，仅用于日志/审计）
    args: str        # 命令首部之后的剩余文本（已 strip），**只消耗首部第一个分隔符**，参数内的 `#` 原样保留
    tokens: tuple[str, ...]   # args 的空白切分，**不再去掉 @ 前缀**
    had_arg: bool    # 是否带参数；用于区分「宝宝」（裸发）与「宝宝 看」
    separator: str | None     # 首部分隔符："space" / "#" / "@"；裸命令为 None
    mentions: tuple[MentionSpan, ...]   # 本条消息已识别的艾特范围（升序、不重叠）

@dataclass(frozen=True)
class Reply:
    text: str | None = None
    image: Path | None = None
    at: str | None = None      # 需要 @ 的 wxid
    kind: str = "text"         # text / at_text

class Engine:
    name: str = ""
    actions: tuple[str, ...] = ()        # 动作 id 清单；**触发词不在这里声明**
    consumes_plain_text: bool = False    # 是否消费非触发词文本（竞猜抢答/找茬作答）

    def on_command(self, cmd: Command, msg: Message, ctx: PluginContext, g: Game) -> Reply | None:
        raise NotImplementedError

    def on_text(self, msg: Message, ctx: PluginContext, g: Game) -> Reply | None:
        return None

    def on_event(self, event: dict, msg: Message, ctx: PluginContext, g: Game) -> None:
        return None

    def on_schedule(self, action: str, params: dict, ctx: PluginContext) -> None:
        return None
```

**`Command` 不带 `room` / `uid`，取值来源固定如下**（引擎不得自行从 `tokens` 再解析一遍）：`room` 一律取 `msg.chat_id`；操作者 `uid` 取 `ctx.speaker(msg)`；目标用户取 `cmd.mentions[0].uid`，`mentions` 为空或 `uid is None` 时按「无有效艾特」处理，由各引擎在自己的 `bare_behavior` 里决定拒绝还是降级。

**触发词与引擎解耦**：v1 把 `commands`/`aliases` 放在 `Engine` 上，等于 47 个功能的词表分散在 10 个引擎文件里，无法做全局唯一性校验。v2 把**全部**触发词收进 `keywords.py` 一张表，`Engine` 只声明 `actions`；`registry.py` 负责把 `action` 路由到 `Engine.on_command`。这样"别名全局唯一"才可校验（§2.6 第 4 条）。

**关键契约：引擎永远返回 `Reply`，绝不自己发消息。** 由 `Plugin.on_message` 在**事务提交之后**统一调用 `ctx.reply` / `ctx.reply_at`。发送映射固定：`at is None` → `ctx.reply(text)`；`at` 非 None → `ctx.reply_at(at, text)`；`image` 非 None 时先发图再发文字。理由：`sdk.py:111-156` 的 `send_text` 会按 1023 字节分片并逐片等待（`pacing.py:9-42`，每 10 字 0.1–0.3 s），一次回复可能耗时数秒——若在事务里，等于持写锁数秒并阻塞整个插件队列（`plugins.py:86-111`）。

### 2.4 core 对外契约（函数签名）

```python
# core/store.py —— 唯一持有连接与 DDL 的地方
class Store:
    def __init__(self, db_path: Path, *, account: str, timeout: float = 3.0) -> None
    def migrate(self) -> None                     # 读 schema.sql，幂等建表，写 meta.schema_version
    @contextmanager
    def tx(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]   # 提交/回滚/关连接异常兜底；immediate=True 即 BEGIN IMMEDIATE
    @property
    def schema_version(self) -> int
    def close(self) -> None

# core/wallet.py —— 唯一的金融写路径（两币余额 + 单事务分录 + 幂等）
Currency = Literal["coin", "diamond"]

@dataclass(frozen=True)
class WalletEntry:
    currency: Currency; delta: int; balance: int
    reason: str; ref: str | None
    operation_key: str; posting_no: int

def ensure_player(conn, *, room: str, uid: str, nickname: str | None, now: int) -> None
    # 建档案，并**同时初始化金币与钻石两个余额行（均为 0）**
def profile(conn, *, room: str, uid: str) -> Player | None
def balance(conn, *, room: str, uid: str, currency: Currency) -> int
def credit(conn, *, room: str, uid: str, currency: Currency, amount: int, reason: str,
           operation_key: str, posting_no: int, now: int,
           ref: str | None = None) -> WalletEntry
def debit(conn, *, room: str, uid: str, currency: Currency, amount: int, reason: str,
          operation_key: str, posting_no: int, now: int,
          ref: str | None = None) -> WalletEntry        # 余额不足抛 Insufficient（领域错误）
def add_exp(conn, *, room, uid, exp: int, reason: str) -> tuple[int, int]  # -> (level, exp)
def touch(conn, *, room, uid, nickname, now) -> None

# core/quota.py
@dataclass(frozen=True)
class Take: ok: bool; used: int; cap: int; retry_at: float | None

def take(conn, *, room, uid, key: str, day: str, cap: int, cost: int = 1) -> Take
def peek(conn, *, room, uid, key: str, day: str, cap: int) -> Take
def cooldown_left(conn, *, room, uid, key: str, now: float) -> float | None
def set_cooldown(conn, *, room, uid, key: str, ready_at: float) -> None

# core/items.py
def defs(conn, *, room: str) -> Mapping[str, ItemDef]           # room 覆盖全局默认（world='*'）
def add(conn, *, room, uid, item_id: str, qty: int = 1) -> int
def remove(conn, *, room, uid, item_id: str, qty: int = 1) -> bool
def bag(conn, *, room, uid, limit: int = 40) -> list[ItemRow]
def roll_box(conn, *, room, uid, box_id: str, rng: random.Random, now: int) -> list[ItemRow]

# core/idem.py
def claim(conn, *, key: str, scope: str, now: int) -> bool      # INSERT OR IGNORE，成功=首次
def prune(conn, *, keep_days: int, now: int) -> int

# core/audit.py
def log(conn, *, room, actor, action, target=None, detail: dict | None = None,
        result: str = "ok", now: int) -> None

# core/rank.py —— v1 只有四榜
Board = Literal["coin", "activity", "marriage", "invite"]
Period = Literal["day", "week", "month", "all"]

def top(conn, *, board: Board, room: str, period: Period | None = None,
        now: int, limit: int = 10) -> tuple[RankRow, ...]
def position(conn, *, board: Board, room: str, uid: str,
             period: Period | None = None, now: int) -> int | None

# core/kv.py
def get(conn, *, room: str, key: str, default: str | None = None) -> str | None
def set(conn, *, room: str, key: str, value: str, now: int) -> None
def bump(conn, *, room: str, key: str, delta: int, now: int) -> int

# core/clock.py
CN: ZoneInfo                     # Asia/Shanghai
def now_ts() -> int
def day_of(ts: float | None = None) -> str      # 'YYYY-MM-DD'（北京时间）
def week_of(ts: float | None = None) -> str     # 'YYYY-Www'
def fmt_left(seconds: float) -> str             # "3小时12分"

# core/permissions.py —— 一层门禁，纯函数，不读 ctx / DB / 网络
GroupRole = Literal["owner", "admin", "member", "unknown"]

@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    role: GroupRole
    reason: Literal["allowed", "member_denied", "role_unknown"]

def management_gate(role: GroupRole) -> GateDecision
    # owner / admin → allowed；member → member_denied；unknown → role_unknown（失败关闭）

# adapters/memberfeed.py —— 只读成员库与群角色
def read_group_role(ctx, *, room: str, actor: str,
                    expected_session: str | None = None) -> GroupRole
    # 调 ctx.group_member_role()；查询异常 / 异常值 / 会话缺失 一律映射 "unknown"；不访问 DB
```

**钱包契约（v3 显式两币）**：

- `amount` **必须是正整数**（单位＝1 个货币单位，取值 `>= 1`）。扣款只能走 `debit(...)`，**不存在 `credit(delta=-N)` 的写法**。
- `WalletEntry.delta`：贷方（`credit`）为正、借方（`debit`）为负（`-amount`）；`WalletEntry.balance` 是本次事务提交后的余额，不是变动前余额。
- `currency` **必填、无默认值**，只接受 `"coin"` / `"diamond"`；非法币种直接拒绝。
- 钱包函数**自己不提交事务、不发消息、不调外部服务**。它们是同一事务内的写操作，事务边界由调用方的 `Store.tx()` 持有。
- 外层事务必须是 `BEGIN IMMEDIATE`，并覆盖**全流程**：检查业务幂等键 → 校验业务状态/次数 → 扣款或发奖 → 更新业务状态 → 写流水、审计、幂等结果 → `COMMIT` → 事务外发送 `Reply`。
- `debit` 用条件更新保证不出现负余额：

  ```sql
  UPDATE wallet SET balance = balance - :amount, updated_at = :now
   WHERE room = :room AND uid = :uid AND currency = :currency AND balance >= :amount
  ```

  影响 0 行时，按「余额不足」与「钱包行未初始化」区分返回领域错误，**绝不写成负余额**。更新成功后在同一事务内读余额并插入流水；流水插入失败则整体回滚。
- `credit` 做整数上限检查，防止 SQLite 整数溢出后退化为浮点存储。
- 余额不足属于**业务结果**而非异常：需要留失败审计时，先回滚业务事务，再独立提交审计，不能让审计被业务回滚吞掉。
- 重复消息命中已完成操作时直接返回已存结果，不再改余额。
- `wallet_ledger` 的 `UNIQUE(room, operation_key, posting_no)` 是**第二道保护**，不替代外层业务幂等键。`operation_key` 标识一次业务操作，`posting_no` 是该操作内的确定性分录序号；一笔业务同时奖励两种币时用两个不同序号。
- 多进程并发仍靠事务 + 条件更新，不靠进程内锁。

**权限契约（v3 一层门禁）**：

- 判断与执行分离：`core/permissions.py` 只做 `GroupRole → GateDecision` 的纯判定；`adapters/memberfeed.py` 只做角色读取；`app.py` 做编排。
- `app.py` 的执行顺序：按动作元数据判断该动作是否需要门禁 → **事务外**查 `ctx.group_member_role()`（经 `read_group_role`，可带 `expected_session`）→ 异常、异常返回值、缺失会话统一映射 `unknown` → `owner`/`admin` 放行，`member`/`unknown` 拒绝 → 拒绝时提交 `audit.result='denied'`（含动作、调用者、目标、角色、原因，**不记消息正文**）→ 拒绝路径不改业务状态、不扣款、不建定时配置。
- 适用动作：小黑屋设置/解除、黑名单增加/移除、潜水统计、群成员改名监测开关，以及将来新增的清理类管理动作。（第 46 项定时推送由用户在框架控制台自己配置，不经 playhub，不在本门禁范围内。）
- 普通成员的个人玩法（签到、抽奖、`宝宝 看`、`宝宝 改名 X`）**不调用门禁**。这里的「改名」专指群管理侧的改名监测开关，与 `宝宝 改名 X` 无关。
- 角色来自可能滞后的本地成员缓存；v1 **不为门禁主动同步群成员**，`unknown` 固定失败关闭，不回退到自建 owner 白名单。

```python
# engines/quiz.py
class QuizEngine(Engine):
    CATEGORIES = ("chengyu","song","riddle","movie","star","animal","plant",
                  "food","honor","anime","guoman","diff","trivia")
    def start(self, g: Game, room: str, category: str, *, uid: str, now: int) -> Reply
    def answer(self, g: Game, room: str, uid: str, text: str, now: int) -> Reply | None
    def hint(self, g: Game, room: str) -> Reply
    def reveal(self, g: Game, room: str) -> Reply
    def sweep(self, conn) -> int            # 收超时题，定时调用

# engines/social.py
def propose(g, room, kind: str, a_uid: str, b_uid: str, now: int) -> Reply   # 握手型请求
def accept(g, room, kind: str, a_uid: str, b_uid: str, now: int) -> Reply
def divorce(g, room, a_uid: str, now: int) -> Reply
def bear(g, room, a_uid: str, now: int) -> Reply
def care(g, room, a_uid: str, action: str, now: int) -> Reply
def super_baby(g, room, a_uid: str, now: int) -> Reply
def card(g, room, uid: str) -> Reply
def match(g, room, a_uid: str, b_uid: str, day: str) -> Reply
def gift(g, room, from_uid: str, to_uid: str, item_id: str, now: int) -> Reply

# engines/duel.py
def challenge(g, room, a_uid, b_uid, now: int) -> Reply
def respond(g, room, a_uid, b_uid, accept: bool, now: int) -> Reply
def sweep(g, now: int) -> int

# engines/groupstat.py
def on_event(g, event: dict, now: int) -> None
def dive_report(g, room, days: int, limit: int) -> Reply
def invite_report(g, room, days: int) -> Reply
def rename_watch(g, room, enable: bool) -> Reply

# engines/moderation.py
def jail(g, room, uid, minutes: int, by_uid, reason, now) -> Reply
def is_jailed(conn, room, uid, now) -> bool
def block(g, room, uid, scope, by_uid, now) -> Reply
def blocked(conn, room, uid) -> bool
```

### 2.5 串行队列下的执行模型

事实：同一插件的回调单线程串行（`plugins.py:86-111`），队列满会丢弃（`plugins.py:76-84`）；定时回调与消息回调通过 `callback_lock(timeout=2)` 互斥（`plugins.py:304-336`），拿不到锁就抛错。

规则：

1. **`on_message` 只做 O(1) 内存工作**：`is_self` 判断、归一化、整词查表、一次短事务（目标 <50 ms）、返回 `Reply`。禁止任何网络/LLM/大文件 IO/大题库全量解析。归一化是纯字符串操作（NFKC + 去零宽 + 折空白），对 200 字消息 < 20 µs，可测。
2. **题库懒加载 + stat 缓存**：`content/loader.py` 首次访问某类目才读盘；之后最多每 60 s 复核一次 `(mtime, size)`，不每次消息都 stat。
3. **慢活（HTTP/LLM/图片）走 `adapters/executor.py`**：有界 `ThreadPoolExecutor(max_workers=4, queue=32)`，任务内部超时 3 s，结果放进「待发包」。
   - 主路径：任务完成时直接 `ctx.send_text(chat_id, ...)`。`sdk.py:111-156` 的发送路径有 `self._lock` / `self._send_lock` 与 `delivery_guard`，设计上支持多线程调用；**但跨线程调用的严格安全性尚未在有测试覆盖的场景下验证（未核实）**。
   - 兜底路径（若跨线程发送被证实不安全）：执行器只往内存 `PendingOut` 里塞结果，由下一次 `on_message` / `on_schedule` 统一 flush。延迟最多一个心跳周期，但绝不跨线程碰连接。
   - **2026-11-06 落地口径**：发送走主路径（worker 直接 `ctx.reply`，与 `scheduler.run_rename` 一致，且发送有 `_send_lock`）；**`Store` 上的写入一律走兜底路径**——`executor.defer()` 入队 + `App._flush()` 在回调线程执行。`PendingOut` 这一层没有单独实现，等价语义由 defer 队列提供。
4. **执行器线程禁止碰 `Store`**：DB 只在插件线程使用，连接永不跨线程。
5. **每命令异常兜底**：`try/except Exception` → `logger.exception` + 审计一行 + 回一句「这个玩法暂时出错了」；异常绝不上抛（上抛会记任务失败，`docs/development/plugins.md:92`）。
6. **功能开关**：`kv` 里 `feature.<engine>`，关闭时命令静默忽略，用于线上出问题时只关一个玩法而不停插件。

### 2.6 关键词路由：整词精确匹配、别名唯一、归一化

**一句话规则**：整条消息 `normalize()` 后，能切成 `触发词 [分隔符 参数…]` 且 `触发词` **整词**命中关键词表，才是命令；否则不是命令，**静默忽略**。

#### 2.6.1 归一化 `normalize()`

```python
# router.py
ZERO_WIDTH = "\u200b\u200c\u200d\u2060\ufeff"                    # 零宽空格/连字/词连接符/BOM
SPACE_ALL  = " \t\u00a0\u3000\u202f\u2005\u2009\u200a\u205f"     # 半角/制表/NBSP/全角/窄NBSP/微信艾特后空格/薄空格/发空格/中数学空格

def normalize(text: str) -> str:
    s = unicodedata.normalize("NFKC", text)                      # 全角→半角、兼容字符归一（含 U+3000 → ' '）
    s = s.translate({ord(c): None for c in ZERO_WIDTH})          # 必须在 NFKC 之后
    s = re.sub(rf"[{SPACE_ALL}]+", " ", s)                       # 各种空白折成一个半角空格
    return s.strip()
```

四条取舍，都要写进测试：

1. **先 NFKC 再删零宽**：NFKC 会把兼容字符展开成多字符序列，展开后可能带出新的零宽字符；反过来做会漏删。
2. **不做繁简转换**：不引入 opencc（新依赖 + 新歧义）。繁简差异写进帮助文案编写规范，运行期不映射。理由：`签到`/`簽到` 这种映射一旦引入，就必须回答"混写怎么办"，而答案只能是又一个映射表。
3. **不做 ASCII 大小写折叠**：69 个词全是中文，折叠对命令无意义；参数（`天气 Hangzhou`、`点歌 Yesterday`）按原样传给引擎，由引擎自己 `casefold()` 做模糊匹配。
4. **不剥标点**：`签到！`、`签到。` **不触发**。这与"精确匹配"同源——能剥一个标点，就等于给自己留了一个包含匹配的口子（`签到吧` 凭什么不剥"吧"？）。帮助文案里明写：**命令前后不加标点**。
5. **艾特范围先识别、再随规范化重映射**：艾特结构由适配层从**原始 `text`** 识别，得到 `MentionSpan(start, end, uid)`（下标对应原文）；`normalize_with_mentions(text, spans)` 在 NFKC、去零宽、空白折叠的**同一步**里同步映射每个范围的起止下标，返回的 span 指向规范化后的同一段文本。理由：NFKC 会改变长度（全角折半、兼容字符展开），删零宽与折叠空白也会整体位移，先规范化再按旧下标切必然错位。全角 `＃` 由 NFKC 折成半角 `#`，因此解析只处理 `#` 一种；参数里的全角 `＃` 同样会被折成 `#`，但**不因此变成分隔符**（只在命令首部拆一次，见 §2.6.2）。

#### 2.6.2 切分与匹配 `parse()`

```python
def parse(text: str, table: KeywordTable, *,
          mentions: tuple[MentionSpan, ...] = ()) -> Command | None:
    s, spans = normalize_with_mentions(text, mentions)   # 规范化同时重映射艾特范围
    if not s or len(s) > 64:                             # 超过 64 字不可能是「命令 + 少量参数」
        return None
    if mention_starts_at(spans, 0) or s.startswith("@"):  # 句首艾特 = 对人说话，不是命令
        return None

    cut, sep = None, None
    i = 0
    while i < len(s):                                     # 单次扫描，只看命令首部
        if mention_starts_at(spans, i):
            cut, sep = i, "@"                             # 艾特整体留在参数里
            break
        ch = s[i]
        if ch == " ":
            cut, sep = i, "space"
            break
        if ch == "#":
            cut, sep = i, "#"
            break
        if ch == "@":                                     # 兼容无空格手打 @
            cut, sep = i, "@"
            break
        i += 1

    head = s if cut is None else s[:cut]
    trig = table.head_index.get(head)                      # dict[str, Trigger]，O(1)
    if trig is None:
        return None

    if cut is None:
        args = ""
    elif sep == "@":
        args = s[cut:].strip()                             # 艾特连同 @ 一起给引擎
    else:
        args = s[cut + 1:].strip()                         # 只消耗这一个首部分隔符

    if trig.needs_arg and not args:
        return table.bare(trig)                            # 裸触发策略：run / guide / silent（§2.6.6）

    return Command(name=head, action=trig.action, raw=text, args=args,
                   tokens=tuple(args.split()),             # 不再 split("#")、不再 lstrip("@")
                   had_arg=bool(args), separator=sep, mentions=spans)
```

六条硬规则：

1. **`head` 是"整段无空白文本"**。`猜 成语`（中间有空格）**不触发** `猜成语`——它是两个词。**分隔符只有三种：空白、`#`、结构化艾特**，且**只拆命令首部的第一个**：`宝宝#改名#姓名` 的 `args` 是 `改名#姓名`，`宝宝 改名 A#B` 的 `args` 是 `改名 A#B`。这从结构上保证"包含匹配不可能发生"。
2. **`@` 与空白同权，都是分隔符**。`签到@张三`、`结婚@张三 同意`、`礼物@张三 玫瑰` 都能正确切出 `head=签到 / 结婚 / 礼物`。这是用户要求的「关键词@某人连写」归一化——微信把艾特渲染成 `@昵称` 文本，群友不打空格是常态，不能强制。
   - **反向不成立**：`@张三 签到` **不触发**（`head` 以 `@` 开头，查表必然落空）。有意为之：以艾特开头的消息是"对着某人说话"，不是命令。
   - `@` 后常跟 U+2005（四分之一空格），已由 `SPACE_ALL` 折成普通空格，不会把 `@张三` 误当成 `@` + 不可分割片段。
   - **`#` 与空白、艾特同权**：`宝宝#改名 小明` 等价于 `宝宝 改名 小明`。全角 `＃` 已由 NFKC 折成 `#`，因此 `宝宝＃改名 小明` 同样成立。**首部之后**的 `#` 与 `＃` 不再是分隔符，原样留给引擎（`宝宝 改名 A#B` → `改名 A#B`；`宝宝#改名 A＃B` → `改名 A#B`，内部发生了 NFKC 折半但仍不拆）。
3. **参数语义由引擎判，`parse` 只判"要不要参数"**。`猜成语 提示` 中 `猜成语` 是 `needs_arg=False`，`rest="提示"` 照样传下去，由 engine 决定"忽略多余参数直接开局"还是"回用法"。这样 `猜成语`（playhub 词）与 `提示`（框架词）可以共存而互不覆盖。
4. **未命中 → 静默忽略**：不回复、不审计、不打 WARN（只 `logger.debug` + 一个内存计数器，供 `/api/logs` 排查）。**这是无前缀方案下最重要的一条**：v1 里不带 `.` 的消息连 parse 都不进；v2 里**每条群消息都要过一遍 parse**，所以"未命中要绝对安静"，任何一次 `logger.info` 在千人群里都会变成日志洪水。
5. **艾特只认结构化范围，不按昵称反推**：参数里的艾特来自适配层给出的 `MentionSpan`（`uid` 已确认），引擎用 `cmd.mentions` 定位"参数里的第几个人"；`tokens` 不再剥 `@` 前缀，也不再拿昵称去查 wxid。**没有结构化范围、又没有可信终止符的艾特串**（纯文本里手打的 `@某某`）不猜身份，统一回「请使用有效艾特」，不进业务。理由：昵称可改可重名，按昵称反查等于把误伤写进数据。
6. **关键词优先于纯文本玩法**：`parse()` 命中即走命令分支，**不再进入**任何"消费纯文本"的引擎（竞猜抢答、接龙判定）。反之 `parse()` 未命中才是抢答答案的候选。这条顺序是硬约束（`__init__.py` 里先 `parse` 再 `answer`），否则对局中发 `签到` 会被当成错误答案——这也是 §2.7 冲突 2/3 在进程内的那一半解法。

**动作绑定与分批可用性**：关键词命中后，`registry` 查不到 action 时**静默 `return None`**，零回复、零日志噪音——与「关键词未命中」是两个分支，但对外表现必须一致。同时在插件加载时对本批已实现的 action 做一次**绑定自检**：缺失绑定直接启动报错，不用运行期静默去掩盖漏注册（§7 第 1 批验收第 3 条）。

**等价类表（逐条进 `tests/playhub/test_router.py`）**：

| 输入 | `args` | 说明 |
|---|---|---|
| `宝宝 改名 小明` | `改名 小明` | 基准（空白分隔） |
| `宝宝#改名 小明` | `改名 小明` | `#` 与空白等价 |
| `宝宝＃改名 小明` | `改名 小明` | 全角 `＃` 经 NFKC 折半后等价 |
| `宝宝#改名#姓名` | `改名#姓名` | 只拆命令首部第一个 `#` |
| `宝宝 改名 A#B` | `改名 A#B` | 参数内 `#` 原样保留 |
| `宝宝#改名 A＃B` | `改名 A#B` | 内部发生 NFKC 折半，但不作为分隔符 |
| `结婚@昵称#甲`（有完整艾特范围） | `@昵称#甲` | 艾特整体进参数，`#` 不拆 |
| `@昵称#甲 宝宝 看` | 不触发 | 句首艾特 = 对人说话 |
| `宝宝看` | 不触发 | 不新增前缀匹配 |

**子命令不注册新的顶层词**：`我的婚姻` / `度蜜月` / `申请离婚` / `婚姻统计` 是 `结婚` 的子命令，写在 `结婚` 的 `args` 里解析，**不**另注册四个四字顶层触发词（否则既违反 §2.6.4 的四字排除，也让词表虚增）。

#### 2.6.3 为什么删掉「最长匹配」

v1 的解析是「剥前缀 → 在剩余串上按长度降序做最长匹配」。v2 删掉，两条理由：

1. **精确匹配下不存在前缀歧义**。`head` 是整段无空白文本，`猜国漫` 与 `猜` 是两个不同的整词，`dict` 查表 O(1) 直接命中；不需要 trie、不需要按长度分组、不需要排序稳定性。命令总数 69，`dict` 比线性试匹配还快一个数量级。
2. **最长匹配会让"别名全局唯一"这条规则失去意义**。允许"更长的词优先"，`猜` 与 `猜国漫` 就能共存、共用前缀；在无前缀场景下这种共存直接变现成误触（`猜` 之后跟任何字都进竞猜）。禁止最长匹配 = 在结构上禁止这种共存。

代价：`猜王者荣耀` 这类"全称"不能与 `猜王者` 并存作为独立触发词，必须二选一。本设计选短词，全称进帮助文案：

> 帮助里写「猜王者（即猜王者荣耀）」，但**不注册** `猜王者荣耀`。

附带收益：`router.py` 140 → 110 行，少一个 trie 与一组排序测试；`router.py` 的测试从"匹配优先级"变成"归一化等价类 + 分隔符切分"，更好测。

#### 2.6.4 关键词表与冲突检测

```python
# keywords.py
@dataclass(frozen=True)
class Trigger:
    keyword: str                 # 归一化后的整词（构建期断言 normalize(k) == k）
    engine: str                  # 引擎名
    action: str                  # 动作 id，路由到 Engine.on_command
    needs_arg: bool = False
    bare_behavior: str = "run"   # "run" / "guide" / "silent"
    risk: str = "low"            # "low" / "mid" / "high"，与 §4.3 联动
    owner: str = "playhub"       # "playhub" / "framework"
```

`KeywordTable.build()` 在 `app.on_load` 跑四道校验，**任何一道都不静默降级**：

1. **归一化幂等**：`normalize(t.keyword) == t.keyword`，否则 `TriggerConflict`（表里写了带空格/零宽的词，说明有人手写表时没走 `normalize`）。
2. **全局唯一**：同一 `keyword` 出现两次 → `logger.error("触发词冲突：%r 同时属于 %s.%s 与 %s.%s", kw, e1, a1, e2, a2)` 并抛 `TriggerConflict`，异常消息里带**两个功能名**（满足"报出冲突的两个功能名"）。校验域 = playhub 62 词 ∪ 框架 `GROUP_CONTROL_COMMANDS` 的 8 个中文词（`wxbot_framework/text.py:8-9`；另两个是 `/help`、`/看图猜成语`，斜杠词不与中文词冲突）= **70 个词**。
3. **四字排除**：任何 `FOUR_CHINESE.fullmatch(keyword)` 为真的 **playhub 自有**词 → 拒绝加载该引擎（`FOUR_CHINESE` 见 `text.py:10`）。理由在 §2.7：接龙进行中四字中文会被 `idiom_chain` 当答案吞掉（`plugins/idiom_chain/__init__.py:110` + `judge.py:39`）。框架词（`看图猜成语`/`公布答案`/`成语接龙`/`合并接龙`/`退出接龙`）不受此限，它们本来就是接龙与看图猜成语自己的词。**§4 的 62 个词全部 ≤3 字，本约束当前恒成立，由测试钉死。**
4. **处置粒度**：`strict=True`（默认；测试、CI、首次安装）→ 抛异常，整插件不加载；`strict=False`（运行期热重载）→ 只 `disable_engine(engine)` 并写 `kv:trigger_conflict`，其余引擎照常。
   - **与用户决策的偏差，需拍板（§9）**：用户要求"冲突则拒绝加载该插件"；本设计改为"默认严格、运行期按引擎禁用"。理由：47 项在一个插件里，一个别名拼错让 47 项全下线，等于把开发期错误放大成线上故障；而按引擎禁用只损失 1–11 个功能，且冲突信息在 `/帮助` 里可见。

`disable_engine(engine)` 的语义：把该引擎的**所有**触发词从表中移除（不是留在表里再静默），否则别的引擎会以为这些词已被占用。`kv:trigger_conflict` 里存 `[{"keyword": "...", "a": "engineA.actionA", "b": "engineB.actionB"}]`，`/帮助` 渲染成「玩法 X 暂不可用（触发词冲突）」。

#### 2.6.5 反向不变量 I1：回包不得等于任何触发词

> **I1**：对 playhub 发出的任何文本 `t`，`normalize(t) ∉ KEYWORDS`（KEYWORDS = 69 个词全集）。

无前缀下，"回包恰好等于一个触发词"会形成**自触发循环**。三道闸，从强到弱：

1. **`is_self` 直接 return（主要防线）**：`Plugin.on_message` 第一行 `if message.is_self: return`。与文案无关、不会误判、不依赖框架行为。
2. **文本同形闸（第二道）**：`Plugin._emit()` 里 `if textkit.is_keyword(reply.text): reply = replace(reply, text=reply.text + "。")` + `logger.error` + `audit(action='err:self_trigger', result='failed')`。加一个句号即破坏精确相等，同时保留信息（比静默吞掉利于排查）。`textkit.is_keyword` 复用 `router.normalize`，保证与解析同一套归一化。
3. **测试闸**：`tests/test_playhub_self_trigger.py`
   - `test_static_replies_not_keyword`：遍历 `registry` 的每个 action，用样本参数渲染 `Reply`，断言 `normalize(text) ∉ KEYWORDS`。
   - `test_business_templates_not_keyword`：对 `business.py` 的每个可编辑函数用样本上下文渲染后同样断言。**这条是唯一能防住"网页改了文案导致自触发"的手段**——`business.py` 在运行期可编辑（`plugin_sources.py:14` 的 `EDITABLE` 含 `.py`）。
   - `test_self_echo_rejected`：把 69 个词逐个当作 `is_self=True` 的消息喂进 `Plugin.on_message`，断言全部返回 `None`（一条回复都不发）。

**为什么框架挡不住、必须自己挡（此处更正 v1 的事实错误）**：

- v1 写的是"仓库根 `__init__.py:58` 用 `GROUP_CONTROL_COMMANDS` 过滤自发消息"。**仓库根没有 `__init__.py`**（根目录只有 `main.py` / `web.py`）。真实位置是 `wxbot_framework/text.py:104-105`：`own_group_input = bool(msg_type == 1 and room and own_group_command(body))`，然后 `dispatch_to_plugins = not (room and is_self and not own_group_input)`；`own_group_command` 在 `text.py:13-17`，判据 = 在 `GROUP_CONTROL_COMMANDS` 内 ∨ 四字中文 ∨ 8 个 `/` 命令。另有同款判断在 `plugins/idiom_chain/__init__.py:58`。
- 推论 A：**2–3 字的自发文本默认不进插件**。本设计的 62 个自有词全是 ≤3 字 → 天然被挡。
- 推论 B：**四字中文会被放行**（`FOUR_CHINESE.fullmatch`）。而 playhub 复用的 4 个框架词里，`公布答案` 是四字、`提示`/`换一题`/`退出猜成语` 直接在白名单内 → 回包若等于它们，**会被送进插件**。
- 推论 C：用户开启 `include_self_groups`（`text.py:105`）后，所有自发群消息的 `dispatch_to_plugins` 都为真 → 全部放行。

所以第 1 道闸不冗余：它是推论 B / C 两条路径上唯一的确定性保护。**不要因为"框架已经过滤"就省掉它。**

#### 2.6.6 裸触发策略 `bare_behavior`

无前缀下，「整条消息只有一个词」是**最容易被误触**的形态（群友喊一声「宝宝」「礼物」）。因此每个词必须显式声明裸触发行为，**不提供全局默认值**（必须逐条写，漏写即构建期报错）：

| `bare_behavior` | 行为 | 用于（示例） |
|---|---|---|
| `run` | 裸发即执行 | 低误触词：`签到` `抽签` `黄历` `猜成语` `排行榜` `找茬` … |
| `guide` | 裸发回**一句**用法，且**每群每词 24 h 只回一次**（`kv:bare_guide.<kw>`） | 中误触词：`星座` `文案` `聊天` |
| `silent` | 裸发完全静默（不回复、不审计、只 `logger.debug`） | 高误触词：`宝宝` `礼物` `天气` `配对` `结婚` `闺蜜` `纳妾` `雇佣` `结拜` |

`guide` 的"24 h 只回一次"是关键：否则一条误触的「星座」会被回"用法：星座 狮子"，反而变成噪音源。代价是新人第一次裸发可能被静默（如果他之前误触过），所以 `/帮助` 必须把全部 62 个词连同参数形态列出来——这也是 §4 表格必须与 `keywords.py` 严格同步的原因（有测试断言词数一致）。

### 2.7 与现有插件的冲突及解法

**冲突 1（严重）：`idiom_chain` 吞掉接龙期间的一切短文本**

- 证据：`plugins/idiom_chain/__init__.py:110` —— `if not game or not text or text.startswith("/"): return`，即接龙开局后，任何不以 `/` 开头的文本都当成语答案送 LLM 判定（`:120` `judge.evaluate(...)`），`:132` `ctx.reply_at(...)` 真的会回一句。
- 后果（v1 靠"`.签到` 带点前缀"避开，**v2 前缀没了，这条退化为致命**）：接龙进行中，`签到`、`猜成语`、`天气 北京` 全部被当成成语答案，且每条都会收到「请输入一个完整的四字成语」。
- 更正 v1 的一个错误理由：v1 说改 `:110` 能"省 LLM 调用"，**这不成立**——`judge.py:39-40` 里 `if not FOUR_CHINESE.fullmatch(word): return Verdict(False, "请输入一个完整的四字成语")` 是本地短路，非四字输入从不请求 LLM。真正收益是**消除刷屏误回复**（每条普通聊天都会被回一句）。
- 方案 A（推荐，代价最小，但**需改现有插件，用户未批准**，已列入 §9）：

  ```python
  # plugins/idiom_chain/__init__.py:110
  - if not game or not text or text.startswith("/"):
  + if (not game or not text
  +         or text in GROUP_CONTROL_COMMANDS        # 已知关键词一律不接管
  +         or not FOUR_CHINESE.fullmatch(text)):     # 接龙答案本来就只能是四字
       return
  ```

  - **零判定损失**，可证：`judge.py:39` 只认四字、`judge.py:10` 的 SYSTEM 要求「只接受常见且真实的四字成语」、`judge.py:53` 也要求 `next_idiom` 为四字 → 非四字输入本来必然被判「不是成语」，在 `:110` 提前挡掉，判定结果完全相同。
  - **排除优先于四字**：`公布答案` 是四字中文且在白名单内，若只写 `not FOUR_CHINESE.fullmatch` 仍会被放行进插件（它会被判成"不是成语"并回一句）→ 必须先排除 `GROUP_CONTROL_COMMANDS`。用户原话「放行已知关键词」方向反了：这里是**拦下**已知关键词，不是放行。
  - 代价：改 1 个 if（3 行）+ 加 2 个用例（`签到` 不被接管、`一箭双雕` 仍被接管）到 `tests/test_idiom_chain.py`；需 `from wxbot_framework.text import GROUP_CONTROL_COMMANDS, FOUR_CHINESE`。风险：把「答案里的英文/数字混写」也挡掉（本来就会被 judge 拒），以及 playhub 未来若引入四字关键词会重新被吞（已由 §2.6.4 校验 3「playhub 自有词一律 ≤3 字」+ 测试钉死）。
- 方案 A'（同 A 但只加 `FOUR_CHINESE`、不加排除集）：改动更小，但 `公布答案`/`提示`/`换一题`/`退出猜成语` 四个框架词的行为会与接龙抢（`公布答案` 四字，会被吞）。**不推荐**。
- 方案 B（不改任何现有插件）：接受退化 —— 接龙进行中 62 个关键词全部失效，且每条都被回「请输入一个完整的四字成语」，等于接龙一开局这个群就不能用了。缓解：帮助文案提示「接龙进行中请先 `退出接龙`」。**无前缀方案下这是不可接受的产品状态**，只在用户拒绝方案 A 时兜底，并在 §8.6 记为已知限制。
- 方案 C：停用 `idiom_chain`，把 `成语接龙` 交给 playhub 重写。代价：等于重做 LLM 判定链路（`judge.py` 的 prompt + 校验 + 容错），并与 `chat_summary`/`personality` 的 LLM 配置耦合，约 +2 人日；且用户现有接龙数据/习惯迁移有成本。不推荐。
- **结论：方案 A 是唯一既能保住 47 项又不退化接龙的选择；因为要改 `plugins/idiom_chain/`（不在禁改列表里，但属现有插件），把它列为 §9 第一问，未获批前按方案 B 上线并在帮助文案里提示。**

**冲突 2：`picture_idiom` 与 playhub quiz 共用 4 个裸词**

- 事实：`plugins/picture_idiom/__init__.py:19-20` 也监听裸词「提示 / 换一题 / 公布答案 / 退出猜成语」，这 4 个词同时在框架 `GROUP_CONTROL_COMMANDS`（`text.py:8-9`）里。
- v1 的解法「playhub 子命令必须带前缀」已失效。v2 改为：这 4 个词是框架词，playhub 的 quiz 引擎**复用而不注册**，并且：
  1. **只在有未结束对局时消费**：`quiz_round` 有 `status='running'` 行才处理，否则立刻 return（不回复）；
  2. **探测对方是否激活**：`ctx.plugin_is_active('picture_idiom')`（用法先例见 `plugins/idiom_chain/__init__.py:77`），激活则本群默认让给 `picture_idiom`，除非 playhub 对局正在进行；
  3. 帮助文案把「按群二选一」写清楚：一个群同时开两种看图猜成语，视觉上无法区分谁在回。
- 未能消除的残留：两个插件**同时**有未结束对局时，一句 `提示` 会被两边各回一次。这是跨插件状态不可见的固有代价（见冲突 3），列入 §9 第四问。

**冲突 3：两个引擎同时消费纯文本（已用框架级共享槽解决，2026-11-05）**

若接龙与竞猜在同一群同时进行，纯文本会被两边都吃（playhub 的 `answer()` 与 idiom_chain 的判定各回一条）。最终解法不是「探测插件是否启用」，而是**一个群同时只允许一局游戏**，由框架提供共享槽：

- **`wxbot_framework/game_slot.py`（新增，进程内）**：`claim(room, owner, *, label="", ttl=None, now=None) -> str | None`、`release(room, owner=None) -> bool`、`holder(room)`、`holder_owner(room)`、`clear()`。键是群号，值是 `(owner, label, expires_at)`；别人占着时 `claim` 返回对方的 `label`，调用方据此拒绝开局。`ttl` 给「会自己超时的玩法」（竞猜一题 300 秒），不给就必须显式 `release`。
- **占用者**：`playhub`（label = 类目名，如「猜动物」）、`idiom_chain`（「成语接龙」）、`picture_idiom`（「看图猜成语」）。三处都是「开局 `claim` → 失败则回『本群还有一局 X 没结束，先发「结束」。』→ 收局 `release`」。
- **收局点必须覆盖全**：竞猜的答对、公布答案、结束、超时清理（`_sweep`）四处都 `release`；`换一题` 是关旧开新，改为重新 `claim` 续期。接龙/看图在 `退出`/`结束`/会话切换时 `release`。
- **单一结束词**：用户 2026-11-05 明确「游戏只能开始一个，结束就是结束当前游戏，不用在具体结束xxx」，因此全群统一的收局词是 **`结束`**（已加进 `text.py` 的 `GROUP_CONTROL_COMMANDS`），不再有 `退出猜动物`/`退出找茬` 这类按类目词；`退出猜成语` 仅作为框架词的兼容别名保留。回包仍要说清结束的是哪一局（「本局（猜动物）已结束」）。
- **局限（显式记入 §8.6）**：槽位只在本进程内存、重启即空、不跨进程。当前插件回调单线程串行，够用；将来若多进程分流必须换成落盘或 IPC。

**冲突 4：欢迎语双发**

`group_notice` 已实现进群欢迎/退群提醒（`plugins/group_notice/__init__.py:28-46`，`message_types:[10002]`，`default_enabled:false`）。playhub 若也发欢迎 = 群里两条。解法：**playhub 不发欢迎，只记事件计数**；欢迎/退群文案统一由 `group_notice` 负责，其模板仅支持 `{nickname}` 占位（≤700 字节），够用。

---

## 3 数据库设计

### 3.1 作用域与主键约定

| 概念 | 取值 | 说明 |
|---|---|---|
| `room` | 群聊 = 群 `chat_id`；私聊 = 对方 id | 所有玩法数据按房间隔离，防跨群刷币；私聊也可用 |
| `uid` | 成员 `wxid` | 从 `msg.sender_id` 取；为空则拒绝进入玩法（回「拿不到你的身份」） |
| `day` | `'YYYY-MM-DD'`（`Asia/Shanghai`） | 不用 `date('now')`，统一由 Python 传入，可测 |
| `at` / `now` | unix 秒（UTC） | 与 `Message.received_at` 同表示 |
| 账号 | 不落列，落在**文件路径** | 一个账号一个 DB 文件（§6），避免每表加 `account` 列 |

排序保证：`uid` 参与关系表的有序对时，`low_uid <= high_uid` 由 `CHECK` 保证，避免 (A,B)/(B,A) 两条。

### 3.2 DDL 草案

```sql
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

-- 按人归属的邀请记录（§8.7 C1：生产者是 core/invite.py，由 app.py:_feed_members 调用）
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
  action TEXT NOT NULL,                      -- cmd:结婚 / evt:join / sched:salary / err:quiz
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
```

#### 3.2.1 钱包相关的补充约束

- **每连接**执行 `PRAGMA foreign_keys = ON`（SQLite 默认关闭，必须显式开，否则上面两条 `ON DELETE RESTRICT` 全部失效）；延续 `WAL` + `busy_timeout = 3000`，写事务一律 `BEGIN IMMEDIATE`。
- 建立玩家时**同时初始化金币与钻石两个余额行，均为 0**；玩家只由 `wallet.ensure_player()` 创建，余额行缺失视为初始化异常（`debit` 影响 0 行时按此归类）。
- `reason` 使用**代码枚举**，绝不由聊天文本传入。v1 允许的钻石枚举：
  - 正向（**仅三处**）：`signin_day7`、`chest_gold`、`fortune_best`；
  - 负向：`blind_box_buy_attempts`、`marriage_reset_proposals`（纳妾在 v1 无实际扣款入口，不产生流水）。
- 中奖但奖励为 0 时**不写流水**（`delta <> 0` 由 CHECK 保证，不要靠调用方自觉）。
- `operation_key` 标识一次业务操作，`posting_no` 是该操作内的**确定性**分录序号；一笔业务同时奖励两种币时用两个不同序号，重放同一操作得到同一组分录。

#### 3.2.2 旧库迁移（9 步，一次性、可回滚）

从 v2 的 `player.coins` + 物理表 `coin_ledger` 迁到两币钱包，顺序固定：

1. 检查旧库里 `coin_ledger` 是**物理表还是视图**，按 `meta.schema_version` 选择迁移路径（老版本可能已经建过同名视图，不能盲目 `CREATE TABLE`）。
2. 旧表改名 `coin_ledger_v1_legacy`：`ALTER TABLE coin_ledger RENAME TO coin_ledger_v1_legacy`，**不丢数据**。
3. 新建 `wallet`、`wallet_ledger`。
4. `player.coins` 原值复制到金币钱包（`currency='coin'`），钻石钱包置 0。
5. 历史流水复制为 `currency='coin'`，`operation_key = 'legacy:coin:' || id`，`posting_no = 0`，保留原 `id` 编号。
6. 旧流水里 `delta = 0` 的记录**留在 legacy 表不导入**，并记录迁移数量，不静默混入新表。
7. 重建不含 `coins` 列的 `player`（`CREATE new → INSERT SELECT → DROP old → RENAME`），重建视图、索引与外键。
8. 创建只读 `coin_ledger` 视图，跑 `PRAGMA foreign_key_check`，核对三项：玩家数、金币总量、非零流水数。
9. 任一核对不一致 → **整体回滚**，保留 `coin_ledger_v1_legacy` 供人工排查。

迁移的三条纪律：迁移**不导入即减余额**（历史流水只做记录）；**不要求**历史净和等于余额，以 `player.coins` 原值为权威基线；旧写路径必须整体切到钱包接口，**不加可写视图触发器**（触发器会让「新代码禁止写视图」这条约束形同虚设）。

#### 3.2.3 两个结构决定

**决定一：独立 `wallet` 表，不在 `player` 上加 `diamonds` 列。** 币种约束、扣减语义、幂等键、流水写入四件事用同一套实现；`player` 只留资料与等级经验。代价是金币榜多一次限定币种的查询，由 `wallet_by_rank` 索引支撑，规模内可接受。也不提前为 v2 的银币/魅力/身价预留列——那只会让约束变松。

**决定二：活跃用实时日聚合 + 聚合永久保留，v1 不建 `rank_period` 快照。** 周/月/总榜直接对 `activity_daily` 做范围聚合（`(room, day, uid)` 索引支撑），不引入每日快照维护任务，也不靠删历史换总榜速度。代价是总榜查询随日聚合行数增长，先按「索引 + 现有群规模」验收；真到瓶颈再议，届时属于新决定，不在 v1 范围。

### 3.3 SQLite 限制与规避

| 限制 | 影响 | 规避 |
|---|---|---|
| 复合外键要求父键**完全相等**，不能写 `world IN ('*', room)` | `inventory.item_id` 无法 FK 到 `item_def(world,item_id)`，因为玩家行不知道 world | **不建外键**，由 `core/items.py:defs()` 在读取时合并「room 覆盖行 + 全局默认行」，写入前用 `add()` 校验 `item_id` 存在 |
| 部分唯一索引不能表达"A 在两条记录里分处高低位" | 一人一婚只能兜住一侧 | 事务内 `SELECT ... WHERE room=? AND kind='marriage' AND state='active' AND (? IN (low_uid, high_uid) OR ? IN (low_uid, high_uid))` 先查后写，包在 `BEGIN IMMEDIATE` 里 |
| 没有 `ON CONFLICT` 的 `CHECK` 组合语义 | 次数超限的并发 | `INSERT ... ON CONFLICT(room,uid,day,key) DO UPDATE SET used = used + ? WHERE used + ? <= cap` 后用 `changes()` 判断是否成功（原子） |
| `AUTOINCREMENT` 仅限 `INTEGER PRIMARY KEY` | 流水表要自增且不复用 | 大表用 `INTEGER PRIMARY KEY AUTOINCREMENT`；小字典/状态表用 `WITHOUT ROWID` 复合主键 |
| 单写者 | 多连接并发写会 `SQLITE_BUSY` | `WAL` + `busy_timeout=3000` + 写事务 `BEGIN IMMEDIATE`；插件内串行，`Store` 只开一条连接（§6） |
| 视图不能建索引 | 排行 `ORDER BY score DESC LIMIT 10` 在视图上要全扫 | 小群（<2000 行）可接受；大群靠 `wallet_by_rank`（含 `currency` 限定）与 `activity_by_room_day` 支撑；**不建 `rank_period` 快照**（3.2.3 决定二） |
| `DELETE` 不释放空间 | 审计/幂等表长期膨胀 | `idem` 按 30 天、`audit` 按 90 天定期 `DELETE` + 每年 `VACUUM`（运维手动，不做自动 VACUUM）；**`activity_daily` 是稀疏日聚合，永久保留，不参与任何定期清理**（90 天清理规则不适用于它） |
| 时间函数默认 UTC | 日切错 8 小时 | 不用 SQL 的 `date('now')`；`day` 一律由 `core/clock.py` 用 `Asia/Shanghai` 生成后传参 |

### 3.4 排行榜契约（四榜）

```python
Board  = Literal["coin", "activity", "marriage", "invite"]
Period = Literal["day", "week", "month", "all"]

def top(conn, *, board: Board, room: str, period: Period | None = None,
        now: int, limit: int = 10) -> tuple[RankRow, ...]
```

`period` 的取值约束：`board="coin"` 时 `period` **必须为 `None`**，传非 `None` 由 `top()` 抛 `ValueError`（金币榜是当前余额，没有时间维度）；`period=None` 对 `activity`/`marriage`/`invite` 三榜等价于 `"all"`。

统一口径，逐条进测试：

- **时间统一 `Asia/Shanghai`**（`core/clock.py`），不用 SQL 的 `date('now')`。
- 期间定义：`day` = 本地自然日；`week` = **周一至周日**；`month` = 本地自然月；`all` = 自本群开始采集以来。所有范围**左闭右开**。
- **活跃榜**分数 = `SUM(activity_daily.messages)`，沿用既有"有效消息"判定（不把机器人消息、命令消息重复计入）；**不把缺失历史补成真实统计**——新接群显示「自开始采集以来」，不假装有历史。
- 同分按 `uid` 稳定排序，名次连续（1、2、3…，不并列跳号）。
- **金币榜**是当前金币余额（`v_rank_coin`），**不接受时间参数**；钻石不混入。
- **婚姻榜**：按婚龄降序，**夫妻合并一行**（`A ❤ B · N 天`），已离婚不上榜；婚龄相同按结婚时间升序。
- **邀请榜**：默认总榜（自开始采集以来），支持日/周/月；同一 `invitee` 对同一 `inviter` **终身只计 1 次**；机器人自身的邀请计分；`actor_id` 缺失的记录只进群级统计，不进按人榜。
- 月/总榜**直接查日聚合**，不生成 `rank_period` 快照（§3.2.3 决定二）。

---

## 4 玩法与触发词总表（47 项）

图例：**触发词**=主词（归一化后整词，全部 ≤3 字）；**别名**=同一功能的其它整词，与主词平权，**全局唯一**（§2.6.4 校验 2）；**需参数**=裸发（只发触发词、不带参数）时是否走 `bare_behavior`（§2.6.6）；**误触**=无前缀场景下被正常聊天误触发的风险等级，`高`/`中` 的治理方案见 §4.3；**状态**=是否需要持久游戏状态（是/否）；**定时**=是否需要定时动作；**归属**=新写/复用。

| # | 触发词 | 别名 | 引擎 | 参数 | 需参数 | 误触 | 状态 | 定时 | 数据依赖 | 归属 |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `签到` | `打卡` | daily | 无 | 否 | 低 | 是（次数+冷却+连签） | 否 | `player` `wallet` `wallet_ledger` `daily_counter` `cooldown` `kv` | 新写（**连签第 7 天产钻石**，见 §4.4） |
| 2 | `点歌` | `点唱` | music | 歌名/关键词 | 是 | 低 | 是（冷却+发歌队列） | 否 | `cooldown` `audit` `kv`(缓存) | 新写（外部源，见 §8） |
| 3 | `抽签` | `求签` | daily | 无 | 否 | 低 | 是（每日 1 次） | 否 | `daily_counter` `wallet` `wallet_ledger` `kv` | 新写（签文库 `fortune.jsonl`；**上上签产钻石**，见 §4.4）；附 `解签`（当天抽过签才回释义，不发币、不限次、跨天失效） |
| 4 | `切磋` | `比武` | duel | `@对象` / `同意` / `拒绝` | 是 | 低 | 是（待应战态） | 是（10 min 清超时） | `game_state` `wallet` | 新写（**不联动监狱/医院**，状态存 `game_state`，不写 `jail`、不建临时婚姻） |
| 5 | `黄历` | `老黄历` | daily | 偏移日（`明天`/`昨天`） | 否 | 低 | 否（纯函数） | 否 | `almanac.jsonl` | 新写 |
| 6 | `礼物` | `送礼` | social | `@对象` + 物品 | 是 | **高** | 是（背包） | 否 | `inventory` `item_def` `lottery_draw` `audit` | 新写（见 §4.3） |
| 7 | `结婚` | `求婚` | social | `@对象` / `同意` / `拒绝` / `我的婚姻` / `度蜜月` / `申请离婚` / `婚姻统计` / `清求婚次数` | 是 | **高** | 是（`pair`） | 否 | `pair` `wallet` `wallet_ledger` `daily_counter` `audit` `kv` | 新写（**默认关闭**；v1 含 4 个子命令 + 清求婚次数；离婚走 200 金币 + 对方 24h 确认，见 §4.4） |
| 8 | `宝宝` | — | social | 子命令：`看` / `喂养` / `改名 X` / `超极` | 是 | **高** | 是（`baby`） | 是（每日成长，可选） | `baby` `wallet` `inventory` | 新写（**v1 基础版**：`看`/`喂养`/`改名 X`/`超极`；`超极` 只说明 v2 开放，**不执行升级、不扣费**，见 §4.3） |
| 9 | `闺蜜` | — | social | `@对象` / `同意` / `拒绝` / `解除 @对象` | 是 | **高** | 是（`pair: sister`） | 否 | `pair` | 新写（**默认关闭**，见 §4.3） |
| 10 | `纳妾` | — | social | `@对象` / `同意` / `拒绝` / `解除 @对象` | 是 | **高** | 是（`pair: concubine`） | 否 | `pair` `kv`(开关) | 新写（**默认永久关闭**；30 钻石是准入门槛、**不是扣款**，v1 无任何钻石扣款入口，见 §4.3） |
| 11 | `雇佣` | — | social | `@对象` / `工资` / `解除 @对象` | 是 | **高** | 是（`pair: employ`） | 是（每日发工资） | `pair` `wallet` | 新写（**默认关闭**，见 §4.3） |
| 12 | `结拜` | — | social | `@对象` / `同意` / `拒绝` / `解除 @对象` | 是 | **高** | 是（`pair: brother`） | 否 | `pair` | 新写（**原「兄弟」不再注册**，见 §4.3） |
| 13 | `天气` | — | adapters/outbound | 城市名（必填） | 是 | **高** | 否（短缓存） | 否 | `kv`(缓存) `outbound` | 新写（外部 API，见 §4.3） |
| 14 | `星座` | `运势` | daily | 星座名（省略取自己） | 否 | 中 | 否（日确定性） | 否 | `horoscope.jsonl` | 新写（见 §4.3） |
| 15 | `配对` | `缘分` | social | `@对象`（必填） | 是 | **高** | 否（`hash(room,day,a,b)` 确定性） | 否 | 无（纯计算） | 新写（见 §4.3） |
| 16 | `盲盒` | `抽盲盒` | lottery | 次数（默认 1） | 否 | 低 | 是（货币+次数+爆率） | 否 | `wallet` `daily_counter` `lottery_draw` `item_def` `inventory` | 新写（5 钻石/次加购抽奖次数，每日限购 2 次，见 §4.4） |
| 17 | `文案` | `情话` | lounge | 主题（可选） | 否 | 中 | 否（轮转，`quiz_stat` 式游标放 `kv`） | 否 | `copy.jsonl` `kv` | 新写（见 §4.3） |
| 18 | `活跃榜` | — | ranking | `日` / `周` / `月` / `总` | 否 | 低 | 否（实时聚合 `activity_daily`） | 否（不建快照任务） | `activity_daily` | 新写（周一为周起点，时间口径 Asia/Shanghai，见 §3.4） |
| 19 | `排行榜` | `榜单` | ranking | `金币` / `活跃` / `婚姻` / `邀请` | 否 | 低 | 否 | 否 | `wallet` `activity_daily` `pair` `invite_credit` | 新写（**v1 四榜**，见 §3.4） |
| 20 | `猜成语` | — | quiz | 无 | 否 | 低 | 是（`quiz_round`） | 是（5 min 收题） | `quiz_round` `quiz_stat` `quiz_attempt` | 新写（子命令复用框架词，见下） |
| 21 | `猜歌名` | `猜歌` | quiz | 无 | 否 | 低 | 是 | 是 | 同上 | 新写（**只用文字提示，不发音频**） |
| 22 | `猜谜语` | `猜谜` | quiz | 无 | 否 | 低 | 是 | 是 | 同上 | 新写 |
| 23 | `猜电影` | — | quiz | 无 | 否 | 低 | 是 | 是 | 同上 | 新写（**不用海报**） |
| 24 | `猜明星` | — | quiz | 无 | 否 | 低 | 是 | 是 | 同上 | 新写 |
| 25 | `猜动物` | — | quiz | 无 | 否 | 低 | 是 | 是 | 同上 | 新写 |
| 26 | `猜植物` | — | quiz | 无 | 否 | 低 | 是 | 是 | 同上 | 新写 |
| 27 | `猜美食` | — | quiz | 无 | 否 | 低 | 是 | 是 | 同上 | 新写 |
| 28 | `猜王者` | — | quiz | 无 | 否 | 低 | 是 | 是 | 同上 | 新写（全称「猜王者荣耀」只进帮助文案，不注册） |
| 29 | `猜动漫` | — | quiz | 无 | 否 | 低 | 是 | 是 | 同上 | 新写（独立题库） |
| 30 | `猜国漫` | — | quiz | 无 | 否 | 低 | 是 | 是 | 同上 | 新写（**独立题库，与 29 不共用**） |
| 31 | `关系卡` | — | social | `@对象`（可选，省略取自己） | 否 | 低 | 否（读关系） | 否 | `pair` `baby` `player` | 新写 |
| 32 | `求财神` | `拜财神` | daily | 无 | 否 | 低 | 是（每日 1 次） | 否 | `daily_counter` `wallet` `wealth.jsonl` | 新写 |
| 33 | `小黑屋` | — | moderation | `@对象 分钟` / `解除 @对象` / `列表` | 是 | 低 | 是（`jail`） | 是（5 min 到期解禁） | `jail` `audit` | 新写（**软禁言**，无 kick/mute 能力；设置/解除**需 owner/admin 门禁**） |
| 34 | `黑名单` | — | moderation | `加/删/列 @对象` | 是 | 低 | 是（`blocklist`） | 否 | `blocklist` `audit` | 新写（增加/移除**需 owner/admin 门禁**） |
| 35 | `开宝箱` | `宝箱` | lottery | 箱型（`铜`/`银`/`金`） | 否 | 低 | 是（背包/钥匙） | 否 | `inventory` `item_def` `lottery_draw` `wallet_ledger` | 新写（**金箱 1/8 概率 +2 钻石**，见 §4.4） |
| 36 | `群空间` | — | lounge | 无 | 否 | 低 | 是（聚合缓存） | 是（每小时刷新） | 全部只读视图 + `activity_daily` | 新写（群内文本卡片；控制台页面见 §9） |
| 37 | `成语接龙` | `合并接龙` `退出接龙` | — | — | — | — | 现有插件持有 | 否 | `idiom_chain` 自有 | **复用 `idiom_chain`**，playhub 不实现、**不注册这些词**（它们已在框架表 `text.py:8-9` 内；与 playhub 的冲突见 §2.7 冲突 1） |
| 38 | `找茬` | `找不同` | quiz | 无 | 否 | 低 | 是（`game_state:diff`） | 是（5 min 收题） | `game_state` `diff.jsonl` | 新写（v1 写作「文字找茬」，词形改短） |
| 39 | `问答` | — | quiz | 无 | 否 | 低 | 是 | 是 | `quiz_round` `trivia.jsonl` | 新写 |
| 40 | — | — | social | 并入 `宝宝 超极`（子命令） | — | — | 是（`baby.is_super`） | 否 | `baby` `inventory` `wallet` | 新写（**不占触发词**；v1 只回「v2 开放」，**不升级、不扣费**，见 §4.3） |
| 41 | `潜水` | — | groupstat | 天数（默认 7） | 否 | 低 | 否（读聚合） | 是（每日 23:50 汇总，可选） | `activity_daily` `member_seen` | 新写（**只统计、不清人**；自建计数，成员库没有按人计数接口） |
| 42 | `邀请` | `邀请榜` | groupstat | 天数（默认 7） | 否 | 低 | 是（`invite_daily`） | 否 | `invite_daily` `invite_credit` | 新写（按入群事件的 `actor_id` 统计；缺失降级为群级，见 §8.7） |
| 43 | — | — | — | 进群欢迎（事件，无触发词） | — | — | 现有插件持有 | 否 | `group_notice` 自有 | **复用 `group_notice`**（`default_enabled:false`，需在控制台启用）；playhub 只记 `invite_daily.joins` |
| 44 | — | — | — | 退群监控（事件，无触发词） | — | — | 现有插件持有 | 否 | `group_notice` 自有 | **复用 `group_notice`** 的 leave 模板；playhub 只记 `leaves`/`kicked` |
| 45 | `改名` | — | groupstat | `开` / `关` | 是 | 低 | 是（`kv` + `member_seen`） | 是（每日巡检一次） | `member_seen` `nickname_log` `group_members()` | 新写（群管理开关，**需 owner/admin 门禁**；v1 写作「改名提醒」，词形改短） |
| 46 | — | — | 框架控制台 | 控制台「定时任务」页的定时文本任务 | — | — | 否 | 否 | `scheduled_tasks.json`（框架侧） | 复用框架控制台定时文本任务，不写新代码（playhub 不声明 `scheduled_actions`，不做批准计划；见 §4.2） |
| 47 | `聊天` | — | — | `开` / `关` | 是 | 中 | 现有插件持有 | 否 | `mention_chat` `personality` `chat_summary` | **复用**；playhub 只提供 `聊天 开/关`（写 `kv` 开关，是否生效由 `mention_chat` 侧判断——**未核实其是否有开关配置**，见 §4.3） |

**触发词账**（与 §2.6.4 的 69 词校验域对齐，测试断言词数一致）：

| 项 | 词数 | 说明 |
|---|---|---|
| playhub 自有主词 | 44 | 表内 42 个功能项各 1 个（第 40 项并入 `宝宝`，第 37/43/44/46 项无触发词 → 47 − 5 = 42），再加本批新增的 `帮助`（`business.help`）1 个 → 43；第 4 批再加 `解签`（`daily.fortune_explain`，归第 3 项抽签）→ 44 |
| playhub 自有别名 | 18 | `打卡` `点唱` `求签` `比武` `老黄历` `送礼` `求婚` `运势` `缘分` `抽盲盒` `情话` `榜单` `猜歌` `猜谜` `拜财神` `宝箱` `找不同` `邀请榜` |
| **playhub 自有合计** | **62** | 全部 ≤3 字，无四字中文（§2.6.4 校验 3） |
| 复用框架词 | 4 | `提示` `换一题` `公布答案` `结束`（`退出猜成语` 为兼容别名） —— quiz 引擎的子命令，**不注册、不占自有词**；由框架表（`text.py:8-9`）放行进插件，playhub 只在 `quiz_round` 有未结束对局时消费（见 §2.7 冲突 2） |
| 框架表其余 4 个中文词 | 4 | `成语接龙` `合并接龙` `退出接龙` `看图猜成语` —— 归 `idiom_chain` / `picture_idiom`，playhub 不碰 |
| **全局唯一性校验域** | **62 + 8 = 70** | playhub 62 与新注册框架 8 个中文词两两不重；2 个斜杠词（`/help` `/看图猜成语`）不与中文词冲突 |
| 表内不注册但需在帮助文案出现的词 | 8 | `成语接龙` `合并接龙` `退出接龙` `看图猜成语` `提示` `换一题` `公布答案` `退出猜成语`（都不进 playhub 的关键词表，只由帮助文案列出；其中 4 个是 quiz 子命令） |

统计：共 **47 项** —— 新写 **43 项**、复用 **4 项**（37/43/44/47）；其中 **5 项不占触发词**（37/40/43/44/46）。新增触发词 62 个（44 主词 + 18 别名）。

第 1 批追加第 61 个词 `帮助`（动作 `business.help`）：`/help` 由各插件各自处理、框架不为它做聚合，playhub 不能共用，因此单列一个整词。

### 4.1 复用 / 重写判定

| 项 | 判定 | 理由 |
|---|---|---|
| 成语接龙 | **复用 `idiom_chain`** | 已含 LLM 判成语链路与对局状态（`plugins/idiom_chain/__init__.py` 4 文件 10.3 KiB）；重写要重建判定与人格配置耦合，价值低。`成语接龙`/`合并接龙`/`退出接龙` 是**框架词**（`wxbot_framework/text.py:8-9`），由 `idiom_chain` 直接响应，playhub **不注册、不映射、不介入**；v1 的"`.成语接龙` 映射成一句引导"随前缀下线一并取消。两者共存问题见 §2.7 冲突 1。 |
| 进群欢迎 / 退群监控 | **复用 `group_notice`** | 已实现 join/leave 模板（`plugins/group_notice/__init__.py:28-46`），模板仅 `{nickname}` 占位，够用；两处都发会造成双欢迎。playhub 只订阅同一事件做**计数**，不发消息。 |
| 智能聊天 | **复用 `mention_chat` + `personality` + `chat_summary`** | 三者覆盖 @触发、人格、总结；playhub 重做等于再写一套 LLM 调用与人格读取。 |
| 猜成语（文字） | **新写** | 与 `idiom_chain`（接龙）和 `picture_idiom`（看图）都不同：这是"文字提示 → 抢答"的竞猜，共用 `quiz` 引擎。`picture_idiom` 作为竞猜引擎的**状态机参考实现**（`plugins/picture_idiom/__init__.py:19-20` 的命令集、`:48-57` 的运行数据目录写法）。 |
| 其余 43 项 | **新写** | `plugins/` 下现有插件清单中无对应实现（`anti_recall*` `chat_records` `chat_summary` `echo` `friend_auto_accept` `group_memory` `group_notice` `growth` `id_responder` `idiom_chain` `image_*` `member_identity` `mention_chat` `personality` `picture_idiom` `voice_reply`） |

### 4.2 后台定时动作（插件自管线程，2026-11-06 定）
**定案**：第 4 批的 5 个后台动作由 playhub **自带的一个后台线程**执行，节奏照抄 `plugins/growth/__init__.py:35-66`（`threading.Event` + daemon 线程 + `stopped.wait()`）。**不**走控制台 `scheduled_tasks`、**不**在 `plugin.json` 注册 `scheduled_actions`、**不**做群内「推送」批准命令、**不**做 `plan_id/revision` 批准契约（原 v3 门禁方案整段作废）。第 46 项「定时推送」直接用框架控制台的定时文本任务，playhub 不写代码。原清单里的 `daily_quiz`、`daily_almanac`、`daily_report`、`space_refresh` **不做**，也不在 47 项之内（直接删除）；第 46 项只是用户自己在控制台配的定时文本，与 playhub 无关。

| # | 动作 | 节奏 | 发群消息 | 受哪个开关 |
|---|---|---|---|---|
| 1 | 改名巡检 `rename` | 每天 1 次，本地 10:00–22:00 窗口内 | **是**（唯一会发消息的动作） | `groupstat.rename_watch`，**读不到 = 开**，`改名 关` 关 |
| 2 | 关系超时收尾 `expire` | 每个 tick | 否 | 无（与婚姻子命令里的惰性收尾同一函数） |
| 3 | 收题与清理 `gc` | 收题每个 tick；清 `idem`/`audit` 每天 1 次（04:00 后） | 否 | 无 |
| 4 | 发工资 `salary` | 每天 1 次（00:05 后），补发最多 3 天 | 否 | `feature.employ`（`switches.is_on`，读不到 = 关） |
| 5 | 周榜发银箱 `chest` | 每周 1 次（周一 00:10 后，本周内任何一天都可补） | 否 | 无 |

#### 4.2.1 模块与签名

新文件 `plugins/playhub/scheduler.py`（放在包根，和 `app.py` 平级）。理由：它要调 `engines/social`、`engines/quiz`、`engines/groupstat`、`engines/ranking`，放进 `core/` 会让 core 反向依赖 engines；它也不是命令引擎，不该进 `engines/`、不进 `build_engines()`。

```python
# plugins/playhub/scheduler.py
FIRST_DELAY_SECONDS = 300      # 首次延迟：等微信连上、群成员缓存就绪
FIRST_DELAY_JITTER = 60        # 首次延迟再加 [0, 60) 秒随机（rng 注入），避免和 growth 等插件同一秒醒
TICK_SECONDS = 600             # tick 间隔 10 分钟
JOIN_TIMEOUT_SECONDS = 10      # on_unload 等线程退出的上限

RENAME_WINDOW = (10, 22)       # 本地小时 [10, 22)；窗口外不播报，也不补
SALARY_AFTER = (0, 5)          # 本地 00:05 之后才发当天工资
SALARY_CATCHUP_DAYS = 3        # 停机后最多补 3 天（含今天）
CHEST_AFTER = (0, 10)          # 周一 00:10 之后发上周银箱
CHEST_TOP_N = 10
GC_AFTER = (4, 0)              # 每天 04:00 后清 idem / audit
IDEM_RETENTION_DAYS = 30
AUDIT_RETENTION_DAYS = 180
PURGE_BATCH = 5000             # 每个事务最多删这么多行，单个写锁持有时间可控
GLOBAL_ROOM = "*"              # kv 里全库级游标的 room 值（沿用 item_def.world='*' 的写法）
ACTIVE_ROOM_DAYS = 7           # 只给最近 7 天在 activity_daily 有记录的群播报改名

@dataclass(frozen=True)
class TickReport:
    renamed: int = 0          # 播报出的改名处数
    expired: int = 0          # 收掉的超时请求/离婚单
    quiz_closed: int = 0
    idem_purged: int = 0
    audit_purged: int = 0
    salary_paid: int = 0      # 成功发放的 (pair, day) 数
    salary_skipped: int = 0   # 余额不足 / 数据坏 / 开关关
    chests: int = 0
    errors: int = 0

class Scheduler:
    def __init__(self, *, ctx, root: Path, now_fn=clock.now_ts, rng: random.Random | None = None,
                 logger=None, store_factory=Store, sleep_event: threading.Event | None = None) -> None: ...
    def start(self) -> None                      # 起 daemon 线程，名字 "playhub.scheduler"
    def stop(self, timeout: float = JOIN_TIMEOUT_SECONDS) -> bool   # set + join；返回线程是否已退出
    def tick(self, now: int | None = None) -> TickReport            # 同步跑一轮，测试直接调它

# 每个动作都是模块级函数，只吃 Store / conn，不碰线程，便于离线测：
def run_rename(store: Store, ctx, room: str, now: int, *, send: Callable[[str, str], object]) -> int
def run_expire(store: Store, social: SocialEngine, room: str, now: int) -> int
def run_quiz_gc(store: Store, quiz: QuizEngine, now: int) -> int
def run_purge(store: Store, now: int) -> tuple[int, int]            # (idem 删除数, audit 删除数)
def run_salary(store: Store, room: str, day: str, now: int) -> tuple[int, int]   # (发放数, 跳过数)
def run_chest(store: Store, room: str, week: str, now: int) -> int
def is_due_daily(last: str | None, today: str, local_hm: tuple[int, int], after: tuple[int, int]) -> bool
def is_due_weekly(last: str | None, this_week: str, local_wd_hm: tuple[int, int, int], after: tuple[int, int]) -> bool
```

为实现上述函数需要改的现有代码（都是小改动，签名定在这里）：

- `plugins/playhub/__init__.py`：把 `_runtime()` 里拼目录的那段（`:70-75`）抽成 `db_path_for(root: Path, account: str) -> Path`，前台和后台共用同一套路径规则；`on_load` 末尾 `self._scheduler = Scheduler(ctx=ctx, root=root, logger=...)` 并 `start()`；`on_unload` 先 `self._scheduler.stop()` 再关前台连接。
- `engines/social.py`：新增公开方法 `SocialEngine.expire_room(self, conn, room: str, now: int) -> int`，内部调 `_expire`（`:1249`）并返回收掉的条数；前台 `:481`、`:817` 两处不变。
- `engines/quiz.py`：`_sweep(..., release_slots: bool = True)`、`sweep(conn, now=None, *, release_slots=True)`；后台传 `release_slots=False`（理由见 4.2.4 收题）。
- `engines/groupstat.py`：新增 `RENAME_SNAPSHOT_KEY = "groupstat.rename_snapshot"`、`watch_on(conn, room) -> bool`（读不到 → `True`；值在 `0/off/false/no/disabled` → `False`；读失败 → `False`，失败不播报）、`diff_names(prev: dict[str, str], rows: list[tuple[str, str]]) -> list[tuple[str, str, str]]`；`_scan_now`（`:245-285`）改用同一快照基线（理由见 4.2.4 改名）。`改名 开/关` 的写法（`:226-228`）不变，开卡文案不变。
- `core/wallet.py` 不改；工资用 `debit`/`credit` 两条分录。

#### 4.2.2 线程与 SQLite（问题 A）

**后台线程自建一条独立的 `Store` 连接，不与 `on_message` 共享连接，也不自建锁。**

- 依据：`Store` 注释写明「`check_same_thread=False` 只是给线程内迁移留余地，运行时仍单线程使用」（`core/store.py:34`）；同一条 `sqlite3.Connection` 在两个线程里交错 `BEGIN IMMEDIATE`，第二个会直接报 `cannot start a transaction within a transaction`，`tx()`（`store.py:100`）的提交/回滚边界也会串。若改成「共享连接 + 插件内锁」，需要把前台所有 `store.tx()` 调用点都包进锁，侵入每个引擎。
- 可行性：`schema.sql:5` 是 `PRAGMA journal_mode = WAL;`，`migrate()`（`store.py:70` 的 `executescript`）执行后 WAL 持久写进库文件，之后所有连接都是 WAL：读不阻塞写、写不阻塞读。写写互斥靠 `BEGIN IMMEDIATE` + `busy_timeout`（`store.py:40`，默认 `timeout=3.0` → 3000 ms）。
- 约束（后台必须守）：
  1. **一个事务只做一个群的一个动作**（工资再细到一个 `pair`、一天），写锁持有时间是毫秒级，前台最坏等一个后台小事务，远小于 3 s。
  2. 后台事务里**不调任何微信接口**（不读快照、不发消息）。改名巡检的顺序是：事务外读快照 → 事务内比对并落库 → 事务外发消息。
  3. 拿锁超时（`sqlite3.OperationalError: database is locked`）只跳过这个群这个动作，**不写 last_run**，下个 tick 重来；所有写都幂等（4.2.5），重来不会重复。
  4. 连接在后台线程里创建、在后台线程退出时关闭（`finally: store.close()`），不跨线程传递。
- `game_slot` 是带 `threading.Lock` 的进程内表（`wxbot_framework/game_slot.py:21`），跨线程调用本身安全，但有时序问题，见 4.2.4 收题。

#### 4.2.3 账号与群（问题 B）

- **账号**：每个 tick 开头读一次 `ctx.members.account`，与前台 `_runtime()`（`plugins/playhub/__init__.py:55`）同一来源。`MemberStore.from_config` 要求配置里有固定的 `wechat_account`（`wxbot_framework/members.py:173-174`），所以这个值在启动时就有，不依赖首条消息。读到空串、`CapabilityError`（没配成员目录）或非字符串 → 本 tick 什么都不做，只在第一次记一条 warning（与前台 `_warned` 同口径）。账号变了（面板换号）→ 关掉旧连接、按新账号路径开新连接；**不**遍历磁盘上其它账号的库（失败关闭：只处理当前登录账号）。
- **库文件**：`db_path_for(root, account)`，与前台完全同一路径；库文件还不存在 → 本 tick 跳过（前台还没处理过任何消息，不替它建库）。存在则打开并 `migrate()`（幂等）。
- **群**：不从框架取群列表（`PluginContext` 不暴露 `chat_allowlist`），而是从本库已有数据推出来，每个动作各查各的：

| 动作 | 群集合 SQL（全部参数化） |
|---|---|
| rename | `SELECT DISTINCT room FROM activity_daily WHERE day >= ?`（`day_of(now) - 7 天`） |
| expire | `SELECT DISTINCT room FROM kv WHERE key LIKE 'divorce:%' UNION SELECT DISTINCT room FROM pair WHERE state = 'pending'` |
| salary | `SELECT DISTINCT room FROM pair WHERE kind = 'employ' AND state = 'active'` |
| chest | `SELECT DISTINCT room FROM activity_daily WHERE day BETWEEN ? AND ?`（上周一~上周日） |
| gc | 不分群（`quiz._sweep(room=None)` 本来就是全库，`idem`/`audit` 按 `at` 删） |

  前台消息只有在 `chat_allowlist` 内才会分发给插件（`wxbot_framework/runtime.py:144-161`，`_emit` 里 `allowed = self.config.get('chat_allowlist', [])`；文本分发见 `:229-239`），所以有活跃记录说明该群至少曾在白名单内。「7 天活跃」保留作减负，**不再**承担白名单职责；白名单由下面的 `ctx.chat_allowed` 判。

**框架只读白名单接口（用户选方案 A，2026-11-06 定）**

- 签名：`PluginContext.chat_allowed(self, chat_id: str) -> bool`。语义与 `runtime.py:144` 一致：白名单为空 = 全部允许；非空时 `chat_id in allowed`；`chat_id` 不是非空字符串 → `False`；读取白名单抛异常 → `False`（失败关闭）。
- 白名单从哪里来：`PluginContext.__init__`（`wxbot_framework/sdk.py:475-486`）现在只拿到本插件的 `options`（`:478` 的 `self.config = MappingProxyType(copy.deepcopy(options))`，是装载时的深拷贝，拿不到全局 `chat_allowlist`）。照 `plugin_active` 的先例（`:486` 存回调、`:488-489` `def plugin_is_active(self, name):` 调回调）新增关键字参数 `chat_allowlist=None`（一个无参 callable，返回当前白名单列表）。`PluginManager._prepare`（`wxbot_framework/plugins.py:195-202`）创建 context 时传 `chat_allowlist=lambda: list(self.config.get("chat_allowlist", []) or [])`。
- 热更新：`PluginManager.config` 由 `store.load()` 初始化（`plugins.py:152`），面板保存后 `BotApplication.apply_config` 执行 `self.plugins.config = dict(config)`（`runtime.py:61`，注释「插件管理器自己留了一份配置拷贝，不刷新的话 enabled/群白名单还是旧的。」），`reload` 也会重读（`plugins.py:279`）。回调在**调用时**读 `self.config`，所以已装载插件不用重载就能看到新白名单，和 `runtime._emit` 看到的是同一次保存。不在 `__init__` 里拷贝列表，就是为了拿到热更新。
- 未注入回调（单测里直接 `PluginContext(...)` 构造）→ 返回 `True`，等同「白名单为空」，不破坏现有测试。playhub 侧用 `getattr(ctx, "chat_allowed", None)`：框架没有这个方法（旧版本）→ **不播报**（失败关闭）。
- `run_scheduled` 里 `copy.copy(record.context)`（`plugins.py:328`）是浅拷贝，回调共享，不受影响；控制台定时任务仍走 `ScheduledService` 自己的白名单（`plugins.py:46`），两者口径相同。

框架改动清单（交 builder）：

| 文件 | 函数 | 改动 |
|---|---|---|
| `wxbot_framework/sdk.py` | `PluginContext.__init__` | 加关键字参数 `chat_allowlist=None`，存为 `self._chat_allowlist` |
| `wxbot_framework/sdk.py` | 新增 `PluginContext.chat_allowed(self, chat_id)` | 按上面的语义实现，放在 `plugin_is_active` 旁边 |
| `wxbot_framework/plugins.py` | `PluginManager._prepare` | 构造 `PluginContext` 时传 `chat_allowlist=lambda: list(self.config.get("chat_allowlist", []) or [])` |
| `docs/development/plugins.md` | ctx 方法表（`ctx.send_text` 那一行附近，约 `:100-102`） | 补一行 `ctx.chat_allowed(chat_id)`：后台主动发消息前必须先查 |

框架测试：现有直接构造 `PluginContext` 的测试有 `tests/test_mentions.py`、`tests/test_image_sdk.py`、`tests/test_text_pacing.py`、`tests/test_management_integration.py` 等（共 10 个文件，见 `grep -rln "PluginContext(" tests`），新参数是可选关键字，它们不用改；构造 `PluginManager` 的有 `tests/test_management_integration.py`、`tests/test_media_anti_recall.py`。新增断言建议放 `tests/test_management_integration.py`：① 不注入回调 → `chat_allowed("x@chatroom")` 为真；② 注入 `lambda: []` → 真；③ 注入 `lambda: ["a@chatroom"]` → `a` 真、`b` 假、`""` 假；④ 回调抛错 → 假；⑤ 经 `PluginManager` 装载后改 `manager.config["chat_allowlist"]`，同一个 context 立刻看到新值。

#### 4.2.4 五个动作

**① 改名巡检 `run_rename`**

- 到期：`watch_on(conn, room)` 为真，且 `is_due_daily(kv[room, 'sched.last.rename'], today, 本地时分, (10, 0))` 且本地小时 < 22。窗口外不播（不在半夜发群消息），不补发；改名会累积到下一次巡检里一起报，不丢。
- 输入：`ctx.group_members(room)`（经 `_member_rows` 的同款解析，`groupstat.py:286-308`）。返回 `None`（缓存未就绪，`docs/design/playhub.md` 第 4 批踩坑①）或抛 `RuntimeError("微信尚未连接")`（`sdk.py:268-269` 的 `_manage`）→ **本群本 tick 跳过、不写 last_run**，下个 tick 再试。去掉 `uid == account`（机器人自己）。
- **比对基线改为快照 kv，不用 `member_seen.nickname`**。原因：前台每条人话都会经 `activity.record` 把 `member_seen.nickname` 覆写成消息里的 `sender_wechat_name`（`core/activity.py:52-58` 的 `CASE WHEN excluded.nickname <> '' THEN excluded.nickname`；昵称来源见 `engines/__init__.py:79-83`），而快照行取的是群昵称优先（`groupstat.py:301` 的 `row.get("nickname") or row.get("wechat_name")`）。两边口径不同：设了群昵称的人每发一句话，`member_seen` 就被改回微信名，次日巡检又会报一次「微信名 → 群昵称」，天天误报；反过来，改了名又说过话的人会被静默吞掉。基线 `kv(room, 'groupstat.rename_snapshot')` 存 `{uid: 名字}` JSON，只由巡检（后台与 `改名 现在`）写。
- 首次（快照 kv 不存在）：只建基线，不播报，写 last_run（**用户已接受，2026-11-06**：改名默认开上线后，已有群第一次巡检只建快照，第一天不会有任何改名播报，也不会上线就刷屏；`改名 现在` 在无基线时同样只建基线并回「已建立改名基线，下次起开始比对」）。
- 事务内：读基线 → `diff_names(prev, rows)`（只报两边都非空且不同的；新进群的人只进基线不报；不在新快照里的人从基线移除）→ 每处变化 `activity.record_nickname_change(...)`（写 `nickname_log` 并同步 `member_seen`）→ 写新基线 → 写 `sched.last.rename = today` → 写一行 `audit(action='sched:rename', result='ok', detail={"changes": n})`。提交。
- 事务外发消息：`changes` 非空时 `ctx.send_text(room, text)`（`sdk.py:512`）。文案用 `render.result("", "🔍 改名播报", rows, at_sender=False)`，前 10 处逐条 `旧 → 新`，超出写「还有 N 处没列出来」，`render.limit` 兜长度。
- 失败处理：发送抛异常或返回 `False` → 记 warning + 补一行 `audit(action='sched:rename', result='failed', detail={"error": 类名})`，**不重试**。理由：`send_text` 分段发送时可能前段已发出（`sdk.py:144` 的「前面部分可能已发送」），重试会重复刷屏；漏一条改名播报代价很低，`nickname_log` 里有记录。这是「至多一次」语义，有意为之。
- 微信连接切换/未登录：`ServiceFacade.send_text` 在未连接时抛 `RuntimeError("微信尚未连接")`（`sdk.py:119-120`），按上面的发送失败处理。`ctx.send_text` 不带 `expected_session`，快照与发送之间若重连，消息会走新连接；同一进程只有一个固定账号（见 4.2.3），新连接是同一账号，不会串号。
- 白名单：控制台定时任务的发送会被白名单拦（`wxbot_framework/plugins.py:46-47`），而 `ctx.send_text` 直连 `ServiceFacade`，**没有白名单检查**。因此发送前必须 `ctx.chat_allowed(room)`（4.2.3）。为假（或框架没有该方法）→ **照常比对、写 `nickname_log` 与新快照、写 last_run，只是不播报**，并记 `audit(action='sched:rename', result='skipped', detail={"why": "not_allowed", "changes": n})`。照写快照的理由：群被移回白名单后，第一次巡检只报移回之后的改名，不会一次补报一堆旧改名。白名单判定放在事务提交之后、发送之前，用的是当下的值。
- 只有本动作发消息；其余四个动作一律静默，结果只进 `audit` 与日志。

**② 关系超时收尾 `run_expire`**

- 每个 tick 对每个候选群开一个 `store.tx()`，调 `social.expire_room(conn, room, now)`（即 `SocialEngine._expire`，`engines/social.py:1249-1282`）：pending 超 24 h 置 `ended`，离婚单过 `deadline` 的结束婚姻并全额退 `DIVORCE_COST`。
- 幂等：离婚单 kv 行在同一事务里被 `_drop_divorce` 删除；退款 `operation_key=f"divorce_timeout:{pair_id}"`（`:1278`）撞 `UNIQUE (room, operation_key, posting_no)`（`schema.sql:61`）会抛 `DuplicatePosting`，整群事务回滚、记 warning、算本群完成（钱已退过）。前台惰性收尾与后台同时跑时，`BEGIN IMMEDIATE` 保证只有一方看得到那条离婚单。
- 不发消息（与现有惰性收尾一致）。

**③ 收题与清理 `run_quiz_gc` / `run_purge`**

- 收题：每个 tick 一个事务 `quiz.sweep(conn, now, release_slots=False)`。**后台不释放 `game_slot`**：前台 `start()` 先 `claim_slot`（`engines/quiz.py:239`）再进事务 `_sweep(conn, now, room=room)`（`:246`）；若后台恰好在两者之间提交并 `release_slot(room, SLOT_OWNER)`（`:478`），会把前台刚占的新局槽位放掉，接龙/看图就能插进来。槽位本身带 `ttl=ROUND_SECONDS`（300 s）会自己失效，后台不必管。
- 清 `idem`：`DELETE FROM idem WHERE rowid IN (SELECT rowid FROM idem WHERE at < ? LIMIT 5000)`，阈值 `now - 30 天`，走 `idem_by_age`（`schema.sql:323`），循环到删不满一批为止，每批一个事务、批间检查 `stopped`。保留 30 天的理由：消息级键 `<session_id>:<sequence>:<cmd>` 只需覆盖重投窗口（分钟级），事件键 `evt:<room>:<kind>:<uid>:<day>` 需要覆盖一天，本节的银箱键要覆盖一周；30 天对所有现有键都有富余，表仍很小。
- 清 `audit`：同样分批，`at < now - 180 天`（`audit_by_room`/`audit_by_action` 都以 `at` 结尾，按 `id` 删）。保留半年的理由：`audit` 是关系操作与管理操作唯一的追溯来源（`engines/social.py:258` 的「关系操作必须可追溯」），婚姻、雇佣、小黑屋纠纷常在几个月后才被翻出来；180 天一年清两次，行数上限可控。`wallet_ledger` **不清**（钱的流水永久保留）。
- 到期：`kv('*', 'sched.last.purge')` 不等于今天且本地时间 ≥ 04:00。删完写 last_run 和一行 `audit(room=NULL, action='sched:gc', detail={"idem": a, "audit": b})`。

**④ 发工资 `run_salary`**

- **谁付谁收**：雇主 = `pair.by_uid`。依据：`rel_propose` 插入时 `by_uid` 是发起人 `uid`（`engines/social.py:858-861` 的 `"VALUES (?, ?, ?, ?, 'pending', ?, ?)"`，第 5 个参数是 `uid`）；`rel_wage` 对 `uid == row["by_uid"]` 显示「<对方>：每日 N 金币」、对另一方显示「被 <by_uid> 雇佣」（`:927-930`），即发起「雇佣 @某人」的人是雇主。员工 = `_partner_of(row, by_uid)`（`:212-213`）。`low_uid/high_uid` 只是字典序排序（`:843` 的 `low, high = (uid, target) if uid <= target else (target, uid)`），与雇主身份无关。`by_uid` 为空或不在这对人里 → 跳过并记 `audit(result='failed', detail={"why": "bad_by_uid"})`。
- 金额：`int(_meta(row).get("wage", WAGE_PER_DAY))`，`WAGE_PER_DAY = 20`（`:94`）；解析失败或 ≤0 → 跳过并审计。
- 到期与天数：本地 00:05 之后；对每条 `active` 雇佣，应发日 = `[max(since 所在日 + 1, today - 2), today]` 中尚未发的日子（`SALARY_CATCHUP_DAYS = 3`）。成交当天不发，避免「雇了立刻发、发完就解雇」的刷币；停机超过 3 天的更早日子不补（当时开关和余额状态已不可知）。群开关 `switches.is_on(conn, room, "employ")`（`core/switches.py:52`）当下为关 → 整群跳过，不写 last_run，不补发关掉期间的工资。
- 每个 `(pair, day)` 一个事务：`ensure_player` 双方 → `debit(雇主, coin, wage, reason="salary_pay", operation_key=f"sched:salary:{pair_id}:{day}", posting_no=0, ref=str(pair_id))` → `credit(员工, coin, wage, reason="salary_income", 同一 operation_key, posting_no=1, ref=str(pair_id))`（`core/wallet.py:194-206`）→ `audit(action='sched:salary')`。
- **雇主余额不足**：`debit` 抛 `Insufficient`（`wallet.py:283-285`）→ 回滚、当天不发、不记欠薪、不解除雇佣、不发群消息，只写 `audit(result='failed', detail={"why": "insufficient", "day": day})`。之后的补发窗口里若雇主有钱了，会按天补上（同一 op key，仍幂等）。员工余额溢出（`WalletError`，`wallet.py:223`）同样整笔回滚并审计。
- 幂等：`DuplicatePosting` = 这天已发 → 回滚、计为已完成。整群所有 pair 都处理完后写 `kv(room, 'sched.last.salary') = today`，只用来快速跳过，正确性不依赖它。
- 一天只发一次：每个 `(pair, day)` 对应唯一的 `sched:salary:<pair_id>:<day>`，同一天任意多个 tick 或重启都只能成功一次。

**⑤ 周榜发银箱 `run_chest`**

- 到期：`clock.week_of(now)`（`core/clock.py:36`）≠ `kv(room, 'sched.last.chest')`，且（今天不是周一，或本地时间 ≥ 00:10）。只发**上一周**；停机跨过整周的话，更早那周不补（周榜奖励时效性强，补发没有意义）。
- 上周区间：取 `last_sunday_ts = now - (本地 weekday + 1) * 86400`，调 `ranking.top(conn, board="activity", room=room, period="week", now=last_sunday_ts, limit=20)`（`engines/ranking.py:86`）；`top` 内部走 `clock.range_of("week", now)`（`ranking.py:142`、`clock.py:55-56`），对上周日的时间戳正好得到「上周一 ~ 上周日」。周键 `week = clock.week_of(last_sunday_ts)`。
- 过滤：去掉 `moderation.is_blocked(conn, room, uid)` 为真的人与机器人自己，再取前 10；不往后递补以外的规则，空榜什么也不发。
- 一个群一个事务：`INSERT INTO idem (key, scope, at) VALUES (?, 'sched.chest', ?)`，`key = f"sched:chest:{room}:{week}"`（**不**用 `OR IGNORE`；撞主键 `IntegrityError` = 这周已发 → 回滚并写 last_run）→ 每人 `ensure_player` + `items.add(conn, room, uid, items.CHEST_SILVER, 1)`（`core/items.py:79`、`:192`）→ `kv(room, 'sched.last.chest') = clock.week_of(now)`（**本周**键，与下行的到期判断同一口径；不是要发的那个 `week`，否则永远判不出「本周已发」）→ `audit(action='sched:chest', detail={"week": week, "uids": [...]})`。10 人要么全发要么全不发。
- 不在群里播报（定案）。获奖者开背包或 `开宝箱` 时自然看到。

#### 4.2.5 幂等键汇总（问题 D）

| 动作 | 正确性来源（必须） | 快速跳过（可丢） |
|---|---|---|
| 改名 | 同一事务写 `nickname_log` + 新快照：再巡检时基线已是新名字，不会重报 | `kv(room,'sched.last.rename')=YYYY-MM-DD` |
| 超时收尾 | 离婚单 kv 行同事务删除；退款 `divorce_timeout:<pair_id>` posting 0 | 无（每 tick 跑） |
| 收题 | `closed_at IS NULL` 条件更新 | 无 |
| 清理 | `DELETE ... WHERE at < ?` 天然幂等 | `kv('*','sched.last.purge')=YYYY-MM-DD` |
| 工资 | `sched:salary:<pair_id>:<YYYY-MM-DD>`，posting 0 = 雇主扣、1 = 员工收 | `kv(room,'sched.last.salary')=YYYY-MM-DD` |
| 银箱 | `idem.key = sched:chest:<room>:<YYYY-Www>`，与发箱同事务 | `kv(room,'sched.last.chest')=YYYY-Www`（写入的是**本周**键） |

`idem` 30 天保留期 ≥ 银箱键需要的 7 天，清理不会让一周内的重发变得可能。

#### 4.2.6 节奏与补发（问题 E）

- 首次延迟 `300 + rng.uniform(0, 60)` 秒（growth 用 600 s；这里更短是因为改名窗口与工资都按天判，晚 5 分钟无所谓，但要给微信连接和群成员缓存留时间）。之后每 600 s 一个 tick，`stopped.wait(TICK_SECONDS)` 返回真即退出。
- 所有「今天」「本周」「本地时分」都由 `clock` 算（`clock.day_of`、`clock.week_of`、`clock._local` 同口径的 `datetime.fromtimestamp(now, clock.CN)`），SQL 里不出现 `date('now')`（`clock.py:3-5`）。`last_run` 存 kv，跨重启有效。
- 补发规则：改名不补（窗口外跳过，累积到下次）；超时收尾与收题每 tick 跑，不存在补发；清理只跑一次当天；工资补最多 3 天（含今天）；银箱只补上一周。
- tick 内顺序：gc（收题）→ expire → salary → chest → purge → rename。发消息的 rename 放最后，前面的写库都不依赖微信连接。

#### 4.2.7 停线程、异常与日志（问题 F）

- `on_unload`：`self._scheduler.stop()` → `stopped.set()` 后 `join(10)`；超时只记 warning（线程是 daemon，进程退出时不会卡住）。线程在每个群、每批删除、每份合同、每个补发日之间检查 `stopped.is_set()`；被打断时不写 `sched.last.*`（`sched.last.salary`、`sched.last.purge` 都只在**跑完**时写），剩下的留给下一次启动——已发过的工资会撞幂等键，不会重复付钱。最长退出时间约为一个小事务 + 一次 `send_text` 的文本等待。后台连接由线程自己在 `finally` 里关闭，`on_unload` 不碰它。框架在插件线程退出时调用 `on_unload`（`wxbot_framework/plugins.py:110-111`）；热重载期间新旧两份实例可能短暂各有一个后台线程，所有写都幂等，最多重复一次无害的读。
- 异常收口三层：① 每个「群 × 动作」包一层 `try/except Exception`，记 `logger.exception("playhub 后台 %s 失败：room=%s", action, room)`，`TickReport.errors += 1`，不写 last_run，下个 tick 重来；② `tick()` 外层再包一层，保证循环不死；③ `start()` 里账号/开库失败只记一次 warning 并在下个 tick 重试。
- 日志：logger 名 `Plugin.playhub`（即 `ctx.logger`，`sdk.py:479`）；每个 tick 只在 `TickReport` 有非零项时记一条 info 汇总，空 tick 不记；改名播报失败、余额不足这类业务失败记 info/warning 并进 `audit`（`action` 前缀统一 `sched:`，与 `schema.sql:330` 的注释一致），`detail` 只写结构化字段，不写消息正文与昵称以外的聊天内容。

#### 4.2.8 测试要点（`tests/test_playhub_scheduler.py`，全部离线）

用临时目录建 `Store`，注入 `now_fn`（固定时间戳，北京时间）、`rng=random.Random(0)`、假 `ctx`（`members.account`、`group_members(room)` 返回值/`None`/抛 `RuntimeError`、`send_text` 记录调用或抛错），直接调 `Scheduler.tick(now)` 或各 `run_*` 函数，不起线程。

1. 改名：`kv` 无 `groupstat.rename_watch` 时视为开；`ctx.chat_allowed(room)` 为假时快照与 `nickname_log` 照写、`send_text` 0 次、`audit.result='skipped'`，随后改为真且无新改名 → 仍不发；假 `ctx` 没有 `chat_allowed` 属性 → 不发；值 `"0"` 时不巡检；首次只建快照不发消息；第二次快照改了一个名 → `nickname_log` +1、`send_text` 恰好 1 次且文本含「旧 → 新」；同一天再 tick → 不再发；`group_members` 返回 `None` → 不发、不写 last_run、快照不变；抛 `RuntimeError` 同上；本地 23:00 不发、次日 10:00 发且包含前一天的变化；`send_text` 抛错 → `audit.result='failed'`，快照已更新，不重试；前台写 `member_seen.nickname = 微信名` 后再 tick，不产生误报；机器人自己的 uid 不报。
2. 超时收尾：造一条 `deadline` 已过的离婚单 → tick 后婚姻 `ended`、发起人退 `DIVORCE_COST`、kv 行消失；再 tick 余额不变；先调前台 `_expire` 再调后台，退款只有一笔。
3. 收题：过期对局 `closed_at = expires_at`；后台 sweep 后 `game_slot` 里前台占的槽仍在（`release_slots=False`）。
4. 清理：`idem` 31 天前的删、29 天前的留；`audit` 181 天前的删、179 天前的留；造 12000 行验证分批（每事务 ≤5000）；`wallet_ledger` 行数不变；同一天第二次 tick 不再执行。
5. 工资：雇主 = `by_uid`（low/high 两种排序都测）；成交当天不发，次日发 20，雇主 −20、员工 +20，两条分录同一 `operation_key`、posting 0/1；同日多次 tick 只发一次；`meta.wage=50` 按 50 发；雇主余额不足 → 双方余额不变、`audit` 记 `insufficient`、雇佣仍 active；次日雇主有钱 → 补发前一天和当天；停机 5 天后只补最近 3 天；`feature.employ` 未设或为 `0` → 不发；`by_uid` 为空 → 跳过并审计。
6. 银箱：周一 00:05 不发、00:10 后发；上周前 10 各 +1 `chest_silver`，第 11 名没有；本周的发言不计入；黑名单用户被剔除；同周再 tick 或删掉 `sched.last.chest` 后再 tick 都不重发（`idem` 拦）；周三首次启动仍补上周；停机两周只发最近一周；`send_text` 调用次数为 0。
7. 线程：`start()` 后立刻 `stop()` 能在 1 s 内返回真；`tick` 内某个群抛异常，其它群照常处理且 `errors == 1`；账号为空时 `tick` 不开库、不抛异常。
8. 并发：两个 `Store` 连同一库，一个持 `BEGIN IMMEDIATE` 不放，后台 tick 在 3 s 后放弃该群并不写 last_run，释放后下一 tick 完成。

#### 4.2.9 钱包写序修复（交 builder，与 §4.2 同批）

**问题**：`credit()`/`debit()` 先 `UPDATE wallet` 改余额（`core/wallet.py:215-219`、`:273-277`），再 `_write_ledger` 写分录（`:226`、`:288`）。分录撞 `UNIQUE (room, operation_key, posting_no)`（`schema.sql:61`）时抛 `DuplicatePosting`，但余额已经改了。调用方在 `with store.tx()` 里接住它并 `return`（例：`engines/social.py:1072-1074` 的 `except DuplicatePosting:` → `return self._no("这次送礼已经记过账了。", ...)`），事务正常提交，结果是**余额变了，却没有流水**。同类接住点还有 `social.py:657/694/791/1199`、`daily.py:444/515/631`、`quiz.py:443`、`lottery.py:155/212/321`，逐个改调用方不现实。

**定案：先按 UNIQUE 查重，再在 `SAVEPOINT` 里做「UPDATE 余额 + 写分录」。**

```python
def _posting_exists(conn, room: str, operation_key: str, posting_no: int) -> bool
    # SELECT 1 FROM wallet_ledger WHERE room = ? AND operation_key = ? AND posting_no = ?（走 UNIQUE 索引）

# credit / debit 的新顺序（签名不变，core/wallet.py:194-206、:250-262）：
#  1. 参数校验（不变）
#  2. if _posting_exists(...): raise DuplicatePosting(...)      ← 余额一分没动
#  3. conn.execute("SAVEPOINT wallet_posting")
#  4. 条件 UPDATE（不变：credit 的上限判断、debit 的 balance >= ?）→ rowcount 0 的分支照旧抛
#  5. after = _require_balance(...)；_write_ledger(..., balance_after=after)
#  6. 成功：RELEASE wallet_posting；任何异常：ROLLBACK TO wallet_posting; RELEASE wallet_posting; 原样上抛
```

- 结果：抛 `DuplicatePosting`（以及 `Insufficient`、`WalletError`、`WalletUninitialized`）时，本次调用对 `wallet` 和 `wallet_ledger` **都没有改动**；调用方接住异常后提交事务，也提交不出半笔账。调用方代码一行不改。
- `balance_after` 仍然取 UPDATE 之后读到的真实余额（第 5 步），计算方式不变，`CHECK (balance_after >= 0)`（`schema.sql:53-54`）照旧成立。
- 为什么查重够用：钱包调用都在 `store.tx()` 的 `BEGIN IMMEDIATE` 里（`core/store.py:107` 的 `conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")`；`immediate=False` 的调用点 `app.py`/`moderation.py`/`lounge.py`/`groupstat.py` 都不调钱包），查重到插入之间不会有别的连接插进同键。`_write_ledger` 里 UNIQUE 的 `except` 保留作兜底，此时 SAVEPOINT 负责把余额回滚。
- `SAVEPOINT` 与 `isolation_level=None`（`store.py:33-37`）兼容，嵌在外层 `BEGIN` 里只回滚到保存点，不影响调用方同事务里此前的写（比如 `items.take`）。
- 不选的方案：
  - 「先插分录、再 UPDATE、最后回填 `ledger.balance_after`」：插入时 `balance_after` 还不知道，`CHECK >= 0` 逼着先填占位值，再多一次 UPDATE，分录就不再是只追加。
  - 「先在 Python 里读余额、算好 `after` 再插分录」：把 debit 的正确性从条件 UPDATE（`wallet.py:265-266` 的注释「条件更新是唯一的正确性来源」）挪到了读后写，和现有的设计约束冲突。
  - 只查重、不加 SAVEPOINT：查重之后的其它失败（比如 `_write_ledger` 抛 `WalletError`）被调用方吞掉时，仍会留下半笔账。
- 测试（加到现有钱包测试文件）：① 同一 `(room, op, posting)` 第二次 `credit` → 抛 `DuplicatePosting`，余额与分录数都不变；② `debit` 同上；③ 在同一个 `store.tx()` 里接住 `DuplicatePosting` 后提交，余额等于第一次之后的值；④ 让 `_write_ledger` 抛 `WalletError`（monkeypatch），接住后提交 → 余额不变；⑤ 送礼重放（`social.py:1072` 路径）后金币余额只扣一次。

#### 4.2.10 消息级幂等（`idem` 表落地，交 builder）

**框架给的消息唯一标识**（核实结果）：

- `Message.sequence: int = 0`（`wxbot_framework/sdk.py:34`），来自原生消息：`native/text_service.py:383` 的 `body, sequence=message.sequence, msg_type=message.type,` → `text.py:114` 放进 data → `sdk.py:65` 的 `sequence=snapshot.get("sequence", 0)`。
- `Message.session_id: str = ""`（`sdk.py:35`），由 `Message.from_native(cls, protocol, data, session_id)`（`sdk.py:48`）传入，是**本次微信连接**的会话号，重连后会变。
- 框架自己就把 `(session_id, sequence)` 当唯一键：`wxbot_framework/members.py:202-203` 的 `CREATE UNIQUE INDEX IF NOT EXISTS messages_by_sequence` / `ON messages(account, session_id, sequence) WHERE sequence > 0;`。
- `data["native_message_id"]`（`native/text_service.py:388` 的 `data['native_message_id'] = str(getattr(message, 'native_message_id', 0) or '')`）：微信服务端消息号，跨重连稳定，防撤回/媒体撤回已用它当消息身份。**作主键**（用户 2026-11-06 定）。

**键与位置**

- 键（用户 2026-11-06 定）：① `native_message_id` 非空且不是 `"0"` → `f"msg:{msg.chat_id}:{native_message_id}:{cmd.action}"`；② 否则 `session_id` 非空且 `sequence > 0` → `f"{msg.session_id}:{msg.sequence}:{cmd.action}"`（与 `schema.sql` 里 `idem` 表注释 `'<session_id>:<sequence>:<cmd>'` 一致）；③ 两者都没有 → 不生成键，**不做幂等**，照常执行。`scope = cmd.action`，`at = now`。`native_message_id` 先 `str(...).strip()`。
- 位置：放在 `App.handle_message` 里、`dispatch(...)` 之前（`plugins/playhub/app.py:171-172`），也就是所有静默分支（小黑屋、未绑定、开关关、说明书卡）之后。不放在 `Plugin.on_message`（`plugins/playhub/__init__.py:95-118`），因为那里还没 `route()`，拿不到 `action`；而且被静默的消息不该占 `idem` 行。
- 纯文本（`cmd is None`，`app.py:129-133`：记活跃 + 竞猜抢答）**不做**消息级幂等：每条闲聊都写一行太多；抢答发奖已由分录 UNIQUE 防重（`quiz.py:443` 的 `except DuplicatePosting:` 注释「同一局重放：钱已发过，不再计次」），重投只会多记一条活跃，影响很小。
- 新文件 `plugins/playhub/core/idem.py`：

```python
def message_key(msg, action: str) -> str | None        # 按上面 ①②③ 顺序取键；③ 返回 None
def claim(store: Store, key: str, *, scope: str, now: int) -> bool
    # with store.tx() as conn: conn.execute("INSERT INTO idem (key, scope, at) VALUES (?, ?, ?)", ...)
    # IntegrityError（主键冲突）→ False；成功 → True。不用 INSERT OR IGNORE，和 wallet.py:178 同一原则。
```

**claim 与业务：独立事务，先 claim 再执行**

- 只能独立：引擎各自开 `store.tx()`（如 `engines/social.py:480`），连接上已在事务中时再 `BEGIN` 会报错；要合成一个事务，就得改所有引擎的事务边界。
- 流程：`key = message_key(msg, cmd.action)` → 有键且 `claim(...)` 为 `False` → **直接返回 `None`**（重复投递静默，不回复，只记 debug 日志）→ 否则 `dispatch`。
- **执行失败不释放 claim**（至多一次，用户 2026-11-06 已接受）。理由：引擎提交事务之后、返回 `Reply` 之前也可能抛异常，这时业务已经生效；而不少操作键带时间戳（例：送礼 `f"gift:{uid}:{target}:{gift.item_id}:{now}"`），重放时分录 UNIQUE 拦不住。释放 claim 会让重投的消息再执行一遍。失败的那条用户已经收到 `ERROR_TEXT`（`app.py:180`），重发一次就是新的 `sequence`、新的键。
- `claim` 自身失败（`database is locked` 等 `sqlite3.Error`）→ 不执行、返回 `None`、记 warning（失败关闭；库忙到 3 s 拿不到锁时，引擎事务大概率也会失败）。
- 回包发送失败（`Plugin._send`）同样不释放。
- 跨重连：主键 `native_message_id` 跨重连稳定，重连后的重复投递也能拦住；只有退回 ② 的消息（没有服务端消息号）在重连后拦不住，由各引擎的分录与状态检查兜底。
- 清理：30 天保留，由 §4.2.4 ③ 的 `run_purge` 删。

**测试**（`tests/test_playhub_idem.py`，离线，用假 `Message`）：① 同一 `native_message_id` 的「签到」投两次 → 金币只加一次，第二次 `handle_message` 返回 `None`，键为 `msg:<room>:<id>:daily.signin` 形态；② 同一 `native_message_id`、不同 `session_id/sequence`（模拟重连重投）→ 仍只执行一次；③ `native_message_id` 为 `""` 或 `"0"` 时退回 `<session_id>:<sequence>:<action>`，同 `session_id/sequence` 投两次只执行一次；④ 两者都没有（空 id、`sequence=0`）投两次 → 都执行（幂等关闭）；⑤ 两个群里相同 `native_message_id` 的键不同（键含 room）；⑥ 引擎抛异常 → 返回 `ERROR_TEXT`，`idem` 行仍在，重投返回 `None`；⑦ 被小黑屋静默、未绑定、开关关的消息不写 `idem`；⑧ 纯文本不写 `idem`；⑨ 另开一条连接持有写锁时 `claim` 超时 → 返回 `None`、引擎未被调用；⑩ `idem` 行的 `scope` 等于 action、`at` 等于注入的 now。

### 4.3 高误触词治理

无前缀方案的唯一新增风险就是**误触**。判定方法：把词放进真实群聊语料里，问「这句话会不会作为一句正常聊天被整条发出来」。结论是 **9 个高风险 + 3 个中风险**，其余 49 个词（主词+别名）判为低（`帮助` 属低风险）。

复核结论与用户判断的差异：**「兄弟」比用户列的其它词更危险**（"叫一声兄弟"是群聊高频整句），但它同时是**语义最不该用这个词的功能**（"兄弟"是称呼，不是动作）——所以最终处理不是加约束，而是**换词**：`兄弟` **不注册**，改用 `结拜`。用户列的其他 8 个（宝宝/礼物/天气/配对/结婚/闺蜜/纳妾/雇佣）核实成立。另核出用户未列的 `星座`/`文案`/`聊天` 为中风险（不做处理会每次都被回一句，做 `guide` 处理只回一次）。

| 触发词 | 风险来源（真实群聊里的样子） | 采用的方案 | 最终形态 | UX 代价 |
|---|---|---|---|---|
| `宝宝` | 群友互称「宝宝」、哄人「宝宝」 | **强制子命令**。`needs_arg=True` + `bare_behavior=silent`；只接受 `看`/`喂养`/`改名 X`/`超极` 四个子命令，其它参数回一句用法 | `宝宝 看`、`宝宝 喂养` | 最重的一条：**裸发「宝宝」毫无反应**，新人必须看帮助才知道要带子命令；`超极宝宝` 也从独立命令降级为 `宝宝 超极`（顺带解决"四字中文会被接龙吞"）。**v1 只交付这四个入口的"基础版"**，`超极` 仅返回"v2 开放"说明，**不执行升级、不扣费** |
| `礼物` | 「谢谢你的礼物」 | **强制 `@对象 + 物品`**，缺任一即回用法；`silent` | `礼物 @张三 玫瑰` | 参数长（艾特要选人再打字），但送礼本来就是"给具体的人送具体的东西"，语义上不算损失 |
| `配对` | 「这两个人很配对」 | **强制艾特**，裸发 `silent` | `配对 @张三` | 不能"测测今天的缘分"了；别名 `缘分` 同规则，也必须带人 |
| `结婚` | 「他俩结婚了」 | **强制参数**（`@对象` 或子命令）+ **群级默认关闭** | `结婚 @张三` → 对方 `结婚 同意`；子命令 `我的婚姻` / `度蜜月` / `申请离婚` / `婚姻统计` / `清求婚次数` | 双重门槛：群主不开就没这功能；开了也必带参数。**不注册「离婚」顶层词**，离婚是 `结婚 申请离婚` 的状态机（200 金币 + 对方 24h 内确认 + 超时全额退费，见 §4.4）；求婚每日 3 次，清求婚次数 15 钻石 |
| `闺蜜` | 「我闺蜜今天…」 | 同 `结婚`（强制参数 + 默认关闭） | `闺蜜 @张三` | 同 `结婚` |
| `纳妾` | 「古装剧里纳妾」 | **默认永久关闭**（`kv` 开关，且不提供群级一键开启，只能控制台改配置） | `纳妾 @张三` | 基本等于不提供该玩法；这是合规与舆情取舍（§8.1），不是技术取舍。**v1 不开放该玩法、无任何扣款入口**；配置里的 30 钻石是**准入门槛**（余额校验），不是扣款（口径见 §4.4 数值表） |
| `雇佣` | 「公司雇佣了他」 | **强制参数** + 默认关闭 | `雇佣 @张三` → 对方 `雇佣 同意` | 同 `结婚` |
| `结拜` | 原词 `兄弟` 极高危 | **换词**（`兄弟` 不注册）+ 强制参数 + 默认关闭 | `结拜 @张三` → 对方 `结拜 同意` | 用户若习惯说「兄弟」，会一次都触发不了；帮助文案里必须写「结拜＝原兄弟」 |
| `天气` | 「今天天气不错」 | **强制城市参数** + 群级默认关闭 + 10 min 缓存 | `天气 北京` | 「今天天气不错」正好是**无参数**形态，强制参数即化解；代价是必须带城市（不能"按 IP 猜"） |
| `星座` | 「你什么星座」 | **中风险：`bare_behavior=guide`**，每群每词 24 h 只回一次 | `星座 狮子` 或 `星座`（取自己） | 裸发会有**一次**引导回复（"用法：星座 狮子"），之后 24 h 静默；同一句话被回一次是可接受噪音 |
| `文案` | 「这段文案写得好」 | 同上 `guide` | `文案 表白` | 同上 |
| `聊天` | 「我们在聊天」 | 同上 `guide` | `聊天 开` / `聊天 关` | 同上；`聊天` 语义上等于"总开关"，最容易与日常用语撞，`guide` 是唯一不损失功能的选择 |

**为什么不用「必须艾特机器人」作为通用门槛**：群聊里艾特机器人是"求助"语义（会同时唤醒 `mention_chat` 的智能聊天），要求每条命令都艾特，等于把 47 项变成"跟机器人对话"，与"群里直接发签到"的产品目标冲突。艾特只能作为**少数高危词的附加条件**（`配对`/`礼物`/关系类），不能当全局规则。

**为什么不用「群里首次使用需群主授权」**：v1 的权限模型是**一层门禁**——只区分"管理动作"与"玩家玩法"，管理动作放行 `owner`/`admin`，其余一律拒绝（§2.4 权限契约）。给每个玩法再加一级"群主授权"要新增 DDL 与面板，收益不抵成本；高危项已用"默认关闭 + 强制参数"覆盖。**也不存在"另建一套角色权限体系"的必要**：群主/管理员的事实来源是 `ctx.group_member_role()`，自建名单只会与真实角色漂移。

#### 4.3.1 五个配置开关（v1 口径，2026-11-05 定）

**只有 5 个开关，且只在配置里改，不做群内命令**（不注册「开启XX」这类词）。理由是**「参数形状」本身就是最好的开关**：高危项全是 `needs_arg=True` + `bare_behavior=silent`，裸发一个字都不回，再叠一层"开启/关闭"命令只是把复杂度搬家——它自己又是一个会误触的关键词，还得配权限门禁。

| 开关 | 键（`kv`，按群） | 覆盖动作 | 默认 | 备注 |
|---|---|---|---|---|
| 结婚 | `feature.marriage` | `social.marry`（结婚/求婚/我的婚姻/度蜜月/申请离婚/婚姻统计/清求婚次数） | **关** | 涉及 520 / 200 金币支出 |
| 闺蜜与结拜 | `feature.kinship` | `social.sister`、`social.brother` | **关** | 关系类，语义敏感 |
| 雇佣 | `feature.employ` | `social.employ` | **关** | 同上 |
| 纳妾 | `feature.concubine` | `social.concubine` | **关** | v1 无扣款入口（30 钻只是准入门槛） |
| 宝宝 | `feature.baby` | `baby.view` | **关** | v1 只交付基础版 |

其余 42 项**默认全开**（签到/抽签/猜 x/盲盒/黄历/星座/求财神/潜水/邀请/关系卡/群空间/点歌/找茬/问答…——它们没有副作用，或已由参数形状把关）。

实现要点（`plugins/playhub/core/switches.py`）：`kv_key(name) -> "feature.<name>"`；`is_on()` **读不到按关、读失败按关**（失败关闭，与权限门禁同一口径）。它与引擎级总闸 `feature.<engine>`（`app._feature_off`，读不到按**开**）是两层，互不替代：前者是"这个群要不要这个敏感玩法"，后者是"整个引擎临时故障停机"。

误触治理不靠开关数量，靠三条既有机制：整条消息 `strip()` 后**精确等于**关键词、`bare_behavior="silent"`、以及"50 条日常闲聊语料零回复"的验收（第 4 批验收第 7 条）。

### 4.4 数值表（v3 已定锚点）

原则：**金币靠固定产出，钻石靠稀有掉落**；每新增一个金币来源都必须同时指定它的 sink，否则只加产出会通胀。下表是 v3 认可的全部数值锚，实现与测试以本表为准；**凡新增钻石消耗必须先在表里校验**（产出总量 ≈ 5.5 钻/周，单项消耗超出这个量级等于死内容）。

**金币产出（全部为固定值，无随机金币）**

| 来源 | 金币 | 频率 |
|---|---|---|
| `签到` | `30 + 5 × min(连签天数-1, 10)`，即 30 → 80 封顶 | 每日 1 次 |
| `抽签` | 见下方签等表，0–30 | 每日 1 次 |
| `开宝箱` | 铜 50 / 银 150 / 金 400 | 按箱型 |
| 竞猜答对 | 10（本题已用过提示则 5） | 每日最多 10 次，即 ≤100/日 |
| 群内发言 | **不产金币** | — |

群内发言**明确不给币**：发言给币会奖励刷屏，且与活跃榜重复计功（活跃榜已经按消息数排名）。

**竞猜奖励为什么是 10/5 且封日上限**（第 2 批补，原表漏写）：答对一次 = 两个签等的量级，与「每日 1 次」的签到、抽签相比略高，因为抢答要求群里有第二个人参与、不是单人可刷；用 `提示` 后减半，避免"先点提示再答"白拿。日上限按**答对次数**（`daily_counter` 的 `quiz_win` 键）而非金币累计，`每日 10 次 × 10 金币 = 100`，与签到 80 同量级，不至于让一个人搬空金币池。**竞猜不发钻石**：钻石产出总账（≈5.5 钻/周）不为它改动；金币 sink 仍是度蜜月 520 / 离婚 200 / 礼物（§9 待定价格），本项不新增 sink。

**抽签签等表（12 份概率）**

| 签等 | 概率 | 金币 | 钻石 |
|---|---|---|---|
| 上上签 | 1/12 | 30 | +3 |
| 上吉 | 2/12 | 20 | — |
| 中吉 | 4/12 | 12 | — |
| 小吉 | 3/12 | 8 | — |
| 末吉 | 2/12 | 5 | — |

**抽签卡与解签（第 4 批改版，照小V 版式）**：签文库 `content/playhub/fortune.jsonl` 每行 `{"id","no","tier","text","jie"}`，五档各 6 条；签诗为三三四四短句，`jie` 为柳如烟口吻两三句白话。先按上表抽签等，再在同档内用 rng 随机挑一条（`_pick_entry`），同一事务把 `{"day","id"}` 写入 kv `fortune.pick.<uid>`。卡片：`@昵称` / `🙏您抽到了第N签 · 签等！` / `-` 虚线 / `签诗：…` / 奖励行 / `💰 当前金币` / `═` 双线 / `💡欲知详情可发送【解签】`。`解签`（`DailyEngine.fortune_explain`）只读当天记录，回 `-` 虚线 + `🔮解签：<jie>`；当天没抽过则提示先抽签；不发币、不限次、不占抽签次数，跨天失效；缺 `jie` 回「这一签的释义还没写好」。

**宝箱三型**

| 箱型 | 取得方式 | 开启成本 | 金币 | 钻石 |
|---|---|---|---|---|
| 铜箱 | 每日签到赠 1 个 | 免费 | 50 | 1/20 概率 +1 |
| 银箱 | 活跃榜每周前 10 名各 1 个 | 免费 | 150 | 1/12 概率 +1 |
| 金箱 | 连签第 7 天赠 1 个 | 免费 | 400 | 1/8 概率 +2 |

**钻石总账（每周期望 ≈ 5.5 钻）**

| 项 | 钻/周 | 说明 |
|---|---|---|
| 连签 | 3 | 每周第 7 天一次 |
| 上上签 | 1.75 | 1/12 × 3 钻 × 7 次 |
| 铜箱 | 0.35 | 1/20 × 1 钻 × 7 天 |
| 银箱 | 0.08 | 1/12 × 1 钻 × 每周 1 个 |
| 金箱 | 0.25 | 1/8 × 2 钻 × 每周 1 个 |
| **合计** | **≈ 5.5** | 三处正向产出之外的任何钻石来源都必须先改这张表 |

**钻石消耗（sink）**

| 用途 | 钻石 | 折合产出周期 |
|---|---|---|
| `盲盒 买次数` | 5 / 次 | ≈ 1 周 |
| `结婚 清求婚次数` | 15 | ≈ 3 周 |
| `纳妾` 准入门槛 | 30（**非扣款**，仅余额校验） | ≈ 6 周 |

清求婚次数由原定的 100 钻改为 **15 钻**：没有充值渠道、产出约 5.5 钻/周，100 钻≈5 个月，等于死内容；等比缩放到 15 钻≈3 周才有人用。清的是**当日窗口**（清零后可重新计满）；当前计数为 0 时**拒绝支付**并提示「当前无需清空」。

**已拍板的玩法数值规则（v3 确定，不再讨论）**

| 功能 | 规则 |
|---|---|
| 度蜜月 | 520 金币/次，冷却 24h；双方挂 24h「蜜月」标记（**纯展示、无收益**）；`honeymoon_count + 1`。定位是金币回收池，不返利 |
| 申请离婚 | 发起方付 200 金币；对方 24h 内回 `同意离婚` / `拒绝离婚`；超时失效并**全额退费**；**不提供单方强离** |
| 婚姻统计 | 个人维度：结婚时间、婚龄（天）、蜜月次数、求婚次数（本人发起累计）、离婚次数 |
| 求婚 | 每日 3 次，日界 Asia/Shanghai |
| 婚姻榜 | 按婚龄降序，夫妻**合并一行**：`A ❤ B · N 天`；已离婚不上榜；婚龄相同按结婚时间升序 |
| 邀请榜 | 默认**总榜**（自本群开始采集以来），并支持日/周/月；同一 `invitee` 对同一 `inviter` **终身只计 1 次**；机器人自身邀请计分；`actor_id` 缺失只进群级 |
| 盲盒次数 | 5 钻石 +1 次抽奖机会；每人每日限购 2 次；当日 `24:00` Asia/Shanghai 清零；不累积、不退还、不可转赠 |
| 礼物（第 3 批落地口径） | 内置 5 件，价格**由 `core/items.py` 定义**：玫瑰 20 / 巧克力 50 / 蛋糕 88 / 烟花 200 / 戒指 520。**背包里有就先扣背包，没有才按价目代买**；代买走的流水 `operation_key = gift:<送礼人>:<收礼人>:<物品>:<时间戳>`。v1 送礼**只记一笔账**（`kv` 的 `gifts:<low>:<high>` 计数 + `audit`），**礼物不进对方背包**——v1 没有可消费礼物的玩法，进背包等于凭空造一个死库存 |
| 配对（第 3 批落地口径） | 不花金币、不落库：`score = int(sha256("room|day|low|high")[:8], 16) % 101`，同一天同一对恒定，跨天重摇；两人顺序无关（先按 uid 排序）。分档 90/75/60/40/20/0 各一句评语 |
| 宝宝（第 3 批 v1） | 领养：**无独立子命令**，第一次 `宝宝 看` / `宝宝 喂养` 自动领一只（有婚姻则 `parent_low/parent_high` 都记，否则只记 `parent_low`），默认名 `{昵称}的宝宝`，重名追加 2/3/…。喂养：**20 金币/次，每日 3 次**（`daily_counter` 键 `baby_feed`），`care +10`（上限 100）、`exp +5`；阶段阈值 `exp ≥ 100/300/600/1000` 依次进 婴儿/幼儿/儿童/少年，**第 5 阶「超极」v2 才开放**（`宝宝 超极` 只回说明，不升级也不扣费）。改名：≤8 字、群内重名拒绝，`audit` 记 `from/to` |
| 关系卡（第 3 批落地口径） | 只读，不花钱。省略参数看自己，带 `@` 看别人；每类关系最多列 3 人，超出写「等 N 人」；只有一行「未婚」时尾注「这张卡几乎是空的」 |
| 盲盒（第 3 批落地口径） | 每日免费 **1** 次（`daily_counter` 键 `blindbox_free`），另有 `买次数` 5 钻/次、每日上限 2 次（`blindbox_buy`）；**加购额度必须真的买过才能用**——购买数与已用数分开计，否则「免费 0 / 加购 2」会凭空多两次。掉落 `玫瑰 40 / 巧克力 25 / 蛋糕 18 / 烟花 12 / 戒指 5`（权重合 100，期望 ≈82 金币 < 戒指 520，防止买次数被当成金币洗出通道），单次最多连开 10 个 |
| 开宝箱（第 3 批落地口径） | **开启免费**，不看钥匙；币值 铜 50 / 银 150 / 金 400，钻石 1/20 +1、1/12 +1、1/8 +2。供给：每日签到送铜箱、连签第 7 天那一次额外送金箱；**银箱等第 4 批活跃榜周榜前 10 的定时动作**，在那之前不会凭空发 |
| 求财神（第 3 批落地口径） | **固定 +20 金币**，一日一次（`daily_counter` 键 `wealth`），签文从 `content/playhub/wealth.jsonl` 轮转——「金币产出全部为固定值，无随机金币」这条不变 |
| 黄历 / 星座（第 3 批落地口径） | 全部本地化、不请求第三方。黄历支持 `黄历 明天` / `黄历 昨天`，内容按**日期**轮转（同一天全群同一份），文档写明「不接真实历法」；星座不给星座时用 `sha256(day, room, uid) % 12` 推定并在卡面上说明，四项指数与幸运数字/颜色同理由 `(种子, 维度)` 哈希得出，**同一天同一人不重摇** |
| 切磋（第 3 批落地口径） | 不押金币、不发金币，只记战绩（`kv` 的 `duel:wins:<uid>` / `duel:losses:<uid>`）；`同意` 后 `rng.random() < 0.5` 定胜负；挑战 10 分钟内有效，同一个人同时只能有一场未完的挑战 |
| 点歌（第 3 批落地口径） | 只登记队列（`kv` 的 `music:queue`，上限 **5** 首）并报排位，**不发音频**；每人 30 秒冷却（`music:cd:<uid>`）；队列在内存级的 `kv` 里，机器人重启即清空 |
| 文案（第 3 批落地口径） | 主题 日常/情话/挽尊/打气/傲娇/沙雕，游标按主题分别存 `kv`（`copy:cursor:<主题>`）；**游标先自增再取模**，所以每组第一次拿到的是第 2 条，随后顺序轮完再回到第 1 条——语义是「再发一次换下一条」，不是「从第一条开始」 |
| 群空间（第 3 批落地口径） | 只读聚合（成员档案/在册关系/宝宝/群内金币与钻石总量/背包件数），缓存键按**整点**（`space:<YYYY-MM-DDTHH>`），每小时最多重算一次；写缓存用独立事务，不在只读事务里升级 |
| 天气（第 3 批落地口径） | `adapters/outbound.WeatherProvider` 协议 + 默认 `OfflineProvider`（恒返回 `None`）：**没接服务就明说接不上，不编预报**；城市必填且 ≤12 字，缓存 10 分钟，失败不写缓存。**2026-11-06 选型定案：和风天气 QWeather v7**（`QWeatherProvider`，两跳：城市名 → LocationID → 3d 预报）。配置在 `bot_config.json → plugins.playhub`：`weather_provider: "qweather"` + `weather_api_key` + **`weather_api_host`（必填，形如 `abc123.re.qweatherapi.com`）**，可选 `weather_geo_host`、`weather_timeout_seconds`（默认 3，夹在 `0 < t ≤ 10`）。**2026-11-06 实测：旧公共域名 `devapi.qweather.com` 对新 key 回 404/403，必须用账号专属 API Host**；一个域名同时服务 geo 与 weather，只填 `weather_api_host` 时 geo 自动跟随。另实测该域名**无视 `Accept-Encoding: identity` 直接回 gzip**，传输层因此解压（`_maybe_gunzip`，解压后仍受 64 KiB 上限约束，防 gzip 炸弹）——不解压的症状是「key 正确但天气永远不可用」；缺 key / 名字不认识 / 值非法一律退回离线（失败关闭）。任何失败（网络、超时、码值非 200、JSON 坏、超长、主机名含 `/`/空格/`:`）都返回 `None` 不抛；**连续 3 次失败熔断 5 分钟**（`FAIL_THRESHOLD`/`BREAKER_SECONDS`），「查无此城」（码 404）不计入失败。**执行模型（2026-11-06）**：两跳 HTTP 走 `adapters/executor.py` 的 worker，回调线程不阻塞、也不回「正在查」，结果由 worker 直接发卡；worker 不碰 `Store`，缓存用 `defer` 交回插件线程、在下一条消息开头的 `flush()` 落库（所以「刚查完立刻再问」可能还没命中缓存）；执行器队列满时退化同步查询，宁可慢不丢查询 |
| 活跃榜（第 4 批落地口径） | 默认**本周**（`DEFAULT_PERIOD="week"`），参数词 `日/今日/今天`、`周/本周/这周`、`月/本月/这个月`、`总/总榜/全部`；区间由 `clock.range_of()` 算，**上界一律取「今天」**而不是周末/月末（未来没有数据，少扫空区间）；周的起点是 `today - timedelta(days=today.weekday())`（周一）；单位是「条」= `activity_daily.messages` 求和，同一句只记一次（命令消息不记） |
| 潜水（第 4 批落地口径） | 天数默认 7、上限 90；判据是 `activity_daily.last_at` 距今天数 ≥ 天数；读的是 `member_seen` 的昵称快照；**只统计、不清人**（框架无踢人能力，`:1743` 的软禁言同理） |
| 邀请（第 4 批落地口径） | 窗口 `[day_of(now - days*86400), day_of(now)]`，默认 7 天、上限 90；按人榜读 `invite_credit`，群级读 `invite_daily`；**同一个 invitee 对同一个 inviter 终身只算 1 次**（`invite_credit` 主键 `(room, inviter, invitee, day)`，跨天去重在查询侧做）。邀请人来自框架 `GroupMemberEvent.actor_id`（C1 已落地，见 §8.7）；`source == "membership"` 的名单差分补录**不产生归属**（`core/invite.py`）。计数口径：凡带成员 ID 的 join/leave 进 `invite_daily`，按 `evt:<room>:<kind>:<uid>:<day>` 幂等去重，重投的系统消息不会把人数记两遍。卡面措辞：有归属时「🚪 入群 N 次 · 邀请入群 N 次」，只有群级时「群级共 N 次邀请入群」，一条记录都没有时「📨 近 N 天没有入群记录」+「ℹ 邀请榜靠框架的入群事件，缺事件时这里就是空的」 |
| 改名（第 4 批落地口径） | `改名 开 / 关 / 现在`；开关存 `kv` 的 `groupstat.rename_watch`（`"1"`/`"0"`），**默认开**（kv 读不到 = 开），管理员发 `改名 关` 关闭；后台每天巡检一次并播报（§4.2.4 ①）；`现在` 走 `ctx.group_members(room)` 快照与改名基线 `groupstat.rename_snapshot` 比对（§4.2.4 ①，不再用 `member_seen`），快照返回 `None`（缓存未就绪）时明说「读不到群成员快照」，**不把 None 当空列表**；变化写 `nickname_log` 并回「发现 N 处改名」 |
| 小黑屋（第 4 批落地口径） | 软禁言：被关期间**在 `route()` 之前**就返回静默（连「黑名单 列」也不回，§8.2）；单次上限 **60 分钟**、每日上限 **10 次**（`daily_counter` 键 `jail_set`）；`jail.until` 到期由任意 `小黑屋` 子命令惰性清理（**维持惰性，不做定时巡检**，2026-11-06 定）；`小黑屋 10` 这种缺目标形态直接拒绝（`target.isdigit() and not extra`）；列表卡标题带人数 |
| 黑名单（第 4 批落地口径） | `scope='play'`，只在本插件内生效；被拉黑者与小黑屋同等待遇（`route()` 之前静默）；`加/删/列` 里 `列` 不需权限，`加/删` 需 owner/admin；`加` 用 `ON CONFLICT(room,uid,scope) DO UPDATE` 幂等，`删` 只删 `scope='play'` |
| 门禁的两种失败方向（第 4 批落地口径） | **权限判定失败关闭**（`MemberFeed.role` 返回 `unknown` / 抛异常 → `permissions.gate` 拒绝，记 `audit.result='denied'`）；**静默判定失败开放**（`_muted()` 读库异常按「不在小黑屋/黑名单」，否则库一抖全群静音）。两者方向相反是有意的，写测试时不要统一 |

**金币 sink 汇总**：度蜜月 520、离婚 200、礼物 20–520（按件计）、宝宝喂养 20/次（日 3 次，日上限 60）。盲盒次数是钻石 sink，不消耗金币。

**礼物价格定表依据**（第 3 批补，原表写「见 §9 待定」）：产出侧签到 30–80/日、抽签 0–30/日、竞猜 10×10/日，即活跃玩家日入约 100–200 金币；因此把礼物定成 **20（随手）/ 50（日常）/ 88（有点心意）/ 200（郑重）/ 520（与度蜜月同档，最贵的单次社交支出）** 五档，一次送礼落在半日到三日的收入之间，既不是随便刷也不是买不起。**这五件是 v1 的固定清单**，`items.seed()` 只补不改（`ON CONFLICT DO NOTHING`），后续加物品不会覆盖群里已改过的行。

### 4.5 卡面规范（对照小V 复刻）

**为什么要有**：小V 每个功能都自带一张「群内发 XX 查看」的自述卡（玩法介绍 + 真实聊天截图），
群里新人不用记文档、管理员不用口头教。43 个玩法挤在一条 `帮助` 里的形态被否决。

**模块划分**

| 位置 | 职责 |
| --- | --- |
| `plugins/playhub/core/render.py` | 排版原语：装饰头 `=====图标 名称 图标=====`、项目行 `├ 金币：+30`、当前值 `💰当前金币：130`、`⚠注：`、行数/字数双上限。纯字符串，不碰库、不碰网络 |
| `plugins/playhub/core/menus.py` | 44 张触发卡文案（与 44 个 playhub 动作一一对应，缺一张测试就红） |
| `plugins/playhub/app.py` | 决定何时发卡：裸发 + `needs_arg`/`guide` + 开关已开 → 回卡 |
| 各引擎 | 结果卡：`@昵称` → 主行 → `├` 明细 → `当前X：N` → 一句彩蛋 |

> 偏离记录：§1 文件表把「文案渲染、截断、占位符」排在 `core/textkit.py`，但仓库里
> `plugins/playhub/textkit.py` 已经是**归一化器**（`normalize` / `Mention` / 零宽处理），
> 同名会误导，故渲染落在 `core/render.py`。

**六条硬规矩**

1. 菜单卡用装饰头起头，一行一条 `图标【信号】说明`，末尾可挂 `⚠注：`；
2. 结果卡先 `@发言人`（小V 每条结果都点人），再说发生了什么；
3. 数值永远回显「当前」：只报 `+30` 不报余额，群里没法判断是否到账；
4. 图标区分「分组 / 项目 / 提示 / 彩蛋」四类，同一张卡里不混用；
5. 行数 ≤ 10、字数 ≤ 1400；超限保留头尾、中间折成 `…（略 N 行）…`（小V 的钓鱼卡有 35 行，在群里是刷屏）；
6. 卡面任何一行归一化后都**不等于**触发词（`§2.6.5` 自触发不变量，测试逐行校验）。

**裸发规则（`app.handle_message`）**

- `needs_arg` 或 `bare_behavior="guide"` → 回触发卡（对应小V 的「群内发 XX 查看」）；
- 不需要参数的（签到 / 抽签 / 13 类竞猜 / 排行榜）→ 照常执行，卡片会挡住真正的动作；
- 卡面判定排在 `feature.<engine>` 与 5 个高危开关**之后**：开关没开就静默，不泄露玩法存在。

**卡面纪律**：卡上出现的每个子命令、每个数值，都必须与引擎里的常量/分支对得上；本轮还没实现的
21 项只写「玩法一句话 + 参数形状」，末尾挂 `⏳ 这个玩法还在施工`，绝不虚构子命令——卡面说了做不到
比没有卡更糟。

**落地进度（2026-11-05）**：`render.py` / `menus.py` / app 接线 / 签到 / 抽签 / 帮助总览 /
13 类竞猜题目卡与结果卡 / 金币榜 / 婚姻与四种关系 / 关系卡 / 配对 / 礼物 / 宝宝 /
盲盒 / 开宝箱 / 黄历 / 星座 / 求财神 / 文案 / 群空间 / 聊天说明 / 切磋 / 点歌 / 天气 的结果卡 已完成，
第 4 批的活跃榜 / 潜水 / 邀请 / 改名 / 小黑屋 / 黑名单 六张卡也已在 2026-11-05 落地
（**43 张触发卡全部可用，不再有 `⏳ 施工中`**）——`social.py` 的 53 处回复
统一走 `_ok()`（`@人 → 主行 → ├ 明细 → 当前`）与 `_no()`（`❌ 一句原因 + 💡 下一步`），
失败回复不再只说「不行」，一定带下一步该发什么。

---

## 5 题库与文案数据

### 5.1 为什么不放插件目录

| 证据 | 结论 |
|---|---|
| `plugin_sources.py:82-97`：合计 ≤1 MiB、≤64 文件、单文件 ≤256 KiB | 11 类目 × 300 题 ≈ 400 KB，吃掉 40% 体积预算 |
| `plugin_sources.py:99-113`：`snapshot` 对「文件 + options」算 sha256 | 题库改一次就产生新版本，插件代码回滚会把题库一起回滚 |
| `plugin_sources.py:115-141`：保存时逐文件校验（.py 编译、.json `loads`） | 题库是 `.jsonl`（不在 `EDITABLE={'.py','.json','.md','.txt'}`，`plugin_sources.py:14`），网页改不了；`.json` 大数组每次保存都要全量解析，慢 |
| `plugin_sources.py:82-97` 只递归插件目录 | 放插件外的数据不进版本快照 → 运维可独立更新题库 |

### 5.2 存放路径与优先级

```
① <每账号工作区>/.runtime/playhub/content/<cat>.jsonl        ← 运行时覆盖（运维热改，.gitignore 已忽略 .runtime/）
② <仓库根>/content/playhub/<cat>.jsonl                        ← 随代码发布的默认题库（进 git，可 review）
③ 代码内置兜底（每个类目 5–10 条示例，仅在 ①② 都缺失时使用，保证功能不因缺文件崩溃）
① 存在则完全覆盖 ②（不合并，避免"新旧混排"难排查；合并模式留作后续可选项）
```

仓库根已有 `tools/` 目录，校验/统计脚本建议放 `tools/playhub_content.py`（薄壳，只调用 `plugins/playhub/content/validate.py`，保证校验逻辑只有一份）。

### 5.3 格式：JSON Lines

每行一个对象（**不用大 JSON 数组**：可逐行解析、坏行隔离、diff 友好、可流式加载）：

```json
{"id":"anime-0001","v":1,"q":"来自 22 世纪的蓝白机器猫","a":["哆啦A梦","机器猫","叮当猫"],"hint":["铜锣烧","四次元口袋"],"d":1,"tag":["日漫"],"src":"builtin"}
```

| 字段 | 必填 | 约束 |
|---|---|---|
| `id` | 是 | 稳定标识，`^[a-z]+-\d{4}$`，类目内唯一；**改题不改 id**（`quiz_stat` 靠它统计） |
| `q` | 是 | 题面，≤200 字；不得含答案明文（校验会做子串检查） |
| `a` | 是 | 答案数组，1–8 项，归一化后非空、互不重复 |
| `hint` | 否 | 提示数组 ≤3 项，逐级揭示 |
| `d` | 否 | 难度 1–3，默认 1 |
| `tag` | 否 | 标签数组（用于按标签出题） |
| `src` | 否 | 出处标记，便于版权追溯（`builtin`/`handmade`/URL） |

`diff.jsonl`（文字找茬）用另一套结构：`{"id":"diff-0001","v":1,"a":"今天天气真好我们出去玩吧","b":"今天天气真好我们去玩吧","d":1}` —— 答案由引擎算出（长度/字符差），不需要人工标注。

`items.json` / `fortune.jsonl` / `almanac.jsonl` / `horoscope.jsonl` / `copy.jsonl` 同格式（`items.json` 例外：它是物品字典，首次启动导入 `item_def` 的 `world='*'` 行）。

### 5.4 加载与校验

`content/loader.py`：

1. **懒加载**：首次访问类目才读盘；之后最多每 60 s 复核 `(mtime, size)`。
2. **逐行解析**：`json.loads` 失败 → 记录 `(行号, 异常摘要)` 到内存计数器与 `logger.warning`，**跳过该行继续**；不抛异常（一个坏行不能毁掉整个类目）。
3. **结构校验**（与 `content/validate.py` 同一套函数）：必填、类型、长度、`id` 正则、答案归一化非空、`q` 不含答案明文。
4. **去重**：`(category, normalize(sorted(a)))` 归一化后重复 → 保留首行、后者丢弃并 warning（归一化：NFKC、去空白、全角转半角、ASCII 小写、去常见标点）。**不引入 opencc 做繁简转换**（避免新依赖），繁简差异写进题库编写规范。
5. **坏数据上限**：坏行数 > 该文件 10% → 整个类目拒绝加载并回退到 ② 或 ③，同时审计一行 `err:content`（防止只改坏一半还上线）。
6. **配额**：类目可用需 ≥30 题（可配 `kv`；2026-11-05 由 50 下调为 30，理由：样例阶段每类 30 题即可跑通，门槛过高会让「题库没准备好」变成常态），不足时开局回「这个题库还没准备好」。

`content/validate.py`（发布前）：

```
python tools/playhub_content.py check      # 校验全部类目，输出错误行号
python tools/playhub_content.py stats      # 各类目题量/难度分布/重复率
python tools/playhub_content.py dedupe     # 输出重复组，不改文件
```

只读检查，**不自动改题库文件**（避免脚本误删人工内容）。

### 5.5 体量与轮转

| 类目 | 初始题量 | 目标 | 单文件大小 |
|---|---|---|---|
| chengyu / riddle / trivia | 200 | 500 | ~80 KB |
| song / movie / star / animal / plant / food / honor | 150 | 300 | ~60 KB |
| anime / guoman | 150 + 150 | 300 + 300 | 各 ~60 KB |
| diff | 100 | 300 | ~40 KB |
| fortune / sign / almanac / horoscope / copy | 100 / 100 / 词表 / 12×7 / 200 | — | 小 |

- 初始合计 ≈ 2200 题 / ≈ 350 KB，不进插件目录。
- 单类目超过 500 题或单文件 >512 KiB 时拆 `chengyu.part2.jsonl`，加载器支持 `<cat>.jsonl` + `<cat>.*.jsonl` 通配合并。
- **轮转**：选题先排除 `quiz_stat.last_at > now - 7d` 的题；排除后为空则全量随机。`shown/correct` 用于难度自适应（正确率高 → 提高 `d` 权重）。同一群不重复同一题（`quiz_round` 近 50 条排除）。
- 内存：单类目 500 题常驻约 1–2 MB，多类目并存仍远低于单进程限制；不预加载全部类目。

---

## 6 多账号 / 并发 / 幂等

### 6.1 每账号数据路径

事实链：

- 机器人是多账号的（`wxbot_framework/account.py`，每账号一个 workspace）。
- 插件加载时 `context.project_root = self.directory.parent`（`plugins.py:203`，= **仓库根**，所有账号相同）。
- 每账号的 plugin 配置通过 `llm_config_path = self.store.path.parent / llm_config_file` 推导（`plugins.py:200-201`），`self.store.path.parent` 就是该账号 workspace。
- 成员库路径 = `members_config.json` 同目录下的 `database_file`（`members.py:87-156`），且 `from_config` 要求固定 `wechat_account` 以隔离账号（`members.py:166-175`）。

**结论：`ctx.project_root` 不能用于落盘（它是仓库根，所有账号共用）**；douluo 文档里 `Path(ctx.project_root)/".runtime"/"douluo"/...`（`docs/design/douluo.md:44`）在多账号下会串库，playhub 必须改。

解析顺序（`app.py:resolve_workspace`）：

```python
def resolve_workspace(ctx, msg) -> Path:
    members = getattr(ctx, "_members", None)          # sdk.py:484 私有属性；group_notice:38 已有同样用法
    cfg = getattr(members, "config_file", None) or getattr(members, "config_path", None)
    if cfg:
        return Path(cfg).parent                       # 成员库所在目录 = 该账号 workspace
    llm = getattr(ctx, "_llm_config_path", None)      # sdk.py:482
    if llm:
        return Path(llm).parent
    acct = (msg.data.get("self_wxid") or "unknown")   # text.py:110-116 的 data 含 self_wxid
    digest = hashlib.sha256(acct.encode()).hexdigest()[:16]
    return Path(ctx.project_root) / ".runtime" / "playhub" / digest
```

- 属性名 `member_store.config_file` **未核实**（`members.py:87-156` 只确认了 `database_file` 配置项与"路径 = 配置文件同目录"的推导）；实现时用 `getattr` 探测两种候选名，并写测试 `test_playhub_workspace.py` 断言「两个不同账号 → 两个不同目录」。
- 现有先例：`plugins/picture_idiom/__init__.py:48-57` 用 `project_root/.runtime/<插件>/<sha256(account)[:16]>` —— 同样做到了账号隔离，但数据在仓库根，备份/迁移语义差；playhub 采用"账号自有目录 + 哈希兜底"。
- 落盘结构：

```
<账号工作区>/.runtime/playhub/
├── playhub.sqlite3 (+ -wal / -shm)
├── content/            ← 运维覆盖题库（可选）
├── bad/                ← 加载失败的坏行导出（含行号与原因）
└── logs/               ← 大动作审计的外部堆栈（可选）
```

### 6.2 并发

| 层 | 事实 | 对策 |
|---|---|---|
| 插件回调 | 单线程串行（`plugins.py:86-111`） | 所有 DB 操作走同一线程、同一连接 |
| 定时回调 | 与消息回调共享 `callback_lock`，`timeout=2` 拿不到即失败（`plugins.py:304-336`） | 事务 <50 ms；定时动作分页处理，单次不扫全表 |
| 执行器线程 | 自己创建 | **禁止碰 `Store`**，只做 HTTP/LLM/文件，结果回主线程 |
| 多进程 | 同账号正常不会双开；用户可能复制目录 | `WAL` + `busy_timeout=3000` + 写用 `BEGIN IMMEDIATE`；捕获 `sqlite3.OperationalError`（locked/busy）→ 回「稍后再试」并审计 `err:busy` |
| 连接 | 单连接 | `sqlite3.connect(path, timeout=3.0, isolation_level=None, check_same_thread=False)`；`isolation_level=None` 手动控事务；`check_same_thread=False` 仅为线程内迁移留余地，**运行时仍只在一个线程使用** |

事务纪律（写成 `store.tx()` 的实现约束 + 测试）：

1. `tx()` 内不得调用 `ctx.*` 的任何发送方法（由 `Reply` 机制在结构上保证）。
2. `tx()` 内不得 HTTP / LLM / 打开文件。
3. 一个命令最多一个 `tx()`；多步更新必须合并到一个事务。
4. 任何 `except` 都要回滚，绝不留半截状态（`credit` 扣钱成功但发奖失败这种必须在同一事务）。

### 6.3 幂等

| 场景 | 幂等键 | 依据 |
|---|---|---|
| 群友发命令 | `f"{message.session_id}:{message.sequence}:{cmd.name}"` | `Message` 有 `session_id` 与 `sequence`（`sdk.py:24-37`）；成员库对 `(account, session_id, sequence)` 建了 `WHERE sequence>0` 的唯一索引（`members.py:202-203`），说明该组合在框架内被视为消息唯一标识 |
| `sequence == 0` 的消息（含群事件） | `f"evt:{room}:{kind}:{uid}:{day}"` | 群事件消息由 `_publish_group_events` 构造，**没有填 sequence**（`runtime.py:241-247`）→ 只能按"同日同人同类型一次"去重 |
| 定时动作 | `f"sched:{action}:{room}:{day}:{hour}"` | 定时任务被重复触发（手动测试/多实例）时不重复发币 |
| 抽奖/开箱 | 外层业务幂等键 + `wallet_ledger` 的 `UNIQUE(room, operation_key, posting_no)` | **先取得业务幂等权再更新**：同一事务内查幂等键 → 校验次数与余额 → `debit` → 写 `lottery_draw` → 写流水 → 提交。分录唯一键只是第二道保护，不替代业务幂等 |
| 事件计数 | 复用 `idem` 的 `evt:{room}:{kind}:{uid}:{day}` 键，**先取得幂等权再更新** | 事件没有消息 id（见下），只靠 `INSERT ... ON CONFLICT DO UPDATE` 只能保「同日同人同类型一条」，保不了「同一事件只处理一次」；因此先 `idem.claim()` 成功，再累加 `invite_daily` / `member_seen` |

`idem.claim()` = `INSERT OR IGNORE` + `cursor.rowcount == 1` 判定首次；`idem` 表按 30 天清理（`scheduled_quiz_gc` 里顺带做）。

**顺序约束（v3 统一表述）**：所有写路径一律「**先取得业务幂等权，再更新状态**」。`idem.claim()` 或业务幂等键检查必须发生在任何余额、次数、业务状态变更之前；反过来（先改状态再登记幂等）在崩溃或多实例下必然重复计功，且无法事后补救。

**注意**：`Msg.data` 里的 `group_event` 由框架解析，`GroupMemberEvent` 只有 `kind/room_id/member_id/nickname/source`（`group_events.py:43-49`），**没有消息 id** —— 这是"事件只能按日去重"的根本原因，不是设计偷懒。v3 由 C1 给它加 `actor_id`（操作者），但 `actor_id` 是**人**的标识、不是事件标识，仍然不能当幂等键用（§8.7）。

---

## 7 分批交付

每批可独立上线、独立验证。人日含单测，不含需求变更。

### 第 1 批：最小可用内核（11 人日）

范围：`core/*`（除 `items`）、`core/wallet.py`（两币 + 分录 + 迁移）、`core/permissions.py` + `adapters/memberfeed.py` 角色读取、`keywords.py`（61 词表 + 四道校验）、`router`（归一化 + 艾特范围重映射 + 只拆首个 `#` + 裸触发策略）、`registry`、`app`（含 `is_self` 免疫、自触发闸、权限编排与拒绝审计）、`business`、`textkit`、`engines/daily`（签到、抽签）、`engines/ranking`（金币榜）、帮助命令、`聊天 开/关` 占位。约 21 个文件。

人日构成：原 9 + 钱包迁移 1.25 + 一层权限 0.5 + `#`/艾特解析 0.25 = **11**。签到的钻石入口、抽签的上上签入口随本批进入，但**概率未拍板前不启用钻石产出**（见 §4.4 与 §9）。

验收标准：

1. `签到` 首次发币、当日第二次提示已签、跨日恢复；`排行榜 金币` 出 top10；`打卡`（别名）与 `签到` 完全等价；余额只经 `wallet` 读写。
   **本批签到奖励边界**：只发金币；金箱延到第 3 批，本批无金箱、不追溯补发。
   本条同时断言签到不产出钻石、不发放任何物品。
2. 杀进程重启，数据仍在（事务已提交）；`schema_version` 写入 `meta`；旧库迁移后余额与非零流水对得上（§3.2.2）。
3. 未实现的 45 项玩法的触发词**静默忽略**（`点歌`、`好的`、`签到！`、`@张三 签到`、`猜 成语` 全部零回复零日志噪音），且 `normalize()` 对零宽/NBSP/全角空格的等价类用例全通过；`#` 与艾特等价类（§2.6.2 表）全通过。
   **榜单分批可用性**：`Board` 保留四个值；本批 `top()` 对 `activity` / `marriage` / `invite` 抛 `NotImplementedError`。入口先判可用性——公开路径对未开放榜单静默返回、不调用 `top()`，内部误调用由测试断言上述异常。本批帮助里的榜单只列金币榜。
4. 两个账号（不同 workspace）数据到各自目录，交叉不可见（`test_playhub_workspace.py`）。
5. `grep SELECT plugins/playhub/engines/` 为空（分层测试）。
6. 1000 条命令压测，p95 <50 ms，无队列丢弃（`plugins.py:76-84`）；`normalize`+查表单次 <20 µs。
7. 69 词冲突检测用例：注入一个与框架词（`提示`）同名的别名，断言 `TriggerConflict` 消息里同时出现两个功能名；注入一个四字中文词，断言该引擎被拒。
8. 钱包并发用例：两条连接同时从 100 扣 80，只有一条成功；余额不足不能跨币补；流水插入失败整体回滚；同 `operation_key` 重放不重复；非法币种/零/负/浮点溢出被拒。
9. 权限用例：`owner`/`admin` 可执行、`member` 拒绝且业务状态不变、`unknown` 与读取异常失败关闭、审计不被业务回滚吞掉。

### 第 2 批：竞猜引擎 + 题库管线（12 人日）

范围：`engines/quiz`、`engines/base`、`content/*`、`content/playhub/*.jsonl` 首批 11 类目各 100 题、`quiz_round/quiz_stat/quiz_attempt` 迁移。（超时收题与清理归第 4 批 §4.2 后台线程；`daily_quiz` 不做。）约 9 个文件 + 题库。

实现进度（2026-11-05）：`content/loader.py`、`engines/quiz.py`、`registry` 13 条绑定、`app._plain_text` 纯文本通路与 `tests/test_playhub_quiz.py`（22 例）已落地并通过；题库为 13 类 × 30 题（样例量，非下表 100 题/类）。**未落地**：`engines/base`（改用 `engines/__init__.py` 的 `Reply`/`GameRuntime`，不单建文件）、超时收题（`sweep()` 已公开，由批 4 §4.2 后台线程接线；`daily_quiz` 不做）、题库扩量。

验收标准：

1. 13 个类目（含 diff/trivia）均可开局、抢答、提示、公布答案、超时收题。
2. `猜动漫` 与 `猜国漫` 题库独立，交叉出题 0 命中（测试断言题 id 前缀不同）。
3. 同群同时只能有一个未结束对局（部分唯一索引生效，第二局被拒）。
4. 坏行容错：注入 10 行坏 JSON、1 行超长、1 行重复，仍能加载且统计正确；坏行 >10% 时整类目回退。
5. 全量题库（≈2200 题）冷启动 <300 ms，单次出题 <5 ms。
6. 抢答只消费**非关键词**文本：`猜成语` 开局后，`提示`/`换一题`/`公布答案`/`结束` 四个框架词按子命令处理（不当作答案），其余 64 个关键词一律不进抢答判定，普通聊天（`今晚吃什么`）被消费且不写 `quiz_attempt` 之外的任何表。

### 第 3 批：社交 / 抽奖 / 外部服务（14 人日）

范围：`engines/social`（含婚姻 4 子命令与清求婚次数）、`engines/lottery`（含钻石购买盲盒次数）、`engines/duel`（不联动监狱）、`engines/music`、`engines/lounge`（文案馆 + 群空间卡片）、`core/items`、`adapters/outbound`、`adapters/executor`、`engines/daily` 扩展（黄历/星座/求财神）、`items.json` 导入。约 8 个文件。

人日构成：原 14 + 婚姻四子命令 1.25 + 钻石买盲盒次数与清求婚次数 0.5 − 超级宝宝 0.75 − 切磋监狱/医院联动 1.0 = **14**。本批验收的是「基础宝宝」（看/喂养/改名 X/超极说明），不是官方 20 条超级宝宝子指令。

**实现进度（2026-11-05，第一批已落盘）**：

- `plugins/playhub/core/switches.py`：5 个开关（§4.3.1），`kv` 键 `feature.marriage|kinship|employ|concubine|baby`，默认关、失败关闭。
- `plugins/playhub/engines/social.py`：婚姻段完整落地——`结婚 @对象` → 对方 `结婚 同意`/`拒绝`；一人一婚（双侧校验）、同一对不重复 pending、每日 3 次求婚、`我的婚姻`、`度蜜月`（520 金币 + 24 h 冷却 + 纯展示）、`申请离婚`（200 金币 + 对方 24 h 内 `同意离婚`/`拒绝离婚`）、`婚姻统计`、`清求婚次数`（15 钻）。`@对象` 两条解析路径：结构化 `cmd.mentions[].uid`（单测用）与 `ctx.members.resolve(昵称, 群).wxid`（真实群；框架 `Message` 没有结构化艾特列表，见 `wxbot_framework/sdk.py:25-58`）；解析不到回「没认出」，**不猜 uid**。
- **两条文档原先没写的落地口径**（已固化在 `social.py` 模块 docstring）：①求婚 24 h 不确认自动失效；②超时退款是**惰性**的——本批无定时器，靠任意婚姻子命令先跑 `_expire()` 收尾，批量定时归第 4 批。这两条都只影响"什么时候退钱"，不影响金额。
- 接线：`registry.CURRENT_BATCH = 3`、`BINDINGS` 17 → 18（`social.marry`）、`engines.build_engines()` 增 `social`；`app.py` 新增 `ACTION_SWITCH` 与 `_switch_off()`，在 `_feature_off()` 之后、静默 `return None`。
- 测试：`tests/test_playhub_social.py` **77 例**（含关系卡 5、配对 5、礼物 6、宝宝 12）（开关、求婚、成婚、一人一婚、每日上限、蜜月扣费与冷却、离婚四条路径含超时退款、清求婚次数、审计行；闺蜜/结拜/雇佣/纳妾的发起-同意-拒绝-解除、多人并存、纳妾两条门槛）；playhub 全套 **262 passed / 547 subtests**。
- **闺蜜 / 结拜 / 雇佣 / 纳妾已一并落地**（`RELATIONS` 表 + 同一套 `rel_propose / rel_accept / rel_reject / rel_dissolve`）：
  - 子命令按 §4 表：`@对象` / `同意` / `拒绝` / `解除 @对象`；雇额外一条 `工资`。
  - **两者之间只差三件事**：开关（`kinship` 管闺蜜+结拜，`employ`、`concubine` 各一个）、纳妾的两条门槛（发起人须已有婚姻；钻石余额 ≥30，**门槛不扣款**）、雇佣的 `meta.wage`（默认 20 金币/日）。
  - **允许一个人同时有多条**闺蜜/结拜/雇佣/纳妾；只有 `marriage` 受「一人一婚」约束——`pair` 的两个部分唯一索引本来就只对婚姻建。
  - `解除 @对象` 对 pending 行表示**撤回邀请**（发起人主动取消），对 active 行表示解除；两种都写 `audit`。
  - `雇佣 工资` **只展示**约定日工资与对方称呼，不发钱；实际发放是第 4 批 `scheduled_pay_salary`。v1 不在本文件内造币。
- **关系卡 / 配对 / 礼物 / 宝宝（v1）已一并落地**（`BINDINGS` 22 → 26，四项都挂在 `social` 引擎上，不另开引擎）：
  - `core/items.py`：5 件礼物的价目 + `inventory` 增删 + `item_def` 播种（`ON CONFLICT DO NOTHING`，不覆盖群里改过的行）。**取物顺序固定「背包优先、没有才代买」**——v1 还没有盲盒/开宝箱，只按背包取会让礼物在第一天就不可用；这条是落地口径，已写进模块 docstring 与 §4.4。
  - `social.card`（关系卡）：只读，省略参数看自己、带 `@` 看别人；每类最多列 3 人。为了卡面能显示昵称，**读卡时也会 `ensure_player`**一次（`player.nickname` 是唯一昵称来源），对没打过交道的账号会补一行。
  - `social.match`（配对）：纯计算、不落库，`sha256("room|day|low|high")` 取模 101；两人顺序无关；跨天重摇。**没有金币成本**，所以不进 sink 表。
  - `social.gift`（礼物）：`礼物 @对象 物品` → 背包有就扣背包，没有就按价目 `debit` 代买（流水键 `gift:<from>:<to>:<item>:<ts>`，捕获 `Insufficient`/`DuplicatePosting`）；成功写 `audit(action="cmd:礼物")` 与 `kv` 的 `gifts:<low>:<high>` 计数。**礼物不进对方背包**（v1 没有消费礼物的玩法）。
  - `baby.view`（宝宝 v1）：`看 / 喂养 / 改名 X / 超极`；没有独立「出生」子命令，第一次 `看` 或 `喂养` 自动领养；喂养 20 金币、日 3 次（`daily_counter` 键 `baby_feed`）、`care +10`、`exp +5`；流水键带上「当日第几次」（`baby_feed:<uid>:<day>:<used>`），否则同一秒连喂会被 `wallet_ledger` 的唯一键判成重复流水（实际踩到过）。
  - 数值表补在 §4.4（礼物五档价、喂养成本与阶段阈值、配对不花钱）。
- **第二批（抽奖 / 娱乐 / 外部服务）已落盘，`BINDINGS` 26 → 37**：
  - `plugins/playhub/engines/lottery.py`：`盲盒`（每日免费 1 次、`买次数` 5 钻限 2 次、连开最多 10、掉落表在 `core/items.BLIND_BOX_LOOT`）与 `开宝箱`（铜/银/金，币值 50/150/400，钻石按 1/20·1/12·1/8 判定，写 `lottery_draw` 台账）。**踩坑**：最初把「未购买的加购额度」当成可用次数，导致裸发第二次仍能抽、`盲盒 8` 抽了 3 次；修法是把「买到的次数」与「已用掉的加购次数」拆成两个计数器。
  - `plugins/playhub/engines/lounge.py`：`文案`（主题轮转 + `kv` 游标，带 `@昵称` 回包）、`群空间`（整点缓存 1 小时的只读聚合）、`聊天`（只回说明卡，**不假装会聊**）。**踩坑**：`space()` 曾在只读事务里再开写事务，报 `cannot start a transaction within a transaction`；改成出事务后单独写缓存。
  - `plugins/playhub/engines/duel.py`：`切磋 @对象` → `同意`/`拒绝`，10 分钟过期，胜率 50%，只记战绩不碰钱包（文档明确「不联动监狱/医院」）。
  - `plugins/playhub/engines/music.py`：`点歌` 只登记队列（上限 5）与排位，每人 30 秒冷却，**不发音频**（音源 §9 未定）。
  - `plugins/playhub/engines/outbound.py` + `adapters/outbound.py`：`WeatherProvider` 协议 + 离线默认实现，`天气 城市` 在没接服务时明说「天气服务还没配置」，不编数据。**2026-11-06 接上和风天气**：`QWeatherProvider`（两跳：`geo/v2/city/lookup` → `v7/weather/3d`）、`build_provider(config)` 失败关闭、3 s 超时、64 KiB 响应上限、连续 3 次失败熔断 5 分钟；`App(config=…)` → `build_engines(config=…)` → provider 一条线传下来。
  - `plugins/playhub/engines/daily.py`：`黄历`（`明天`/`昨天` 偏移、按日轮转）、`星座`（不给星座按哈希推定、四项指数 + 幸运数字/颜色）、`求财神`（固定 +20 金币、一日一次），文案库 `content/playhub/{almanac,horoscope,wealth}.jsonl` 各 24 条。
  - 签到现在发箱：每日铜箱 1 个、连签第 7 天额外金箱 1 个（`items.add` 写 `inventory`）。
  - 测试：`tests/test_playhub_lottery.py`（20 例）、`tests/test_playhub_batch3.py`（23 例）、`tests/test_playhub_weather.py`（26 例：两跳解析/LocationID 缓存/缺口截断/各类失败吞成 `None`/熔断四条/装配与引擎缓存）；2026-11-06 实测 `pytest -k "playhub or idiom_chain"` → **554 passed / 570 subtests**，`main.py --check` 通过。
  - `plugins/playhub/core/kv.py`：房间级 JSON 键值工具（`get/get_json/put/set_json/bump/clear`），把引擎里的重复 SQL 收成一处。
- **第 4 批 6 个动作与 5 个定时动作均已落地**（`aea02f1` 起，详见 §7 第 4 批）。

验收标准：

1. `结婚 @A` → A 收到请求，A 发 `结婚 同意` 建关系；B 再求婚被拒；一人一婚；`结婚 我的婚姻` 出婚龄与蜜月次数；`结婚 度蜜月` 扣 520 金币并挂 24h 标记；`结婚 申请离婚` 扣 200 金币进入待确认，对方 24h 内 `同意离婚`/`拒绝离婚`，超时失效并全额退费（见 §4.3）。
2. `纳妾` 在未开开关的群静默；v1 **默认为永久关闭且无扣款入口**（30 钻石是准入门槛、不是扣款）；`纳妾` 裸发永远静默（高危词 `bare_behavior=silent`）。
3. `盲盒` 扣币、发物品、写 `lottery_draw`（`cost` 与 `cost_currency='coin'` 同时记）；爆率统计与配置一致（1 万次模拟 ±5%）；`盲盒 买次数` 花 5 钻石 +1 次，每日上限 2 次，`24:00` Asia/Shanghai 清零，不累积不退还。
4. `点歌` 外部源超时 3 s 后回降级文案，不阻塞队列（实测 5 次连续超时，`签到` 仍 <50 ms）。
5. `群空间` 输出房间概览（人数/活跃/榜首/关系数），数字与查询一致。
6. `文案` 轮转不重复直到耗尽（`kv` 游标）；`文案` 裸发走 `guide`，每群 24 h 只回一次用法。
7. `礼物` 裸发或只带 `@对象` 不带物品时**不执行**（回一句用法），`礼物 @A 玫瑰` 才扣币发货。

### 第 4 批：群管统计 / 定时推送 / 合规收口（12 人日）

范围：`engines/groupstat`、`engines/moderation`、`activity_daily`/`member_seen`/`nickname_log`/`invite_daily`/`invite_credit` 计数接线、`core/rank.py` 四榜与活跃榜月/总、5 个后台定时动作（插件自管线程 `scheduler.py`，§4.2；第 46 项定时推送复用框架控制台定时文本任务，不写新代码）、C1（`wxbot_framework/group_events.py` 五处）+ `group_notice` 回归、功能开关面板说明。**不再包含 `rank_period` 快照任务**（v1 改为实时聚合）。

人日构成：原 10 + 四榜及活跃月/总 0.5 + C1 与群通知回归 0.75 + 后台定时线程（§4.2，含 4.2.9 钱包写序、4.2.10 消息级幂等）0.5 + 跨模块一致性验收 0.25 = **12**。

验收标准：

1. 群消息驱动 `activity_daily` 累加；`潜水 7` 与 `活跃榜 周` 数字对得上；`活跃榜 月`/`活跃榜 总` 在跨月、跨年、2 月边界用例上区间正确（周一为周起点）。
2. 改群昵称后，次日巡检（或手动 `改名 现在`）出一条 `nickname_log` 并在群内播报。
3. `小黑屋 @A 10` 后 A 的游戏命令被静默；10 分钟后自动解除（定时巡检）。
4. 权限判定统一走 `ctx.group_member_role()`（`adapters/memberfeed.py` 封装）：只有群主/管理员能执行 `小黑屋`/`黑名单`/`潜水统计`/`改名` 监测开关；`member`、`unknown` 与读取异常一律拒绝，记 `audit.result='denied'` 并回一句拒绝，**不得改业务状态、不扣款**。自建名单（旧 `members.owner_ids`/`is_owner` 思路）命中但群角色为 `member` 时仍拒绝。
5. 5 个后台定时动作（§4.2）按 §4.2.8 的离线用例通过：改名巡检默认开、每天至多播报一次、`改名 关` 后不播；离婚超时自动退款；超时题自动收、`idem` 30 天 / `audit` 180 天前的行被清；工资每对每天只发一次（雇主 `by_uid` 出、余额不足不发并审计、`feature.employ` 关时不发）；每周一给上周活跃榜前 10 各发 1 个银箱、不播报、同周不重发；重启与同日多次 tick 均不重复。第 46 项定时推送在控制台「定时任务」页用框架定时文本任务可建、可停、可删，playhub 不参与。
6. `宝宝 改名 X` 不被门禁误拦（它是玩家玩法，不是群管理开关）。
7. 全量回归：47 项逐条冒烟（脚本化，按 `keywords.py` 的 62 个词遍历），无异常堆栈、无队列堆积；同一脚本再跑一遍"误触语料"（50 条常见闲聊），断言零回复；§4 总表项数、触发词账、帮助文案三处数量断言一致。

**落地进度（2026-11-05，前半批已完成）**

- 已落盘：`core/activity.py`（`record` 双 UPSERT `activity_daily` + `member_seen`、`scores`、`last_seen`、`seen_names`、`record_nickname_change`、`recent_names`）、`core/clock.range_of()`、`engines/groupstat.py`（`潜水`/`邀请`/`改名`）、`engines/moderation.py`（`小黑屋`/`黑名单`，导出 `is_jailed`/`is_blocked`）、`engines/ranking.py` 的 `activity` 榜（`top()` 支持 `period`，`format_board()` 泛化、新增 `format_activity()`）。
- 接线：`registry.CURRENT_BATCH = 4`，`BINDINGS` **37 → 43**（本批 6 个动作全部绑定，`ACTION_PLAN` 里不再有 `None`）；`build_engines` 加 `groupstat`/`moderation`；`app.handle_message` 在 `route()` **之前**判小黑屋/黑名单静默，纯文本分支记 `activity.record`（命令消息不记、自己不记）。
- 验收对应：第 1、2、3、4、6 条已由 `tests/test_playhub_batch4.py`（56 例）覆盖；第 5 条（后台定时动作）与第 7 条（47 项脚本化冒烟）未做。
- 全量：playhub **363 passed / 557 subtests**，`main.py --check` 通过。
- 第 4 批剩余（都是定时侧，2026-11-06 改为插件自管线程，见 §4.2）：改名巡检、离婚超时退款、收题与 `idem`/`audit` 清理、发工资、周榜发银箱共 5 个动作，落在新文件 `plugins/playhub/scheduler.py`；不绑 `plan_id/revision`、不做群内批准。`daily_quiz`/`daily_almanac`/`daily_report`/`space_refresh` 不做（不在 47 项内）；第 46 项只是用户在控制台自配的定时文本。另含 §4.2.9 钱包写序修复、§4.2.10 消息级幂等、框架 `ctx.chat_allowed`。
- 三条踩坑（已写进代码注释）：①`ctx.group_members()` 缓存未就绪返回 `None`，**不能** `raw or ()` 当空列表，否则「读不到快照」会被误报成「没人改名」；②`小黑屋 10` 的目标位会解析成数字，必须先判「纯数字且无后续」再回「要点人」；③`render.result()` 的 headline 与 `render.head()` 都写标题会重复一行，列表卡只用 headline 带人数。

**落地进度（2026-11-06，收尾批）**

- 新增 `adapters/executor.py`（221 行，§2.5 第 3、4 条）：有界线程池 `workers=4` / `queue_size=32` / `timeout=3 s`，`submit()` 拿不到名额就返回 `False` 由调用方退化；`defer()` + `flush()` 是「worker 不碰 `Store`」的落地形态（`App._flush()` 在每条消息开头执行）；`shutdown()` 丢弃延迟队列（卸载时库马上关，硬写只产生噪音）。超时只记账、不中断，已在模块 docstring 写明。
- `engines/outbound.py` 天气改为「有执行器走异步、队列满退化同步」：异步路径**不回「正在查」**，worker 直接发结果卡；缓存用 `defer` 交回插件线程，**先入队再发送**（否则「刚查完再问」会稳定多打一次外部接口）。
- 补硬测试：`tests/test_playhub_loader.py`（16 例，60 s `(mtime,size)` 复核、坏行 10% 门槛、覆盖层回落）、`tests/test_playhub_scheduler.py` +17 例（`is_due_daily`/`is_due_weekly` 窗口、跨年周键、`_salary_days` 补发 ≤3 天与回拨）、`tests/test_playhub_executor.py`（19 例）、`tests/test_playhub_weather.py` +7 例异步路径（返回 `None`、worker 发卡、缓存落到下一条消息、队列满退化、无 `ctx.reply` 只记日志、库已关静默）。
- 全量：`pytest -k "playhub or idiom_chain"` **635 passed / 570 subtests**，`main.py --check` 通过。真机冒烟单在 `docs/design/playhub-smoke.md`（29 条 + 开库 SQL + 故障对照表，版本自检期望已同步）。

### 人日汇总

| 批次 | 内容 | 人日 |
|---|---|---|
| 1 | 内核 + 两币钱包与迁移 + 一层权限门禁 + `#`/艾特解析 + 签到/抽签/金币榜 | 11 |
| 2 | 竞猜 + 题库管线 + 13 类目 | 12 |
| 3 | 社交（含婚姻 4 子命令）+ 抽奖 + 点歌/天气/文案/群空间 | 14 |
| 4 | 群管统计 + 四榜 + 后台定时线程 + C1 与合规收口 | 12 |
| — | 合计 | **49** |
| — | 缓冲 15%（题库补录、联调、真机验证） | 7.35 |
| — | 合计（含缓冲） | **56.35 → 取 57** |

与 v1 的差异：净增 4 人日（45 → 49）。新增 5.75（钱包迁移与两币契约 1.25、一层权限 0.5、`#`/艾特解析 0.25、婚姻四子命令 1.25、钻石买盲盒次数与清求婚次数 0.5、四榜及活跃月/总 0.5、C1 与群通知回归 0.75、后台定时线程 0.5（原「定时推送批准契约」作废，人日原位替换）、跨模块一致性验收 0.25），删减 1.75（超级宝宝 0.75、切磋的监狱/医院联动 1.0）。第 1 批先落权限框架，后续批次的管理动作直接复用；`rank_period` 从第 4 批必交项移出，改为实时聚合。

题库内容生产（2200 题）不在人日估算内，建议与 B2 并行外包/自录，按 60 题/人日 ≈ 37 人日的内容工作量，需单独立项。

**若改用面板身份认证替代群角色门禁**，第 4 批需重新估算（批准计划、执行前重查、审计链路都要改），不沿用上表数字。

---

## 8 风险清单

### 8.1 合规与舆情

| 风险 | 说明 | 缓解 |
|---|---|---|
| 真人关系功能（结婚/纳妾/闺蜜/雇佣/结拜） | 在公开群映射现实关系，易引发起哄、骚扰、群内冲突；「纳妾」在现代语境高度敏感 | 全部关系功能**默认关闭**，按群开启（`kv: feature.<kind>`）；`纳妾` 单独开关且默认永久关闭；帮助文案不使用"妻妾"措辞，配置项命名为"关系玩法"；所有关系操作写 `audit`（可追溯谁对谁发起） |
| 未成年人 | 无年龄识别能力（框架无此数据） | 关系类功能只在管理员开启的群生效；不做任何涉及身体/金钱交换的玩法设计（彩礼仅用游戏币） |
| 数据画像 | 潜水统计/改名/黑名单=对群成员持续建档 | 只存 `wxid/昵称/计数/时间`，**不存消息正文**；`activity_daily` 的稀疏日聚合**永久保留**（它是总榜与活跃榜的唯一数据源，90 天清理**不适用**于它，见 §3.2.3），其余明细与 `nickname_log` 按 90 天清理；`群空间` 里明示"本群使用统计类插件"；`audit.detail` 禁止写正文（§3 注释） |

### 8.2 自动处罚误伤

- **事实**：框架能力集里**没有踢人/禁言**（`wxbot_framework/backend.py:10-31`，群相关只有 `group.member.invite` / `group.create` / `group.members.read` / `group.events`）。任何"自动踢人/自动禁言"都必须改 native 层，不在本设计范围（**未核实** native 侧是否已有未暴露的踢人接口）。
- 因此 `小黑屋` 只能做**软禁言**：不响应该成员的游戏命令与抢答、不进排行、不参与抽奖；群内播报"已进小黑屋 10 分钟"。
- 误伤防护：仅群主/管理员可操作，判定统一走 `ctx.group_member_role()`（`adapters/memberfeed.py` 封装，`core/permissions.py` 决策）；`member`/`unknown`/读取异常按失败关闭拒绝；单次上限 60 分钟、每日上限 10 次；全部写审计；到期自动解除；提供 `小黑屋 解除 @某人`。**不再使用 `members.owner_ids`/`is_owner` 作为放行依据**——那是本地自建名单，与群内真实角色可能不一致，且无法覆盖群主转让。
- **无前缀方案的额外误伤面**：`小黑屋`/`黑名单` 虽被判为低误触（"把人关小黑屋"在群里极少整句出现），但它们是**处罚类**命令，误触代价最高。因此这两项额外要求：裸发只回用法不执行、且执行前必须有 `@对象`（`小黑屋 10` 这种缺人的形态一律拒绝）。
- **黑名单**同理：只在本插件内生效（`scope='play'`），不影响 `mention_chat` 等其它插件（除非它们自己读我们的表，而它们不会）。

### 8.3 版权

| 项 | 风险 | 硬约束（写进题库规范 + 校验） |
|---|---|---|
| 猜电影 | 海报/剧照有版权 | **只用文字题面**（演员/台词/年份/剧情关键词），不下载、不发送任何图片 |
| 猜动漫/猜国漫 | 角色立绘、截图 | 同上，纯文字；不引用长段台词原文（≤20 字且属评论性引用范围） |
| 猜歌名 | 音频/歌词版权 | **不发音频、不发歌词原文**；提示只用"歌手/年代/曲风/首字"，且不提供试听链接 |
| 猜明星/猜王者 | 肖像、游戏素材 | 纯文字 |
| 文案馆 | 网络文案转载 | 只收自制/授权文本；`src` 字段记录出处；提供"删除某条"的运维路径（改 jsonl + 重载） |
| 谜语/成语 | 传统公有领域为主 | 标注 `src`，避免整本现代谜语书搬运 |

### 8.4 外部接口

| 接口 | 风险 | 缓解 |
|---|---|---|
| 天气 API | **2026-11-06 已选型并落地：和风天气 QWeather v7**（免费订阅 1000 次/天，key 走账号专属 API Host `*.re.qweatherapi.com`）。剩余风险：key 泄漏、额度耗尽、`weather_api_host` 漏填（旧公共域名对新 key 一律 404/403） | `adapters/outbound` 定义 `WeatherProvider` 协议，默认实现为离线占位（`OfflineProvider` 返回 `None`，卡面写「天气服务还没配置」）；**配了 key 才启用**；3 s 超时（`0 < t ≤ 10`）+ **熔断（连续 3 次失败停 5 min）**；响应体上限 64 KiB（含 gzip 解压后）；城市名解析结果进程内缓存，热路径只一跳；失败一律返回 `None`，引擎只回「服务不可用」，不编数据 |
| 点歌音源 | 音源合法性、音乐版权、大文件发送 | 默认只回文字"已点：《歌名》- 歌手"，不发音频文件；`file.send` 能力存在且框架已取消 20 MiB 上限（`docs/messages/files.md:24`），但**音源授权未解决前不启用音频** |
| LLM（文案馆/智能聊天） | 成本、超时、串行阻塞 | 文案馆默认走本地库；LLM 为可选开关，且必须走执行器 + 3 s 超时 + 失败降级 |
| 内容源（星座/黄历） | 无需外部，全部本地化 | 黄历用本地宜忌词表 + 日期确定性抽取（同一日期所有人结果一致），不请求第三方 |

### 8.5 性能

| 风险 | 事实 | 缓解 |
|---|---|---|
| 单线程串行 | 同插件所有玩法共用一条队列（`plugins.py:86-111`），队列满**丢弃**（`plugins.py:76-84`） | 命令只做内存+短事务；慢活走执行器；实测 p95<50 ms 作为 B1 验收项 |
| 事务持锁阻塞全插件 | 发送会分片等待（`sdk.py:111-156`，`pacing.py:9-42`），在事务里发消息=锁数秒 | `Reply` 机制结构性禁止；`store.tx()` 里禁止 `ctx.*` |
| 排行全表扫 | 视图无索引；`rank_period` 快照在 v1 不建 | 日/周/月榜走 `(room, day, uid)` 索引直查 `activity_daily`；总榜走 `v_rank_active` 聚合；金币榜走 `wallet_by_rank`。成员 >2000 且实测超时的群再评估引入快照（v2，见 §3.2.3） |
| 题库加载 | 11 类目全量解析 | 懒加载 + 60 s stat 缓存 |
| 定时动作拿不到锁 | `callback_lock(timeout=2)`（`plugins.py:304-336`） | 回调分页、单次 <200 ms；失败记日志不重试（框架本身也不重试，`docs/development/plugins.md:92`） |
| 跨线程发送 | 发送路径有锁（`sdk.py:111-156`），但**跨线程调用的严格安全性未核实** | 设计双路径：直发 + `PendingOut` 兜底（§2.5）；2026-11-06 已落 `adapters/executor.py`：发送走直发（与 `scheduler` 同模式），`Store` 写入走 `defer`→`flush` |

### 8.6 已知功能限制（需在帮助文案里对用户说明）

1. **邀请统计依赖框架改动（C1，已落地 2026-11-06）**：`GroupMemberEvent` 原本只有 `kind/room_id/member_id/nickname/source`（`group_events.py:43-49`），解析时邀请者信息被丢弃。v3 已按 §8.7 改框架五处补 `actor_id`，现在的口径是：能证明邀请人时按人统计、进邀请榜；`actor_id` 缺失（纯文本无链接、角色词「群主/管理员/你」、名单差分补录、链接不可验证）时**降级为群级计数**，按人榜为空。**残留代价**：`wxbot_framework/` 是上游目录，这五处改动会产生上游合并冲突风险，需随身跟进上游更新（缓解与回归边界见 §8.7）。
2. **接龙进行中关键词被吞**（`plugins/idiom_chain/__init__.py:128`）：接龙开局后任何非 `/` 文本都被当答案送判定，`签到` 也会被回一句「请输入一个完整的四字成语」。**已修（2026-11-06，方案 A）**：守门条件加上 `text in GROUP_CONTROL_COMMANDS`，框架控制词（含 playhub 的 `结束`）不再进判定；`tests/test_idiom_chain.py::test_four_char_control_words_are_ignored_during_chain` 钉住。**残留**：`公布答案`/`换一题`/`提示` 这类**四字**控制词仍会被当答案（本地短路，不花 LLM 额度），要彻底消掉得改 `judge` 的分派顺序，未做。
3. **机器人自己账号发的关键词不进插件**（`text.py:104-115` 的 `own_group_command` 白名单只放行四字中文与框架命令；playhub 的 62 个词全是 2–3 字）。这对正常使用无影响（playhub 另在 `on_message` 首行对 `is_self` 一律 return，见 §2.6.5），**只影响"用自己的号自测"**：自测请用另一个号（进验证群的运营号）。
4. **小黑屋不是真禁言**（无 kick/mute 能力，`backend.py:10-31`）。
5. **群空间控制台页面未定**：`panel_http.py:87-234` 是硬编码路由表，没有插件注册页面机制；B1–B4 只做群内文本卡片 + 数据由此可查，控制台页面需框架改动（见 §9）。
6. **关键词触发天然无法"只对某群生效"**：群里发 `签到` 与日常用语同形，`bare_behavior`/强制参数能压住大部分误触，但**做不到零误触**；已知残留见 §4.3 的 9 个高危词。
7. **两个看图猜成语插件共用 4 个裸词**（`提示`/`换一题`/`公布答案`/`退出猜成语`）：两边都有未结束对局时会被各回一次，跨插件状态不可见（§2.7 冲突 2/3）。—— 自 2026-11-05 起由 `wxbot_framework/game_slot.py` 的共享槽消除：一个群同时只允许一局，后开的一律被拒（见 §2.7 冲突 3）。
8. **游戏槽只在本进程内存**：`wxbot_framework/game_slot.py` 的 `room → 占用者` 表随机器人重启清空、不跨进程。当前插件回调单线程串行，够用；将来若多进程分流，必须改成落盘或 IPC。
9. **天气已异步化，但仍有一处延迟是设计代价**（`adapters/outbound.QWeatherProvider` + `adapters/executor.py`，2026-11-06 落地）：两跳 HTTP（城市名 → LocationID、LocationID → 预报）跑在 worker，回调线程立刻返回、**不回「正在查」**，结果由 worker 直接发一张卡。三条残留：①**队列满或没有执行器时退化成同步**，那时最坏阻塞 `2 × weather_timeout_seconds`（默认 ≤6 s）——宁可慢也不丢查询；②**缓存落库延迟到下一条消息**（worker 不碰 `Store`，写缓存 `defer` → `App._flush()`），所以「刚查完立刻再问一次」可能还没命中缓存，多花一次额度；③**线程无法中断**，`timeout` 只记录超时警告，真正的超时来自 transport。**另外一个坑**：`urllib` 会读环境里的 `ALL_PROXY`/`HTTP_PROXY`，若指向一个不可用的代理，天气会一直「服务不可用」且 3 次后熔断 5 分钟（开发机就是这个状态，2026-11-06 实测）；排查时先清代理再测。
10. **`聊天 开/关` 不存在**（2026-11-06 核实）：`plugins/mention_chat/plugin.json` 没有开关字段，`mention_chat/__init__.py:24-25` 只在启动读 `members.history_enabled`。要关只能停用插件；playhub 的 `聊天` 卡面已写明「那个通道归框架插件 mention_chat 管，本群没有开关」。

### 8.7 邀请人修复（C1：改框架五处）

**结论**：v1 改 `wxbot_framework/group_events.py` 一个文件、五处，把「事件操作者」作为 `actor_id` 带进事件；playhub 只在**已确认邀请入群且 `actor_id` 有效**时记 `invite_credit`。这是本方案里除 `idiom_chain:110` 之外**唯一**的框架改动，必须计入维护预算。

| # | 位置 | 改动（已落地） |
|---|---|---|
| 1 | `group_events.py:22` | `_ACTOR = rf"(?P<actor>{_ANY_PLACEHOLDER}\|你\|群主\|管理员)"`：非捕获组改成命名捕获，原有占位符与「你/群主/管理员」语法不变 |
| 2 | `group_events.py:43-51` | `GroupMemberEvent` 末尾新增 `actor_id: str = ""`（带默认值，旧五参数构造仍可用）；语义是"事件操作者"，不保证等于邀请人 |
| 3 | `group_events.py:103-115` | `_classify()` 返回 `(kind, target, actor)` 三元组，配 `_actor_of(role)`（`$占位符$` → 名字，角色词/空 → `""`）；`groupdict().get("actor")` 兼容没有 `_ACTOR` 的三条正则；`_text_events` 解包成 `kind, role, _actor` |
| 4 | `group_events.py:119-135`、`:216-221` | XML 分支：`actor_id = "" if role == actor else _linked_single(links, actor)`；`_linked_single()` 取**操作者自己的** `link`，唯一成员、`username` 可校验才返回，否则 `""`；与目标同一人时也置空。纯文本分支操作者恒 `""` |
| 5 | `group_events.py:441-442` | `observe_system()` 重建事件时显式带上 `event.actor_id` |

另：成员快照差分事件（`membership`）**保持不动**，继续用默认空操作者。

**解析规则（写进实现与测试）**：

1. 操作者链接必须唯一可验证、且属于操作者占位符；**不能把目标成员的链接当成邀请人**（`role == actor` 时直接置空，`_linked_single` 不复用目标 `link`）。
2. 占位符「你」只在 `self_id` 有效时映射机器人自身；「群主/管理员」是角色词，不映射到猜测的成员（`_actor_of` 只接受 `$…$`）。
3. 操作者解析失败**不丢弃**有效成员事件：成员状态与群级统计照常更新，`actor_id` 留空。
4. 邀请人为空 → 按人榜不加分。可观测性靠 `invite_daily` 里 `joins - invite_joins` 的差（既有列，不新增计数器）。
5. 同一入群事件只记一次邀请：`App._feed_members` 先用 `idem.claim()` 占 `evt:<room>:<kind>:<uid>:<day>`，占不下整条跳过；`invite_credit` 的 `(room, inviter, invitee, day)` 主键只承担**同日去重**。

**消费侧约束（落地版本，与 v1 计划的不同处）**：v1 计划写的 `adapters/memberfeed.py` 并不存在——实际实现是 `plugins/playhub/core/invite.py`（`record()`）+ `plugins/playhub/app.py:_feed_members()`，在 `handle_message` 里 `is_self` 之后、关键词路由之前调用，只记账、永不回复；`source == "membership"` 的补录**不产生邀请归属**（`core/invite.py` 的 `CREDITED_SOURCES`）。`runtime.py` 的 `asdict(event)` 自然携带新字段，无需改动。

**回归边界**：`plugins/group_notice/__init__.py:18-46` 按字典键读 `kind/room_id/member_id/nickname`，多出的 `actor_id` 不影响消费；欢迎/退群消息的唯一所有权仍在 `group_notice`，playhub 不发第二份欢迎。五个反向用例已在 `tests/test_group_events.py` 落成断言：①新正则改变目标识别 → 欢迎缺失或错人；②把邀请人当目标成员 → 双欢迎；③新返回元组未同步解包 → 系统消息解析失败；④改动事件去重或基线 → 重复欢迎；⑤`observe_system()` 重建丢邀请人。

**测试**：`tests/test_group_events.py`（邀请人→`actor_id`、角色词/无操作者置空、目标链接不复用、缺邀请人链接仍产事件、纯文本不猜人、`observe_system` 保真、移出操作者）；`tests/test_playhub_invite.py` 12 例（群级计数、按人归属、`membership` 不归属、`actor == member` 丢弃、重投只记一次、同群两人同日各记一次、别的群忽略、不进活跃榜、非进退群系统消息吞掉、普通消息通路不受影响、`邀请` 卡片用到真实事件）。

**风险（必读）**：`wxbot_framework/` 是上游目录，**这五处改动会产生上游合并冲突风险** —— 上游更新若落在同一区域（`_ACTOR` 定义、`_classify()` 返回值、事件 dataclass 字段、`observe_system()`），每次合并都要人工重解。缓解：补丁**限制在这一个文件**、给解析器固定输入样例与输出断言（`tests/playhub/test_invite.py` 直接调框架解析器，同时钉住 `group_notice` 的消费契约），并把跟进上游的工时计入维护预算。**不改的代价**：只有群级进退人数，按人邀请榜只能显示「暂无可归属邀请记录」。v3 选择改，因为邀请榜是已拍板的 v1 四榜之一。

---

## 9 未决问题（开工前需确认）

按优先级排列，1–2 不定则 B1 之后无法开工。**2026-11-06 状态：第 1、7 条已关闭（已落地 / 已核实为不能），第 2–6、8–9 条已由实现口径消化**（正文各节已按最终口径写定），本清单保留原始问句作为决策留档。v3 已拍板的九条裁决（两币、一层门禁、超级宝宝降级、婚姻四子命令、切磋不联动监狱、四榜、活跃四档、潜水只统计、只拆首个 `#`）已并入正文，不再列在此处；另有一条已定：帮助入口（方案 A，第 61 个词 `帮助`，动作 `business.help`），见 §4 触发词账。

1. **`idiom_chain` 是否允许改 1 个 if（3 行）**（`plugins/idiom_chain/__init__.py:128`）—— **已定并已落地（2026-11-06）**：按方案 A 在守门条件里补 `text in GROUP_CONTROL_COMMANDS`，现为 `if (not game or not text or text.startswith("/") or text in GROUP_CONTROL_COMMANDS or not FOUR_CHINESE.fullmatch(text)): return`；配套用例 `tests/test_idiom_chain.py::test_four_char_control_words_are_ignored_during_chain`（锁住「公布答案」这类四字控制词不被当成本轮答案）。`plugins/idiom_chain/` 在插件目录，**不涉及上游合并**。原三个选项留档：
   - **方案 A（推荐）**：`if (not game or not text or text in GROUP_CONTROL_COMMANDS or not FOUR_CHINESE.fullmatch(text)): return`。零判定损失（`judge.py:39` 本就只认四字），同时是 bugfix（消除接龙期间对每条闲聊的刷屏误回复）。代价：改 3 行 + 2 个用例，需 `from wxbot_framework.text import GROUP_CONTROL_COMMANDS, FOUR_CHINESE`。
   - **方案 B（不改）**：接龙一开局，本群 62 个关键词全部失效且每条闲聊被回一句错误提示 —— 无前缀下这是**不可接受**的产品状态，只在 A 被否时兜底。
   - **方案 C**：停用 `idiom_chain`，由 playhub 重写接龙（+约 2 人日，且要重做 LLM 判定与人设耦合）。
   - 注：`plugins/idiom_chain/` 在插件目录，**不涉及上游合并**；与此相对，§8.7 的 C1 改 `wxbot_framework/group_events.py` **会产生上游合并冲突风险**，两者不可混为一谈。
2. **是否同意 playhub 对 `is_self` 一律 return**（`on_message` 第一行）？这是自触发循环的唯一确定性防线（§2.6.5 论证了框架挡不住四字/白名单词）。代价：**不能用机器人自己的号自测**，自测须换号（验证群里的运营号）。若坚持"自号也能触发"，则必须接受"回包恰好等于触发词时自触发"的残余风险，并额外投入一套基于 `msg_id` 的回包指纹抑制（约 +0.5 人日，且仍有竞态）。
3. **高危词的最终词形是否接受**（§4.3）？具体要确认 4 件：
   - `兄弟` **不注册**、改 `结拜`；
   - `宝宝` **必须带子命令**（`宝宝 看`），裸发完全静默，`超极宝宝` 降级为 `宝宝 超极`；
   - `结婚/闺蜜/雇佣/结拜` 强制参数 **且默认关闭**（群主不开就没有）；
   - `纳妾` **默认永久关闭**（只能控制台改配置）。
4. **冲突处置粒度**：本设计是「`strict=True` 整插件拒绝加载（测试/首装）、`strict=False` 只按引擎禁用（运行期）」；你原话是"拒绝加载该插件"。是否接受运行期"只损失 1 个玩法"而不是"47 项全下线"？
5. **`picture_idiom` 与 playhub quiz 共用 4 个框架裸词**：接受「同一群二选一」（本设计默认，帮助文案说明），还是要求 playhub 必须换一套自己的子命令词（会增加 4 个新词，且与框架词并存时行为更难解释）？
6. **高危裸触发要"完全静默"还是"首次回一次引导"**：本设计是静默（`silent`），中危用"24 h 一次引导"（`guide`）。完全静默的代价是新人不知道有这个玩法；改成"每群首次一次引导"可以让发现成本降低，但会让「宝宝」这种词在群里冒出一条机器回复（正是我们想避免的）。
7. **`聊天 开/关` 是否真的能生效** —— **已核实（2026-11-06）：不能**。`plugins/mention_chat/plugin.json` 只有 `api`/`description`/`message_types`/`requires`/`business` 五个字段，没有开关；`plugins/mention_chat/__init__.py:24-25` 只在启动时读框架的 `members.history_enabled`（为假直接 `ValueError`），不接受群级 kv。即：**@ 就答，群内关不掉**。处理：`聊天` 保持只回说明，并在卡面里明说「那个通道归框架插件 mention_chat 管，本群没有开关：@ 了就答，要关只能停用插件」（`plugins/playhub/engines/lounge.py`）。要在群内开关必须改 `mention_chat` 去读 kv，属框架插件改动，暂不做。
8. **「群空间」控制台页面**是否纳入范围？纳入要改 `wxbot_framework/panel_http.py`（硬编码路由，`:87-234`）+ `webui/app.js`，属框架改动、需单独排期；不纳入则第 4 批只交付群内文本卡片。
9. **纳妾门槛的数值口径（已定 30 钻石）**：全文统一为 **30 钻石**（非扣款，仅余额校验），与 §4.4 数值表一致。原 `300 钻石` 是官方在可充值环境下的定价，本群无充值渠道、钻石产出约 5.5 钻/周，300 钻≈11 个月等于死内容，故等比缩放为 30 钻（≈6 周）。v1 纳妾默认永久关闭且无扣款入口，此值仅在开启该玩法后才生效。

### 9.1 官方无明确规则的玩法（13 项，开工前逐项确认）

这些玩法官方没有可查的规则文本。**其中 `签到`/`抽签`/`开宝箱` 的全部数值已由 §4.4 数值表定案（v3），开工即按该表实现，不再等待确认**；其余项仍按「未确认 → 未实现」处理：触发词已注册但只回用法，不产生任何数值。下表列出的是尚未落地的残留空白。

| # | 玩法 | 待确认的空白 |
|---|---|---|
| 1 | 签到 | **数值已定（§4.4）**：30 + 5×min(连签-1,10)，封顶 80；日界 `Asia/Shanghai`；每满 7 天（连签天数 % 7 == 0）额外 +3 钻石并赠 1 个金箱；断签则连签归 1 |
| 2 | 点歌 | 音频数据源；是否收费；冷却时长；返回形式（文字/链接/文件）；失败是否退费 |
| 3 | 抽签 | **数值已定（§4.4）**：每日 1 次；五档 1/12、2/12、4/12、3/12、2/12；金币 30/20/12/8/5；上上签额外 +3 钻石 |
| 4 | 黄历 | 用本地确定性口径还是指定来源；日期与个人化规则（是否按用户八字/生肖） |
| 5 | 礼物 | 目录由谁定义；价格；库存制还是永久；是否转移价值；收礼方是否有收益 |
| 6 | 天气 | **已定（2026-11-06）**：服务商 = 和风天气 QWeather v7（免费订阅 1000 次/天）；默认城市 = **无**（强制带城市参数，不按 IP 猜）；地点解析 = `geo/v2/city/lookup` 取 LocationID（进程内缓存）；额度 = 免费额度内，10 分钟结果缓存 + 5 分钟熔断兜底；失败文案 = 「天气服务暂时不可用」/ 未配置时「天气服务还没配置」。key 由用户在 `bot_config.json → plugins.playhub.weather_api_key` 填 |
| 7 | 星座 | 查询语法；内容源；更新周期；是否个人化 |
| 8 | 求财神 | 次数；费用；奖励；是否只出文案 |
| 9 | 小黑屋 | **部分已定（§8.2）**：单次上限 60 分钟、每日上限 10 次、到期自动解除、`小黑屋 解除 @某人`、只做插件内软限制不联动框架。剩余：对哪些功能生效（建议：仅 playhub 自有玩法，不拦聊天） |
| 10 | 黑名单 | 是否群级作用域；屏蔽范围；是否保留帮助/申诉；与框架控制命令的边界 |
| 11 | 开宝箱 | **数值已定（§4.4）**：铜箱=每日签到赠 1、银箱=活跃榜周榜前 10 各 1、金箱=连签每满 7 天赠 1；开启免费；金币 50/150/400；钻石 1/20、1/12、1/8 分别 +1、+1、+2 |
| 12 | 潜水统计 | **部分已定（§4 表项 41）**：默认 7 天、只统计不清人、数据源 `activity_daily`+`member_seen`。剩余：「从未发言」的基准（建议按 `member_seen` 无记录计）、退群/机器人是否排除、结果条数上限（建议 20 条） |
| 13 | 智能聊天 | 保留既有聊天插件还是 Playhub 提供；真实开关；模型接入方式；触发条件与费用 |

外部依赖：**天气已定**（和风天气 QWeather，2026-11-06，配置见 §4.4「天气」行；key 待用户申请后填入 `bot_config.json`）；仍**未定**的是点歌音频源/费用/冷却、星座内容源（本地静态表即可）、智能聊天复用 `mention_chat` 还是新接（§9 第 7 条已核实不能群内开关）、礼物物品表由谁定义（v1 已按建议内置 5 档固定物品、20–520 金币，见 §4.4）。

