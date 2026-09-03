# dp 调度血缘接入 — 技术方案（v0.2，待最终 review）

> 状态：草案。对应决策 `spec/dp-lineage-ingest/decisions.md`（D1–D10 + 收尾 3 默认值）。
> v0.2：补 D10（任务/节点元数据承载清单 + 资产 Owner 回填策略），新增 §4.4、恢复 §4.5；§3.1 加 owner_backfill；§11 开放项更新（gmt_modified 实测、115 产出表已资产化、影子用户创建细节）。
> 批准后按 TDD + 独立提交推进。

---

## 1. 目标与范围

将本地 dp 数据源（id=4, mysql，`dp_stable` 数开/调度平台元库）中**所有开发任务的全部 SQL 节点**解析为血缘，写入现有血缘模块；支持字段级全链路、任务信息旁路、LLM 分级确认与人工抉择、前端可视化配置与运维、生产准实时增量更新。

**入图范围**：type=1（SQL 任务）+ stype=7（Hive/Spark SQL 节点）的 `script_info`；过滤明显临时表（`tmp/temp/_bak/adhoc`，规则可配置）；DataX（stype=2）/shell/质量节点跳过（可配置开关决定是否解析 DataX JSON 的 table 映射——本期不做，留扩展）。

## 2. 复用与落点（已核对）

| 能力 | 落点 | 复用方式 |
|---|---|---|
| SQL 确定性解析（L1 表级 + L2 字段级 + DDL） | `backend/app/services/lineage/parser.py` | `extract_table_lineage` / `extract_field_lineage` / `extract_ddl_lineage`（多语句由 `_split_statements` 拆分） |
| 血缘入库（表级 + 字段级 + 运行记录） | `backend/app/services/lineage/service.py` | `parse_and_store(req, actor_id)`（`LineageParseRequest.sql/dialect/provenance/target_table`）；**已内置**建边、`upsert_edge_with_status`、`lineage_ingest_run` 运行记录 |
| 边失效/删除语义 | `backend/app/models/lineage.py` | `LineageEdge.last_seen_at / missing_count / stale / stale_since` —— **节点删除 → 边不再被确认 → 自动走 stale 保留历史**（收尾默认值 2 零新逻辑） |
| 周期任务 | `backend/app/services/collector/worker.py` | `functions = [...]` 聚合 + arq `cron`；新增任务模块 `app/services/lineage/dp_sync_tasks.py` |
| 分布式锁 | `backend/app/tasks/lock.py` | 复用 `_guard_once` 语义防多副本并发轮询 |
| LLM 客户端 | `app/services/llm/client.py`（现有路由/熔断/空 content failover） | 共识确认/仲裁/兜底/语义增强统一走 LlmClient |
| 前端血缘页 | `frontend/src/App.tsx` `/lineage` → `LineageView.tsx` | 同路由新增子 Tab（配置 / 待抉择 / 运维），权限 `RequirePerm` 沿用 |
| 权限点注册 | `backend/app/services/governance/policy.py` | 新增 `lineage:sync`（配置/运维）、`lineage:resolve`（抉择）两个动作点 |

## 3. 数据模型（新增 5 表，均走 alembic 迁移）

### 3.1 `dp_sync_config`（同步配置，单行语义 + key-value 可扩展）
| 列 | 类型 | 说明 |
|---|---|---|
| id | PK | 单行（id=1） |
| enabled | bool | 同步总开关（停用不再轮询/解析，血缘保留） |
| source_id / schema_name | int / str | dp 数据源 id（默认 4）/ 库（默认 dp_stable） |
| task_table / step_table | str | dispatch_task / dispatch_task_step（允许换表） |
| poll_interval_minutes | int 1–60 | 轮询间隔，默认 5（**前端可配置**） |
| task_type_filter / step_type_filter | JSON | 默认 `[1]` / `[7]` |
| exclude_task_patterns / exclude_table_patterns | JSON | 排除规则（任务名/目标表前缀正则，默认 tmp/temp/_bak/adhoc） |
| llm_enabled | bool | LLM 开关（关 = 纯 sqlglot，复杂/失败节点全进待抉择） |
| llm_complexity_rules | JSON | 分级特征规则（子查询深度/CTE 数/窗口/多 join/方言特征/告警阈值，可调） |
| llm_model | str | 模型（沿用平台 LLM 配置，默认空=平台默认） |
| resolve_memory_enabled | bool | 待抉择记忆复用开关（默认开，见 6.4） |
| owner_backfill | str enum | 资产 owner 回填策略：`orphan_only`（默认，仅孤儿回填）/ `never`（不回填，只存 dp_task_refs）——见 4.4（D10） |
| updated_by / updated_at | — | 审计 |

### 3.2 `dp_sync_watermark`（增量水位）
| 列 | 说明 |
|---|---|
| table_name（task/step） | 分表记录 |
| last_max_update | 上次扫描到的最大 `update_time`（实施时核对两表确有 update_time 列；若无则按主键 id 水位 + 创建时间双保险） |
| last_scan_at / last_full_scan_at | 上次扫描/上次全量 |
| reset 语义 | 运维动作「重置水位」→ 置空触发下轮全量（D8 运维区） |

### 3.3 `lineage_field_mapping`（字段映射独立表 —— D2 一等查询对象）
| 列 | 类型 | 说明 |
|---|---|---|
| id | PK | |
| edge_id | FK→lineage_edge.id | 所属表级边（便于边删查联动；字段级聚合到表级边下） |
| source_table / source_column | str | 源（列 NULL=表级降级占位，见 degraded） |
| target_table / target_column | str | 目标 |
| expression | Text NULL | 表达式（聚合/计算列时非空且 source_column=NULL → **表达式列无直接字段来源**，标表级） |
| degraded | bool | `SELECT *` 等无法枚举字段的降级标记 |
| confidence / provenance | float / str | high+sqlglot / high+sqlglot+llm / low+llm（参考边不落正式？见 5.3——**low 只在用户采纳后落**） |
| sql_hash | str | 来源节点 SQL 指纹（见 6.4 记忆） |
| task_id / step_id | bigint | 来源任务/节点（审计追溯） |
| 唯一索引 | | uq(source_table, source_column, target_table, target_column, degraded) 防重；ix(target_column) 支持按字段反查 |

### 3.4 `dp_resolution_ticket`（待抉择单 —— D9）
| 列 | 类型 | 说明 |
|---|---|---|
| id | PK | |
| task_id / step_id | bigint | 来源 |
| sql_text | Text | 原始 script_info（9d：无法解析时**展示节点内容供手动配置**） |
| sql_hash | str + idx | 内容指纹（裁决记忆 key） |
| status | enum | `diverged`（sqlglot/LLM 不一致）/ `llm_fallback`（sqlglot 失败 LLM 兜底）/ `unparseable`（双方都失败）/ `pending`/`resolved`/`ignored` |
| sqlglot_result | JSON | 表级+字段级边候选 |
| llm_opinion | JSON | LLM 意见（agree/纠正/兜底流转/无法提炼） |
| divergence_reason | Text | 不一致原因（LLM 给的） |
| resolution | enum NULL | `accept_sqlglot` / `accept_llm` / `manual`（手动修正边/字段映射）/ `ignore` |
| manual_edges_json | JSON NULL | 手动配置的边/字段映射（manual 时） |
| resolved_by / resolved_at | — | 留痕 |
| 唯一约束 | | uq(step_id, sql_hash) —— SQL 未变不重复进单（6.4） |

### 3.5 `dp_sync_run_log`（每轮扫描运行记录 —— D8 运维区，与 LineageIngestRun 职责分离）
| 列 | 说明 |
|---|---|
| run_at / status | running/success/failed |
| scanned_tasks / scanned_steps | 本轮扫描量 |
| parsed_ok / llm_confirmed | sqlglot 直入 / LLM 确认一致 |
| diverged / llm_fallback / unparseable | 三态待抉择产生数 |
| tickets_created / tickets_resolved / errors | 抉择单新增/裁决/错误 |
| llm_calls / duration_ms | 成本与耗时 |
| detail_json | 快照（每 step 结果摘要，可下钻） |

**不动** `lineage_edge` 表结构（边 provenance 保持通道语义 `dp_sql`；任务静态身份见 4.3 落 `dp_task_refs`——若需落边，选加 `LineageEdge.dp_task_refs` Text JSON，理由见 4.3）。

## 4. 解析管线

### 4.1 单节点（step）处理流程
```
script_info ─┬─ sqlglot 多语句拆分 + extract_table/field/ddl_lineage
             │    （每段 CTAS/INSERT 的目标表 = 中间表/产出表，源表 = 该段读取；
             │      中间表天然串链：上游边 target = 下游边 source）
             │
             ├─ [简单] 无 L3 特征、干净解析 → 直接入库（high / sqlglot）
             ├─ [复杂] 命中分级特征 → LLM 确认：
             │     ├─ agree → 入库（high / sqlglot+llm）
             │     └─ disagree → 建待抉择单（diverged，附 sqlglot 结果 + LLM 意见 + 原因）
             └─ [失败] ParseError / 空边但 SQL 非空 → LLM 兜底：
                   ├─ 提炼出流转 → 建待抉择单（llm_fallback，低置信参考）
                   └─ 提炼不出 → 建待抉择单（unparseable，展示原文供手动配置）
```
**产出表归属**：任务 `out_table` 作为最终产出标记；节点脚本内 create/insert 目标是中间表。血缘只需**全部 step 的边入库**即自动形成全链路（无需额外串链逻辑）；`dp_task_refs` 记录每个边来自哪些 task#step。

### 4.2 复杂度分级（触发 LLM 确认的特征，规则可前端配置）
默认：子查询嵌套 >2 / CTE 数 >3 / 含窗口函数 / JOIN 数 >4 / 含 `set` 变量且展开后有非字面量 / sqlglot 产生告警 / 字段级出现 degraded 或「表达式列」>0。命中任一 → 复杂。

### 4.3 写入语义（D4 落库映射）
- **同任务多节点产同一表 / 跨任务产同一表**：`parse_and_store` 的 `upsert_edge_with_status`（唯一索引 uq_lineage_edge）天然合并——同 (source,target,edge_type,granularity) 复用同一条边；
- **dp_task_refs 聚合（静态快照 + 增量顺刷，D10）**：`LineageEdge.dp_task_refs`（Text JSON）承载任务/节点**静态身份与准静态元数据**：
  ```json
  [{ "task_id": 1386, "task_no": "...", "task_name": "...", "out_table": "...",
     "step_id": 5012, "step_name": "...", "task_node_type": 2, "task_step": 3,
     "director": "licp", "created_user_id": null, "modified_user_id": null,
     "checker": null, "settle_project_director": null,
     "project_id": null, "settle_project_name": "...", "settle_department_name": "...",
     "budget_unit_name": "...",
     "cycle": "day", "cron_express": "...", "week_day": null, "month_day": null,
     "specific_time": null, "frequence": null,
     "remark": "...", "task_version_desc": "...", "task_version": 3,
     "master_task_id": null, "is_master_task": false }]
  ```
  upsert 时对既有边 read-modify-write 追加去重（或 SQL `JSON_MERGE_PATCH`）；**任务 id 不变但静态字段变更（director/调度/描述等）→ 本轮增量解析顺手刷新**该任务所有边的 dp_task_refs（边本身不动、不因字段变化重建）；字段映射独立表每行带 task_id/step_id 直接落，无聚合问题；
- **动态运行态不落边**（D3/D10）：`task_state`/`run_status`/`run_time`/`gmt_submit` 每次执行都变，不写 dp_task_refs——前端边详情展示时实时 `SELECT status, last_run_at FROM dp_stable.dispatch_task WHERE id=?` 旁路（经 dp 数据源只读连接）；
- **与既有血缘融合**：同源同目标的既有边（可能来自 Hive 采集）→ upsert 复用，仅合并 provenance/dp_task_refs；**不建并行边**；
- **幂等**：边 = 唯一索引 + upsert；字段映射 = 唯一索引 + insert-on-duplicate；待抉择单 = uq(step_id, sql_hash)；运行记录每轮一条——重复轮询不产生重复数据；

### 4.4 资产 Owner 回填（D10，防孤儿）
解析产出表 `out_table` 落血缘后，顺带对**产出表资产实体**执行 owner 回填：
- **范围**：仅当 `db_catalog.owner_id IS NULL`（孤儿）时回填——已被人工认领/治理的资产**绝不覆盖**（配置项 `owner_backfill = orphan_only（默认）/ never`）；
- **director → 平台用户**：优先按 `user.username = dp_director` 匹配；匹配不到 → **自动创建影子用户**（`username=dp_director` 唯一、`display_name` 初始占位、`status=DISABLED` 不可登录、归属「外部协作」组织）后回填 `owner_id`；管理员后期在平台用户管理配置真实中文用户名（配置后资产卡片自动显示中文，现有 `display_name||username` 展示逻辑天然生效）；
- **不做独立 dp_user_mapping 映射表**（Q10c 收敛）；回填动作留痕（创建影子用户记录 + 资产更新记录），不进孤儿资产列表。

### 4.5 删除语义（收尾默认 2）
节点/任务删除 → 其产出的边在本轮不再被确认（未出现在任何 step 解析结果）→ `missing_count+1`；达阈值（沿用现有失效管理）进 stale 保留历史。**零新逻辑**，复用 `LineageEdge` 现有失效机制。

## 5. LLM 协议

### 5.1 共识确认（复杂节点）
输入：原始 SQL（多语句全量）+ sqlglot 提取的表级/字段级边 JSON。
输出（JSON，一次调用）：
```json
{ "agree": true|false,
  "missing_edges": [{"target":"db.t","source":"db.s","field_mappings":[["s.c","t.c"]]}],
  "wrong_edges":   [{"reason":"...", "target":"...", "source":"..."}],
  "reason": "..." }
```
- agree=true → 入库；agree=false → 建 diverged 单，`divergence_reason`=reason。
- Prompt 强约束：只许报告与 sqlglot 的差异，不许复述；输出必须合法 JSON；给出「无法判断」分支。

### 5.2 兜底（sqlglot 失败）
输出：`{target_tables:[], source_tables:[], field_mappings:[[...]], confidence:"low", note}` → 建 llm_fallback 单，**不直接写正式血缘**（防幻觉污染权威），用户采纳才落（采纳时 confidence=low / provenance=llm）。

### 5.3 语义增强（可选开关）
复杂/中间表无注释时，LLM 补一句业务语义进 `dp_task_refs` 或边 description（不参与判定）。

### 5.4 成本边界
- 简单节点零 LLM；预计 LLM 调用量为全量 20–40%（D6）；
- 单轮 `llm_calls` 预算上限（默认 200 次/轮，超限本节点跳过进 llm_fallback 单 + run_log 记录）；连续失败熔断跳过并记录（沿用 LlmClient 熔断）；
- 每轮 LLM 调用量在 run_log 可见（运维区）。

## 6. 待抉择工作台（D9）

- **列表**：状态筛选（diverged / llm_fallback / unparseable / pending / resolved / ignored）+ 任务/表关键字 + 分页；
- **详情**：SQL 原文（可复制）｜sqlglot 边候选表｜LLM 意见卡（agree/纠正/兜底流转/无法提炼 + 原因）三栏对照；unparseable 显示原文 + 手动配置表单（手填边：源表→目标表 + 字段映射行编辑）；
- **操作**：采纳 sqlglot / 采纳 LLM / 手动修正（边与字段映射可编辑后保存）/ 忽略节点（不写血缘标已处理）；留痕 resolved_by/resolved_at/resolution；
- **记忆复用**（6.4）：裁决时把 (step_id, sql_hash, resolution, manual_edges_json) 存单；后续轮询同 step 同 sql_hash → 直接复用上次裁决（自动采纳/忽略），不进待抉择；SQL 变化（新 sql_hash）才重新裁决；
- **无法解析归档**：双方失败 → unparseable 单展示原文，用户可手动配置或标忽略；不无限重试。

## 7. 轮询与增量（D7 + 收尾默认 3）

- **载体**：arq worker 新增 `dp_lineage_poll_task`（`backend/app/services/lineage/dp_sync_tasks.py`，注册进 worker.py `functions`）；
- **动态间隔**：注册**每 1 分钟轻量 ticker**（不用静态 cron 间隔），读 `dp_sync_config.poll_interval_minutes` 判断距上次 `last_scan_at` 是否到点 → 执行扫描（间隔改配置即时生效，无需重启 worker；1 分钟 ticker 空转成本可忽略）；
- **锁**：`tasks/lock.py` 分布式锁（`dp_lineage_poll` key，TTL 覆盖单轮时长）防多副本并发；
- **增量扫描**：按 watermark（task/step 的 update_time 增量，实施时核对列存在性，无则 id+created_at 双保险）；检出新增/修改的 SQL 节点 → 入队解析（与现有 run_collection_task 同队列模式，或 ticker 内顺序处理 + 超时拆分）；
- **全量重置**：运维「重置水位」→ 置空 → 下轮全量扫描（幂等安全）；
- **每轮收尾**：写 `dp_sync_run_log`；边 last_seen 更新触发 stale 机制（4.4）。

## 8. 前端（血缘模块内，方案 A）

`LineageView.tsx` 或同路由新增 `/lineage/sync` 子页，三个 Tab：

| Tab | 内容 | 权限 |
|---|---|---|
| **同步配置** | 总开关 / 轮询间隔（1–60 分钟滑条）/ 任务·节点类型过滤 / 排除规则（Tag 编辑）/ LLM 开关与分级规则（高级折叠）/ 保存即生效 | `lineage:sync`（可读）+ 写需 `lineage:sync` 管理 |
| **待抉择** | 列表 + 详情三栏对照 + 操作按钮（采纳/忽略/手动配置）+ 记忆复用说明 | `lineage:resolve` |
| **运维** | 水位查看/重置（触发全量）、运行记录表（每轮 scanned/parsed/diverged/llm_calls/duration，可点看 detail_json）、依赖实时状态 | `lineage:sync` |

**动态任务信息旁路展示**：血缘边详情（现有 LineageView 边详情）若 `provenance=dp_sql`，额外渲染「调度来源」卡：dp_task_refs 静态信息 + 实时旁路查询（状态/最近执行）→ 展示「由任务 X（负责人）产出，状态=运行中，最近执行 10:30」。

## 9. 权限

- 新增动作点：`lineage:sync`（配置页读写 + 运维动作）、`lineage:resolve`（待抉择操作）；
- 只读查看待抉择/配置：血缘查看权限（现有 `/lineage` 查看权限）即可，写操作需对应动作点；
- dp 数据源只读连接：复用数据源权限（owner/admin），LLM 调用走平台额度。

## 10. 测试与部署计划

**单元测试**（每模块独立提交）：
1. 模型 + 迁移（5 表 + LineageEdge.dp_task_refs）+ 唯一索引/幂等；
2. 解析管线：简单/复杂/失败三态路由；多语句拆分；中间表串链；临时表过滤；分级特征判定；
3. LLM 协议：agree/disagree/兜底/无法提炼四分支（mock LlmClient）；JSON 解析容错；
4. 写入：聚合（同表多任务合并边 + dp_task_refs 合并 + 字段映射聚合）、与既有边复用、幂等重跑 0 重复；
5. 轮询：watermark 增量、动态间隔判断、锁防并发、stale 触发、删除不物理删边；
6. 抉择工作台：四操作、留痕、记忆复用（同 hash 不再进单）、手动配置入库；
7. API + 前端组件测试（配置读写/抉择操作/运维页）。

**集成/真实验证**：本地 dp 源取真实 task/step 子集端到端（sqlglot 全量 + LLM 确认抽样），核对血缘图边与字段映射；模拟节点变更验证增量与 stale。

**部署**：迁移 `alembic upgrade head`；worker 注册新任务（镜像重建自动带）；默认 enabled=false（**默认不启用**，避免未配置即扫描 dp 源）；首次启用走配置页开启 + 全量扫描按钮。

## 11. 开放项（实施中确认，不阻塞方案）

- ✅ `dispatch_task`/`dispatch_task_step` 的更新时间列：实测两表均有 **`gmt_modified`**（watermark 用 `gmt_modified` 增量；删除标志列实施时核对，非删除任务 ~1390 暗示存在软删）。
- ✅ 产出表资产化：实测 **115 张 `wedw_dwd.dp_*` 已在资产目录**（owner 回填是更新既有实体，非先建实体）；平台用户仅 4 个 E2E 账号 → director（拼音缩写，前 15 人覆盖 33~142 任务）基本都需走影子用户创建（Q10a）。
- ⚠️ **影子用户创建细节**：实施时核对 `user` 表组织/角色约束（归属「外部协作」组织需存在或自动创建）、`username` 唯一冲突处理（dp 账号若已存在则直接复用）、创建动作权限与审计（由同步服务以系统身份执行，留痕记录）。display_name 初始占位（dp 账号），管理员在用户管理配置中文名。
- stype=7 script_info 中 `set` 变量展开已有 `expand_variables`（parser.py L182），确认覆盖 dp 脚本形态。
- 中间表是否全量入图 vs 仅任务最终产出表入图：已定「节点级全链路」（中间表也入图），但**明显临时表**按排除规则过滤（收尾默认 1），默认规则实施时按 dp 实际中间表命名校准。
- 字段映射表 edge_id 外键在「同边多任务聚合」下的归属（字段映射行来自不同 step 但同边）——edge_id 指向聚合后的边，task_id/step_id 行内保留来源。
