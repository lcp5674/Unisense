# Runbook：AI 问数服务（ai）

> 模块状态：`implemented`（门禁 11/13 绿；runbook 已建；§6.3 双视角审查进行中；待 perf 压测 + §1.5 人工 ratify）
> 关联文档：TD §12.7、FR-14、docs/CHANGELOG_MODULES.md

## 1. 职责边界

AI 问数服务负责：

- 语义锚定式自然语言转 SQL（`nl2sql`）：将自然语言中的**已注册词汇**（发布术语名 + 指标编码/名称）锚定为 SQL 片段，组装为对 `unified_metric` 视图的受限查询
- 注入防护（基于禁用词 + 词汇白名单双重校验）
- 执行委托：标注 `execute=True` 时由消费层执行（一期仅生成 SQL，执行见 §8）

**依赖**：MySQL（只读 `term` / `metric` 取词汇表）、audit 服务（写审计）。
**一期明确不做**：真实 LLM/NL 理解、复杂 join/聚合生成、执行结果回填。

## 2. 关键端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/ai/nl2sql` | 自然语言转 SQL（write，body `nl_query` / `execute`） |

## 3. 状态机与留痕

无状态机。`nl2sql` 写审计（`detail={"safe": bool}`，`entity_id` 取查询前 64 字符）。

## 4. 业务语义（关键）

`nl2sql` **不是 LLM 推理**，而是词汇锚定：

1. 取已发布词汇集合（term.name + metric.metric_code/name）。
2. 将 `nl_query` 与词汇做子串匹配，命中的词汇作为 SQL `SELECT` 锚点。
3. 组装 `SELECT <锚点> FROM unified_metric WHERE 1=1`，并做禁用词校验（`;`、`--`、`/*`、`select *`、`delete`、`drop` 等）。
4. `safe=False` 或含禁用词 → 返回安全拒绝（`200` 但 `sql=None`），**不抛异常**。

> **产品视角重要限制**：该能力本质是「受限词汇查询构造器」，并非真正的自然语言问答（见 §6.3 / §8）。对未注册词汇的提问无法理解，返回空 SQL。

## 5. 依赖与故障

| 依赖 | 故障表现 | 处置 |
|------|----------|------|
| MySQL（term / metric 只读） | 5xx | 确认上游迁移 `upgrade head`；连接池 |
| 词汇表为空 | 所有查询返回空 SQL | 确认术语/指标已 PUBLISHED |
| RBAC（write=analyst/viewer） | 403 | 确认调用方角色 |

## 6. 迁移与回滚

- 本服务**无自有迁移**（仅只读 term/metric）。
- 回滚：代码用 K8s 版本回退；无 schema 变更。

## 7. 可观测性

- 结构化日志：未启用（仅审计落盘）。
- 审计：`nl2sql` 写 audit（含 safe 标志）。
- 监控重点：命中率（`safe=True` 占比）、拒绝率。

## 8. 已知限制（一期）

- **非真实 NL 理解**：仅靠词汇子串锚定，未注册词汇/复杂问法无法处理（产品契合度风险，§6.3 重点项）。
- `ask(..., execute=True)` 一期**不实际执行**，仅附 `note`（静默声称 vs 实际未执行，见 §6.3）。
- 注入防护依赖词汇白名单 + 禁用词子串；若注册词汇名本身含特殊字符未校验，存在绕过风险（§6.3）。
- 无结果缓存、无查询复杂度限制，极端长查询有资源风险。
- 性能基线未单独压测。
