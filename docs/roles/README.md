# 角色操作手册（四角色）

本目录面向 Unisense 指标语义中台的四类关键运营角色，沉淀各自的核心职责、日常操作、能力映射与应急响应。每份文档均对齐平台已实现的能力（端点 / 服务 / 迁移），避免与产品文档脱节。

| 角色 | 文档 | 一句话定位 |
|------|------|-----------|
| 安全官 | [security-officer.md](security-officer.md) | 数据安全、PII 分级、权限治理、审计与被遗忘权 |
| DBA | [dba.md](dba.md) | 存储、迁移可逆、备份恢复、性能与容量 |
| 运营（SRE） | [operations.md](operations.md) | 可用性、监控告警、混沌韧性、发布回滚 |
| 产品 | [product-manager.md](product-manager.md) | 指标治理流程、血缘/冲突/质量验收与路线图 |

> 所有接口路径以 `/api/v1` 为前缀；能力对应 14 个领域服务的 runbook（`docs/runbooks/`）。
> 模块交付状态见 `docs/module-status.yaml`，变更审计见 `docs/CHANGELOG_MODULES.md`。
