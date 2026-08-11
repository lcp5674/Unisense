# ADR-005: 治理采用 RBAC 六角色 PDP 纯函数决策（默认拒绝）+ 域授权 + PII 门禁

## 状态：Accepted
## 日期：2026-08-08
## 背景
需统一权限判定与合规门禁（TD §12.5，FR-11）。核心挑战：
1. 权限模型既要表达力足够、又要可穷举测试；
2. 合规强制（PII）必须 fail-closed；
3. 敏感分级需防误放开。

对应模块 `governance`（状态 released，13/13 门禁）。

## 决策
1. **RBAC 六角色 + 域授权**：`platform_admin / domain_admin / metric_owner / reviewer / compliance_officer / viewer`；跨域访问须另有 `grants` 授权（含 `metric_whitelist` / `domain` / `expires_at` / `row_level`）。PDP `decide()` 为**纯函数**，判定顺序：动作合法性 → PII 合规门禁 → platform_admin 直通 → 本域角色 → 跨域授权 → 拒绝，**默认拒绝（fail-closed）**。
2. **PII 合规门禁 COMPL-1**：未通过合规复核的 PII 资产，除合规官 `review` 动作外一律拒绝（`FORBIDDEN_PII`）。
3. **敏感级只升不降**：`infer_sensitivity` 对人工/历史已判定为高敏的级别，规则引擎不得自动降级（避免误放开）；分级重扫规则引擎 `detect_pii_columns` 规则字典（`PII_RULES`）可配置，`RULES_VERSION` 变更可回填，误报由治理人工修正。

## 备选方案
- **ACL 细粒度**：建模复杂、运维成本高、角色爆炸 → 否决。
- **默认放行**：合规风险，违反最小权限 → 否决。
- **敏感级可被规则引擎自动降级**：误放开风险 → 否决。

## 后果
- **正面**：默认拒绝收紧攻击面；PII 强门禁满足合规；规则引擎可演进且不破坏既有高敏判定；PDP 纯函数可穷举边界单测。
- **负面**：纯函数 PDP 一期不含行级强制过滤（仅标注 `restricted`，TD §12.5）；角色/规则字典变更需治理评审与 ADR 跟踪；`analyst` 兼容角色为历史迁移遗留（只读）。
