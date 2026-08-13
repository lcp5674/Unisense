# Runbook：资产地图服务（assetmap）

> 模块状态：`released`（TD §12.11 / FR-18；生产级补强 + 产品补充已完成，门禁全绿）
> 关联文档：TD §12.11、docs/CHANGELOG_MODULES.md、docs/module-status.yaml

## 1. 职责边界

资产地图服务（**只读聚合 + 资产工作台视图**）负责：

- 资产目录总览（`catalog_summary`）/ 分类（`classification_summary`）/ 指标（`metric_summary`）
- 表 / 视图清单与孤儿资产识别（`orphan_assets`）
- **实体详情**（`entities/{id}`）：schema 摘要、敏感度、PII 标记、血缘边明细、关联指标、源健康、新鲜度
- **图谱视图**（`graph`）：Neo4j Cypher 优先，MySQL 血缘边拼接降级
- **热力视图**（`heatmap`）：按 domain / sensitivity / owner / dw_layer 聚合
- **责任人视图**（`owner-view`）
- **产品补充（FR-18 生产化）**：
  - 全局搜索（`search`）：目录 + 指标统一结果，LIKE 通配符转义防模糊放大
  - 资产健康（`health`）：不健康源 / schema 不完整 / 孤儿 / 陈旧资产（7 天未更新）
  - PII 合规视图（`pii`）：按敏感级 / 域聚合 PII 资产
  - 变更追踪（`changes`）：最近 N 天新增 / 变更的目录与指标
  - 我的资产（`my-assets`）：当前登录用户负责的目录与指标
  - CSV 导出（`export.csv`）：目录资产清单（UTF-8 BOM，Excel 兼容）

**依赖**：MySQL（只读聚合 `db_catalog` / `metric` / `lineage_edge` / `classification` / `data_source`）、Neo4j（可选，图谱优先读，不可用降级 MySQL）。

## 2. 关键端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/assetmap/summary` | 目录总览 |
| GET | `/api/v1/assetmap/classification` | 敏感级分类 |
| GET | `/api/v1/assetmap/metrics` | 指标清单 |
| GET | `/api/v1/assetmap/tables` | 表 / 视图清单（source_id/sensitivity 过滤） |
| GET | `/api/v1/assetmap/orphans` | 孤儿资产 |
| GET | `/api/v1/assetmap/entities/{entity_id}` | 实体详情（血缘边/关联指标/源健康/新鲜度） |
| GET | `/api/v1/assetmap/graph` | 图谱（domain/depth/pii_only） |
| GET | `/api/v1/assetmap/heatmap` | 热力（dimension） |
| GET | `/api/v1/assetmap/owner-view` | 责任人视图（owner_id） |
| GET | `/api/v1/assetmap/search` | 全局搜索（q/type/limit） |
| GET | `/api/v1/assetmap/health` | 资产健康视图 |
| GET | `/api/v1/assetmap/pii` | PII 合规资产视图 |
| GET | `/api/v1/assetmap/changes` | 变更追踪（days/limit） |
| GET | `/api/v1/assetmap/my-assets` | 我的资产 |
| GET | `/api/v1/assetmap/export.csv` | 资产 CSV 导出 |
| POST | `/api/v1/assetmap/entities/{id}/owner` | 认领/转让归属（owner_id=None 解除） |
| POST | `/api/v1/assetmap/entities/{id}/sensitivity` | 重分类敏感级（枚举值） |
| POST | `/api/v1/assetmap/batch-owner` | 批量认领/转让（≤200） |
| POST | `/api/v1/assetmap/batch-sensitivity` | 批量重分类（≤200） |

## 3. 安全边界

- 读端点：`Depends(require_roles(*_READ_ROLES))` + `Depends(guard_against_injection)`（RBAC 闸门 + 注入守卫）。
- **写端点**（2026-08-13 新增）：仅 `platform_admin` / `domain_admin`（`_WRITE_DEPS`），非写角色 403；写入与审计同事务原子提交（PLAT-3）。
- 敏感字段剥离：`models/base.py::_SENSITIVE_FIELDS` 序列化黑名单（`connection_config` / `password` / `secret` / `token` / `credential` / `etl_sql` / `schema_json`），详情接口 `etl_sql` 恒为 `None` 不外泄。
- `sensitivity_level` 为资产地图核心展示字段，**不在**黑名单内（前端敏感度列依赖）。
- Neo4j 图谱读经 `CircuitBreaker`（阈值 5，复位 30s），故障降级 MySQL 不抛错。
- 写操作目标用户须存在（`user_exists` 校验），防孤儿归属脏数据；批量空列表 422。

## 4. 业务语义

- 实体详情血缘匹配节点编码形态：`entity_name` / `table:{name}` / `field:{name}`。
- 关联指标：血缘下游指向 `metric:` 前缀节点的边（指标级血缘）。
- 搜索 LIKE 通配符转义：`%` / `_` 作为字面量匹配，防全表模糊放大；按类型分流查询（table/field 只查目录，metric 只查指标）。
- 陈旧资产阈值：7 天未更新（`updated_at < now-7d`），可通过 `stale_days` 配置。
- Neo4j driver 惰性单例复用（`_get_neo4j_driver` / `_close_neo4j_driver`），防每请求连接泄漏。
- 聚合端点（summary/classification/metrics/heatmap/health/pii）经 **cache-aside 缓存**（TTL 30s + 熔断），Redis 不可用时静默回源，不阻断主链路。

## 5. 依赖与故障

| 依赖 | 故障表现 | 处置 |
|------|----------|------|
| MySQL（上游表只读） | 5xx | 确认上游迁移 `upgrade head`；连接池 |
| 上游数据缺失 | 清单 / 覆盖为空 | 确认上游服务正常写入；非本服务故障 |
| Neo4j 不可用 | 图谱降级 MySQL（`graph_written=false` 或空边） | 熔断自动降级，无需人工干预；恢复后自动回归 |
| RBAC（read 角色） | 403 | 确认调用方具备读权限 |

## 6. 迁移与回滚

- 本服务**无自有迁移**（仅聚合上游表），无 `downgrade` 影响。
- 回滚：若上游 schema 变更导致聚合异常，回退上游迁移即可；本服务代码回滚用版本回退。

## 7. 可观测性

- 读端点依赖 API 层 RED 指标（`/metrics`）。
- **写审计**（2026-08-13 新增）：`ASSET_ASSIGN_OWNER` / `ASSET_RECLASSIFY` / `ASSET_BATCH_ASSIGN_OWNER` / `ASSET_BATCH_RECLASSIFY` 写入 `audit_log`（actor/entity/detail，PLAT-3 原子提交）。
- 数据新鲜度取决于上游写入频率；资产健康视图（`/assetmap/health`）提供不健康源 / 陈旧资产一览，应结合上游健康判断。

## 8. 已知限制（记录在案）

- **perf 基线已实跑入库**（2026-08-13，`assetmap_perf_2026-08-13.txt`）：k6 v2.1.0 压真实业务端点（带鉴权），10 VU / 30s / 2700 请求，**P95 = 19.97ms**（契约 <800ms）、失败率 0.00%。聚合端点命中缓存（TTL 30s）。
- **图谱边上限**：Neo4j 边 LIMIT 1000、MySQL 边 LIMIT 1000，超大图会截断（有界返回，避免响应爆内存）。
- **大规模（>10 万资产）搜索为 LIKE 前缀**：建议后续接 ES 全文检索；聚合已有 Redis 短缓存，超大规模可换物化表。
- **数据新鲜度依赖上游**：上游延迟会静默反映为陈旧资产地图（非故障，需监控上游）；异常修复需回到上游服务。

## 9. 前端入口

- 页面：`frontend/src/pages/AssetMap.tsx`（11 个 Tab：概览 / 搜索 / 图谱 / 热力 / 资产健康 / PII 合规 / 变更追踪 / 我的资产 / Owner / 孤儿 / 数据表）。
- 数据表 Tab 支持敏感度过滤 + CSV 导出 + 实体详情抽屉（血缘边明细 / 关联指标 / 源健康 / 新鲜度）。
