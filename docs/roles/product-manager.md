# 产品操作手册（Product Manager）

## 1. 角色定位
对**指标语义治理的产品价值、需求与验收**负总责：指标注册/版本/发布闭环、血缘影响分析、冲突仲裁、质量规则与基准对账、用户赋能与路线图。

## 2. 核心职责
- 牵引指标从定义 → 评审 → 发布 → 废弃 的全生命周期。
- 用血缘与冲突能力控制指标膨胀与口径分歧。
- 用质量能力与外部基准对账保证指标可信。
- 验收 14 个领域服务的交付（13/13 门禁），推进 §1.5 人工 ratify。

## 3. 能力 ↔ 模块映射
| 产品能力 | 服务 / FR | 关键端点 |
|----------|-----------|----------|
| 指标定义与版本发布 | semantic（FR-01） | `POST /api/v1/metric-definitions`、`/publish`、`/deprecate`；PII 发布需合规复核 |
| 字段/表级血缘 | lineage（FR-04） | `POST /api/v1/lineage/parse`、`/impact`、`/edges`；影响分析 BFS 深度≤5、max_edges≤5000 |
| 冲突仲裁 | conflict（FR-09） | `POST /api/v1/conflicts/check`、`/arbitrate`、`/escalate`、`/close`；状态机 OPEN→NEGOTIATING→ESCALATED→RULED→CLOSED |
| 权限治理 | governance（FR-11） | 授权/回收/权限检查/PII 复核/分级重扫 |
| 质量规则与事件 | quality（FR-10） | 规则 CRUD、事件状态机、静态阈值检测；`/quality/rules`、`/quality/events` |
| 外部基准对账（D11） | quality（FR-10） | `POST /quality/benchmarks/import`（幂等）、`/benchmarks/{id}/bind`、`POST /quality/reconciliation/run`、`GET /quality/reconciliation-records`、`POST /quality/reconciliation-records/{id}/confirm` |
| 口径对账 | dimension（FR-07） | `POST /api/v1/dimensions/reconciliations`（同源） |
| 指标发现与推荐 | recommend / assetmap | `GET /api/v1/recommend/metrics`、`/api/v1/assetmap/summary` |
| 术语治理 | glossary（FR-06） | `POST /api/v1/terms`、冲突、关系 |

## 4. 外部基准对账验收口径（D11，2026-08-11）
- **导入幂等**：同一 `(source_id, metric_code, bench_date, dims)` 重复导入为更新，不产生重复行。
- **对账判定**：差异率 `|观测-基准|/基准*100` ≤ 容忍率 → OK；≤ 2× 容忍率 → WARN；否则 ALERT；ALERT 自动发 `reconciliation.alert` 事件。
- **Owner 确认**：差异经 `reasonable`（合理）或 `caliber_error`（口径有误→走变更）闭环，记录 `confirmed_by`/`checked_at`。
- **可见性**：基准列表与对账差异记录均对读角色开放；导入/绑定/执行需写权限，确认需治理权限。
- 测试：单测 7 项 + 安全测 8 项全绿，mypy --strict 0 error，迁移 0014 可逆。

## 5. 验收标准与状态
- 14 个领域服务均达 `released`（13/13 机械门禁全绿，§6.3 双视角 0 High）。
- 唯一缺口：**§1.5 人工 ratify**（须人工签名，agent 不可代签），由本角色组织 ratify。

## 6. 路线图（诚实提示）
- FR-10 已交付子集：规则 CRUD + 事件状态机 + 静态阈值 + **外部基准对账（D11）**。
- 仍规划待实现：动态基线、同环比、跨源、修复建议。
- 推荐/资产地图等只读聚合能力已 released，待产品侧推广与用户培训。
