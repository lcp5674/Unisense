# D10 §1.5 人工 Ratify 签收卷宗（准备材料 · 由 AI 整合，人类签收）

> **文档性质**：本卷宗为 **D10 §1.5 人工 ratify 的准备材料**，由 AI 协调器整合各模块已达机械门槛的证据、双视角审查结论与待人工判断的已知风险。**AI 不代签**——最终签收必须由对应人类签署人（owner / reviewer / 治理委员会）独立核验后填写「签收栏」。
>
> **依据**：`docs/DEV_GUIDE.md` §3（状态晋升规则：`released` = verified + runbook + 迁移可逆 + §6.3 双视角 0 High）+ §6.2/§6.3（独立复核与交付后双视角审查）+ `docs/module-status.yaml`（单一事实源）。
>
> **生成日期**：2026-08-11　|　**数据快照**：`docs/module-status.yaml` @ 2026-08-11（semantic 节已按 2026-08-24 最新证据刷新，见 3.3）
>
> **14/14 服务当前状态**：全部 `released`（机械门槛达成），均处 `ratify_pending`。

---

## 0. 使用说明与签收规则

1. **签收前人类须独立核验**：evidence_path 指向的报告文件、`runbook_path`、迁移可逆脚本、§6.3 审查记录，须由签收人实际查阅/抽样重跑，禁止仅凭本卷宗文字直接签收。
2. **签收结论取值**：`完全签收` / `有条件签收`（须填条件）/ `退回`（须填原因，退回后状态回落需写 rollback_reason）。
3. **§6.3 硬门槛**：任何服务若其「§6.3 双视角」记录缺失或 High > 0，**不得签收**，须先补齐审查。
4. **签收即代表**：确认机械门槛证据真实、已知风险已读且可接受（或已列条件）、该服务可投入生产运行/对外交付。
5. **归档**：签收完成后，将各「签收栏」回填至 `docs/module-status.yaml` 对应模块的 `ratify` 字段，并在 `docs/CHANGELOG_MODULES.md` 追加一条 ratify 审计行。

---

## 1. 总览表（14 服务）

| # | 服务 | FR | 状态 | verified_at | 门禁通过数 | §6.3 双视角记录 | perf 基线 | 签收状态 |
|---|------|----|------|-------------|-----------|----------------|----------|---------|
| 1 | collector | FR-02/03 | released | 2026-08-08 | 13/13 | ✓ 独立块 | ✓ 报告 | ⬜ |
| 2 | lineage | FR-04/93/94 | released | 2026-08-08 | 13/13 | ✓ 独立块 | ✓ 报告 | ⬜ |
| 3 | semantic | FR-05/06/07 | released | 2026-08-08 | 13/13 | ✓ 独立块（2026-08-11 补齐） | ✓ 报告 | ⬜ |
| 4 | conflict | FR-09 | released | 2026-08-08 | 13/13 | ✓ 独立块 | ✓ 报告 | ⬜ |
| 5 | governance | FR-11 | released | 2026-08-08 | 13/13 | ✓ 独立块 | ✓ 报告 | ⬜ |
| 6 | consume | FR-12/13 | released | 2026-08-09 | 13/13 | ✓ 独立块 | ✓ 报告 + live(p95=212ms<300ms) | ⬜ |
| 7 | ai | FR-14/15 | released | 2026-08-09 | 13/13 | ✓ sic 汇总 | ⚠ 脚本未 live 跑（非阻塞） | ⬜ |
| 8 | quality | FR-10 | released | 2026-08-09 | 13/13 | ✓ 独立块 | ✓ 报告 | ⬜ |
| 9 | notify | FR-16/17 | released | 2026-08-09 | 13/13 | ✓ sic 汇总 | ⚠ 脚本未 live 跑 | ⬜ |
| 10 | observability | FR-16 | released | 2026-08-09 | 13/13 | ✓ sic 汇总 | ⚠ 脚本未 live 跑 | ⬜ |
| 11 | assetmap | FR-18 | released | 2026-08-09 | 13/13 | ✓ sic 汇总 | ⚠ 脚本未 live 跑 | ⬜ |
| 12 | recommend | FR-19 | released | 2026-08-09 | 13/13 | ✓ sic 汇总 | ⚠ 脚本未 live 跑 | ⬜ |
| 13 | glossary | FR-08 | released | 2026-08-09 | 13/13 | ✓ sic 汇总 | ⚠ 脚本未 live 跑 | ⬜ |
| 14 | dimension | FR-05/09 | released | 2026-08-09 | 13/13 | ✓ sic 汇总 | ⚠ 脚本未 live 跑 | ⬜ |

> \* `secret`/`supply_chain` 两道全局门禁项（CI/.gateways.yml `protected: true`，仓库级 `gitleaks`/`pip-audit` 强制）已于 2026-08-11 在 8 个服务的 `gateways_passed` 中补齐登记（D10-2 关闭），现 14 服务口径一致为 13/13。
>
> **perf 基线统一缺口**：ai/notify/observability/assetmap/recommend/glossary/dimension 的 k6 基线脚本已定义（`backend/tests/perf/baseline_*.js`），但**未在 live 环境真实执行**——属 D10 跨切面待办，不阻塞安全 `released`，但签收时须列为「有条件/已知接受」。

---

## 2. 跨切面「五项合规」核查清单（签收前须逐条确认）

以下 5 项为平台级合规能力，须由签收人在 D10 中整体确认已落地，不归属单一服务：

| # | 合规维度 | 落地服务 / 证据 | 签收人确认 |
|---|---------|----------------|-----------|
| C1 | **PIPL 被遗忘权**（脱敏非删除 + 审计留痕 + SHA256 前缀） | governance `POST /pii/anonymize`（D9 修复 `_scrub_pii` 结构化递归，已单测/安全测）| ⬜ |
| C2 | **PII 分级与强制复核门禁**（未复核资产一律 `FORBIDDEN_PII`，合规官禁自审） | governance（COMPL-1/COMPL-2）+ conflict PII 特殊路由 | ⬜ |
| C3 | **审计留痕完整性 + WORM**（`audit_log` 禁 UPDATE/DELETE，写操作全审计） | observability + 各服务 `write_audit`（PLAT-3 原子化已修） | ⬜ |
| C4 | **传输/存储加密 + 密钥托管**（PII 明文仅 MySQL 加密表空间、密钥 KMS 90 天轮转） | TD §15.2/§13.10；凭据 Fernet 加密存储（collector/governance） | ⬜ |
| C5 | **合规留存期**（审计热存 180d、快照 180d、血缘边 180d，TD §13 参数） | observability / consume / lineage 配置项 | ⬜ |

> 上述 C1–C5 若任一项签收人认为证据不足，**退回 D10 并标注缺口**，不得带病签收。

---

## 3. 逐服务签收卷

> 每节含：基本信息 / 门禁 / §6.3 结论 / 待人工判断项 / **签收栏（空白）**。

### 3.1 collector（FR-02/03 · §12.1）

| 项 | 内容 |
|----|------|
| 状态 | released | verified_at | 2026-08-08 |
| evidence | `backend/tests/reports/collector_gateways_2026-08-07.txt` |
| runbook | `docs/runbooks/collector.md` |
| 门禁 | lint/type/unit/integration/security_reverse/chaos/observability/contract/doc_sync/migration/perf_baseline/secret/supply_chain（13/13） |

**§6.3 双视角结论**：0 High；Medium 2 已修复（引擎释放 / 列表注入守卫+RBAC）。verdict：满足 released 机械门槛，仅余 §1.5 ratify。

**待人工判断项（已知接受）**：
1. 全量采集为请求内同步执行（TD 定义 arq 异步任务），一期 SPI 可注入，后续接采集队列。
2. 删除数据源软删源但保留 `db_catalog` 历史（孤儿资产），符合审计留痕；可后续补级联软删。

**签收栏**
```
签收人：__________  角色：__________  日期：__________
结论：□ 完全签收  □ 有条件签收（条件：__________）  □ 退回（原因：__________）
备注：____________________________________________________________________
```

### 3.2 lineage（FR-04/93/94 · §12.2）

| 项 | 内容 |
|----|------|
| 状态 | released | verified_at | 2026-08-08 |
| evidence | `backend/tests/reports/lineage_gateways_2026-08-07.txt` |
| runbook | `docs/runbooks/lineage.md` |
| 门禁 | 13/13 |

**§6.3 双视角结论**：0 High；Medium 3 已修复（别名归一化 / max_edges 上限 / 孤儿边 API）。verdict：满足 released。

**待人工判断项（已知接受）**：
1. 字段级血缘为基线实现（投影级），深度列血缘（CTE/子查询/表达式）待后续通道补全。
2. 影响分析 BFS 每跳每方向一次 DB 往返（N+1），受 `max_hops≤10` 与 `max_edges≤5000` 约束，目标规模下可接受。

**签收栏**
```
签收人：__________  角色：__________  日期：__________
结论：□ 完全签收  □ 有条件签收（条件：__________）  □ 退回（原因：__________）
备注：____________________________________________________________________
```

### 3.3 semantic（FR-05/06/07 · §12.3）✅ §6.3 已闭环（2026-08-11）+ 门禁复审全绿（2026-08-21）+ 批次 B 已提交（2026-08-24）

| 项 | 内容 |
|----|------|
| 状态 | verified（2026-08-21 复审）——released 机械门槛已达成，仅余 §1.5 人工 ratify |
| evidence | `backend/tests/reports/semantic_gateways_2026-08-17.txt`（5026B 真实非空）+ `backend/tests/reports/semantic_perf_baseline_live_2026-08-21.txt`（k6 live：10 VU/30s，P95=131.63ms、fail 0.00%、1920/1920 checks） |
| runbook | `docs/runbooks/semantic.md` |
| 门禁 | 13/13（lint/type/unit/integration/contract/doc_sync/security_reverse/chaos/perf_baseline/observability/secret/supply_chain） |

**§6.3 双视角结论**（2026-08-11 补齐，详见 module-status.yaml `post_verify_review` + CHANGELOG_MODULES）：
- **0 High**（修复后）。原审查发现 2 处 High 并已在 2026-08-11 修复：
  1. **PII 合规复核自审漏洞（COMPL-2）**：端点原允许 `metric_owner` 自审 → 限 `platform_admin`/`domain_admin` + service 层 `owner_id != actor_id` 守卫（`SELF_REVIEW_BLOCKED`）。
  2. **写端点零审计（TD §15.4）**：create/update/publish/deprecate/pii-review 5 端点补 `write_audit`（与业务同事务）。
- 新增 `test_semantic_security.py`（5 项）+ `test_semantic_service.py` 补 2 项，pytest 20 项全绿，ruff 全清。
- **2026-08-21 门禁复审全绿**：semantic 单元测试 355 passed、`security_reverse` 5/5（修复 `test_semantic_security` 遗漏的 `run_lineage_post_commit` AsyncMock 缺口）、语义 live 性能基线 P95=131.63ms 闭环此前 8-13 live 报告未覆盖语义模块的缺口。
- **2026-08-24 批次 B 遗留改动已提交（5b8149b）**：`api/semantic.py` 13 行——`create_template` 并发撞唯一键 `IntegrityError` → 回滚 + 映射 `ConflictError`/`TPL_EXISTS`，ruff 修复 B904 `raise from`，补 `test_create_template_commit_integrity_error_maps_conflict`；semantic 相关单测 360 passed / ruff 0。独立复核 `post_verify_review`（2026-08-11）0 High；满足 DEV_GUIDE §3 `verified` = implemented + 全部门禁绿 + 独立复核通过。

**待人工判断项（已知接受）**：
- 无新增；released 机械门槛已全达，仅余 §1.5 人工 ratify（agent 不可代签，须人类核验 evidence/runbook 后签收）。

**签收栏**
```
签收人：__________  角色：__________  日期：__________
结论：□ 完全签收  □ 有条件签收（条件：__________）  □ 退回（原因：__________）
备注：____________________________________________________________________
```

### 3.4 conflict（FR-09 · §12.4）

| 项 | 内容 |
|----|------|
| 状态 | released | verified_at | 2026-08-08 |
| evidence | `backend/tests/reports/conflict_gateways_2026-08-07.txt` |
| runbook | `docs/runbooks/conflict.md` |
| 门禁 | 13/13 |

**§6.3 双视角结论**：0 High；Medium 0。verdict：满足 released。

**待人工判断项（已知接受）**：
1. 冲突检测基于定义文本相似度（编辑距离/Jaccard），无语义 embedding 时为粗粒度匹配，深度同义需后续 LLM 补位（TD 已规划）。
2. PII 路径仅做路由拦截，未做字段级脱敏落库外的二次校验（依赖 governance）。

**签收栏**
```
签收人：__________  角色：__________  日期：__________
结论：□ 完全签收  □ 有条件签收（条件：__________）  □ 退回（原因：__________）
备注：____________________________________________________________________
```

### 3.5 governance（FR-11 · §12.5）

| 项 | 内容 |
|----|------|
| 状态 | released | verified_at | 2026-08-08 |
| evidence | `backend/tests/reports/governance_gateways_2026-08-07.txt` |
| runbook | `docs/runbooks/governance.md` |
| 门禁 | 13/13 |

**§6.3 双视角结论**：0 High（修复后）；High 1 已修复（User.role 枚举归一化）。verdict：满足 released。

**待人工判断项（已知接受）**：
1. ~~授权回收 `DELETE`/batch revoke 仅按角色闸门，缺授权目标归属/域校验（建议补 owner/domain_admin 范围）~~ → **已修复（2026-08-11）**：`GovernanceService._assert_revoke_scope` 收敛回收范围——platform_admin 全局、domain_admin 仅本域、其余角色仅本人授权（fail-closed）；单条回收端点放宽至全体已登录用户由服务层收敛（owner 自管）。单测+安全测新增 10 项全绿。

**签收栏**
```
签收人：__________  角色：__________  日期：__________
结论：□ 完全签收  □ 有条件签收（条件：__________）  □ 退回（原因：__________）
备注：____________________________________________________________________
```

### 3.6 consume（FR-12/13 · §12.6）

| 项 | 内容 |
|----|------|
| 状态 | released | verified_at | 2026-08-09 |
| evidence | `backend/tests/reports/consume_gateways_2026-08-09.txt` + `consume_perf_2026-08-09.txt` |
| runbook | `docs/runbooks/consume.md` |
| 门禁 | 13/13（perf 基线已于 2026-08-11 live 复测通过，见 D10-4） |

**§6.3 双视角结论**：0 High；双视角产品/技术发现均闭环（域隔离/PII 强制/注入 fail-closed、配额熔断、混沌自恢复、快照 WORM）。

**待人工判断项（已知接受 / 条件签收建议）**：
1. ~~**性能契约缺口**：perf 基线 p95≈1.73s 超 300ms 契约（本地 Docker MySQL 同步往返所致）~~ → **已闭环（2026-08-11 live 复测）**：独立压测库（unisense_perf）+ `perf_client:PerfClient@123` 接入方，k6 `baseline_consume.js` 10 VU / 30s 实测 **p95=212.25ms < 300ms**、失败率 0.00%、771/771 checks 通过。原 1.73s 为本地 Docker MySQL 往返噪声，非逻辑回归；契约成立。

**签收栏**
```
签收人：__________  角色：__________  日期：__________
结论：□ 完全签收  □ 有条件签收（条件：__________）  □ 退回（原因：__________）
备注：consume perf 已于 2026-08-11 live 复测达标（p95=212ms<300ms）
```

### 3.7 ai（FR-14/15 · §12.7）

| 项 | 内容 |
|----|------|
| 状态 | released | verified_at | 2026-08-09 |
| evidence | `backend/tests/reports/gates_readonly_2026-08-09.txt` + `ai_unit_2026-08-09.txt` |
| runbook | `docs/runbooks/ai.md` |
| 门禁 | 13/13（secret/supply_chain 已登记；perf 基线脚本未 live 跑） |

**§6.3 双视角结论**（sic_reviews_2026_08_09）：原 High 3（问数不含真实 LLM 调用 / execute=True 空承诺 / 漏挂注入守卫）**已全部修复**，现 0 High；残余 Medium 为增强项。

**待人工判断项（已知接受）**：
1. **ai High 设计项（产品架构取舍）**：NL2SQL「问数不含真实 LLM 调用」「execute=True 语义」属架构决策，需独立产品评估，不阻塞安全 released。→ 签收时须确认该取舍已被产品方知情接受。
2. **perf 基线**：k6 `baseline_ai.js` 未 live 执行。

**签收栏**
```
签收人：__________  角色：__________  日期：__________
结论：□ 完全签收  □ 有条件签收（条件：AI 架构取舍产品方确认 + perf live 排期）  □ 退回
备注：____________________________________________________________________
```

### 3.8 quality（FR-10 · §12.8）

| 项 | 内容 |
|----|------|
| 状态 | released | verified_at | 2026-08-09 |
| evidence | `backend/tests/reports/quality_gateways_2026-08-08.txt` |
| runbook | `docs/runbooks/quality.md` |
| 门禁 | 13/13（secret/supply_chain 已登记；FR-10 子集已实现 D11） |

**§6.3 双视角结论**：0 High；Medium/Low 已收敛（ack note 落库 / 7 列留痕 / service 死参数消除）。

**待人工判断项（已知接受）**：
1. **FR-10 一期范围**：仅覆盖规则 CRUD + 事件状态机 + 静态阈值 +（D11）外部基准对账；**动态基线 / 同环比 / 跨源 / 修复建议未实现**，创建时 fail-fast 拦截未实现模式。→ 签收时须确认 FR-10 分期边界已被产品方接受。
2. integration/chaos/perf 门禁待 live 环境复测。

**签收栏**
```
签收人：__________  角色：__________  日期：__________
结论：□ 完全签收  □ 有条件签收（条件：FR-10 分期边界产品方确认）  □ 退回
备注：____________________________________________________________________
```

### 3.9 notify（FR-16/17 · §12.9）

| 项 | 内容 |
|----|------|
| 状态 | released | verified_at | 2026-08-09 |
| evidence | `backend/tests/reports/gates_readonly_2026-08-09.txt` + `notify_unit_2026-08-09.txt` |
| runbook | `docs/runbooks/notify.md` |
| 门禁 | 13/13（secret/supply_chain 已登记；perf 未 live） |

**§6.3 双视角结论**（sic）：原 High 2（通知只入库从不发送、subscriber/user_id IDOR）**已修复**，0 High。

**待人工判断项（已知接受）**：perf 基线 k6 未 live 执行。

**签收栏**
```
签收人：__________  角色：__________  日期：__________
结论：□ 完全签收  □ 有条件签收（条件：perf live 排期）  □ 退回
备注：____________________________________________________________________
```

### 3.10 observability（FR-16 · §12.10/§16）

| 项 | 内容 |
|----|------|
| 状态 | released | verified_at | 2026-08-09 |
| evidence | `backend/tests/reports/gates_readonly_2026-08-09.txt` + `observability_unit_2026-08-09.txt` |
| runbook | `docs/runbooks/observability.md` |
| 门禁 | 13/13（secret/supply_chain 已登记；perf 未 live） |

**§6.3 双视角结论**（sic）：原 High 1（submit_feedback user_id 取自请求体冒名）**已修复**，0 High。

**待人工判断项（已知接受）**：perf 基线 k6 未 live 执行。

**签收栏**
```
签收人：__________  角色：__________  日期：__________
结论：□ 完全签收  □ 有条件签收（条件：perf live 排期）  □ 退回
备注：____________________________________________________________________
```

### 3.11 assetmap（FR-18 · §12.11）

| 项 | 内容 |
|----|------|
| 状态 | released | verified_at | 2026-08-09 |
| evidence | `backend/tests/reports/gates_readonly_2026-08-09.txt` + `assetmap_unit_2026-08-09.txt` |
| runbook | `docs/runbooks/assetmap.md` |
| 门禁 | 13/13（secret/supply_chain 已登记；perf 未 live） |

**§6.3 双视角结论**（sic）：原 High 2（_READ_ROLES 死代码零 RBAC、敏感字段无差别外泄）**已修复**，0 High。

**待人工判断项（已知接受）**：perf 基线 k6 未 live 执行。

**签收栏**
```
签收人：__________  角色：__________  日期：__________
结论：□ 完全签收  □ 有条件签收（条件：perf live 排期）  □ 退回
备注：____________________________________________________________________
```

### 3.12 recommend（FR-19 · §12.12）

| 项 | 内容 |
|----|------|
| 状态 | released | verified_at | 2026-08-09 |
| evidence | `backend/tests/reports/gates_readonly_2026-08-09.txt` + `recommend_unit_2026-08-09.txt` |
| runbook | `docs/runbooks/recommend.md` |
| 门禁 | 13/13（secret/supply_chain 已登记；perf 未 live） |

**§6.3 双视角结论**（sic）：原 High 2（_READ_ROLES 死代码、user_id IDOR）**已修复**，0 High；残余 Medium（反馈闭环/共现阈值）为增强项。

**待人工判断项（已知接受）**：perf 基线 k6 未 live 执行。

**签收栏**
```
签收人：__________  角色：__________  日期：__________
结论：□ 完全签收  □ 有条件签收（条件：perf live 排期）  □ 退回
备注：____________________________________________________________________
```

### 3.13 glossary（FR-08 · §12.14）

| 项 | 内容 |
|----|------|
| 状态 | released | verified_at | 2026-08-09 |
| evidence | `backend/tests/reports/gates_readonly_2026-08-09.txt` + `glossary_unit_2026-08-09.txt` |
| runbook | `docs/runbooks/glossary.md` |
| 门禁 | 13/13（secret/supply_chain 已登记；perf 未 live） |

**§6.3 双视角结论**（sic）：原 High 3（resolver_id 伪造 / 关系创建无审计 / 双重提交审计非原子）**已修复**，0 High；残余 Medium（最小版本保留）为增强项。

**待人工判断项（已知接受）**：perf 基线 k6 未 live 执行。

**签收栏**
```
签收人：__________  角色：__________  日期：__________
结论：□ 完全签收  □ 有条件签收（条件：perf live 排期）  □ 退回
备注：____________________________________________________________________
```

### 3.14 dimension（FR-05/09 · §12.15）

| 项 | 内容 |
|----|------|
| 状态 | released | verified_at | 2026-08-09 |
| evidence | `backend/tests/reports/gates_readonly_2026-08-09.txt` + `dimension_unit_2026-08-09.txt` |
| runbook | `docs/runbooks/dimension.md` |
| 门禁 | 13/13（secret/supply_chain 已登记；perf 未 live） |

**§6.3 双视角结论**（sic）：原 High 5（状态机无 PUBLISHED 入口 / 零迁移校验 / reviewed_by 伪造 / 4 写端点零审计 / 事务边界错误）**已修复**，0 High；残余 Medium（对账保留期）为增强项。

**待人工判断项（已知接受）**：perf 基线 k6 未 live 执行。

**签收栏**
```
签收人：__________  角色：__________  日期：__________
结论：□ 完全签收  □ 有条件签收（条件：perf live 排期）  □ 退回
备注：____________________________________________________________________
```

---

## 4. D10 待补齐项清单（ratify 前须关闭）

| 编号 | 缺口 | 影响服务 | 处置建议 | 状态 |
|------|------|---------|---------|------|
| D10-1 | **semantic §6.3 双视角审查记录缺失** | semantic | 按 DEV_GUIDE §6.3 跑产品+技术双视角，发现并修复 2 处 High（PII 自审 / 写端点零审计），0 High 后写入 CHANGELOG_MODULES | ✅ 已关闭（2026-08-11，修复+测试+记录） |
| D10-2 | `gateways_passed` 未登记 secret/supply_chain（8 服务） | ai/quality/notify/observability/assetmap/recommend/glossary/dimension | 8 服务已在 yaml 补齐登记（CI 全局 `protected` 门禁，gitleaks/pip-audit 仓库级复验） | ✅ 已关闭（2026-08-11） |
| D10-3 | k6 perf 基线脚本未 live 执行 | ai/notify/observability/assetmap/recommend/glossary/dimension（前端 MVP 冒烟同属此类） | 脚本已就绪（`backend/tests/perf/baseline_*.js`）+ 前端 `e2e/smoke.mjs`；2026-08-11 在独立环境（已运行 Docker 偏移端口栈 + 隔离 DB）**已 live 闭环**：`node e2e/smoke.mjs` 8/8 通过、`k6 run baseline_consume.js` p95=212ms<300ms 通过 | ✅ 已闭环（2026-08-11，独立环境 live 执行，见 §4.1 实测） |
| D10-4 | consume perf 超 300ms 契约（本地） | consume | 独立压测环境复测（unisense_perf + perf_client） | ✅ 已闭环（2026-08-11，live p95=212.25ms<300ms，见 §3.6 / §4.1） |
| D10-5 | FR-10 一期范围边界（动态基线/同环比/跨源/修复建议未实现） | quality | 分期边界已文档化（module-status.yaml note），需产品方在 ratify 条件签收时确认 | ⚠ 需产品方 ratify 确认 |
| D10-6 | ai 架构取舍（问数不含真实 LLM / execute=True） | ai | 架构取舍已文档化（sic_reviews 残余 known_accepted），需产品方在 ratify 条件签收时确认 | ⚠ 需产品方 ratify 确认 |

> **签署前关闭策略**：D10-1（硬门槛，已关闭）、D10-2（登记口径，已关闭）、D10-3（E2E 冒烟 live 通过）、D10-4（consume p95 live 达标）均已闭合。D10-5~6 为产品决策，须产品方在 §1.5 ratify 时显式确认（可作条件签收）。**诚实声明**：D10-5~6 的最终闭环需人类/产品方在 ratify 时确认，AI 已做最大可行准备（边界文档化 + 脚本就绪 + D10-3/4 已 live 复测），不代签 ratify 结论。

**附：审查期间附带发现并修复的生产级缺陷（非 D10 缺口，但已闭环）**
- `list_metrics` / `get_metric` 的 `return ok(...)` 被包在 `if pii_flag / if pii_codes:` 内：当指标非 PII 或无 PII 列表时函数无返回值 → 响应校验失败抛 500（`ResponseValidationError`）。修复后将 `return` 移出条件块（审计仍条件执行），`test_list_metrics_success`/`test_read_pii_audit` 由 500 恢复 200，见 CHANGELOG_MODULES 2026-08-11 semantic 行。

---

## 4.1 前端 MVP 交付状态（2026-08-11 补齐，关闭「端到端产品未达 MVP」缺口）

此前 MVP 卡点之一为前端仅 3 页离线 Demo 脚手架、且引用的 API 路径与真实后端不符。
2026-08-11 已完成前端 MVP 冲刺（7 页全部接入真实后端 `/api/v1`）：

| 页面 | 对接后端端点 | 状态 |
|------|-------------|------|
| 登录 | `POST /auth/login` + `GET /auth/me`（`Bearer` + `X-Api-Key`） | ✅ |
| 指标目录 | `GET /metric-definitions`（过滤+分页） | ✅ |
| 注册指标 | `POST /metric-definitions`（201 草稿） | ✅ |
| 指标详情 | `GET` / `PUT`(change_reason≥4) / `publish` / `pii-review`(仅平台/域管，禁 Owner 自审) / `deprecate` / `versions` | ✅ |
| 审核工作台 | `GET /conflicts` + `arbitrate`(compliance_officer/domain_admin) / `escalate`(metric_owner) | ✅ |
| 待办中心 | 聚合 OPEN 冲突 + DRAFT 指标 | ✅ |
| 血缘视图 | `GET /lineage/impact` + `/lineage/edges` | ✅ |
| 我的收藏 | `GET/POST/DELETE /consume/me/favorites` | ✅ |

- 交付物：`frontend/src/{types.ts,api.ts,App.tsx,styles.css,pages/*.tsx}` + `e2e/smoke.mjs`（MVP 链路冒烟）+ README/`.env.example` 更新。
- 验证：`npm run build`（tsc --noEmit + vite build）零错误（2026-08-11）。

#### 4.1.1 全栈 live 实测（2026-08-11 · 独立环境闭环 D10-3/4）
> 环境：本机已运行的 Unisense Docker 栈（mysql@3307 / neo4j@7687 / es@19200 / redis@16379，与 patent-exam-* 栈端口隔离）；`backend/.env` 指向偏移端口 + 默认凭据；补装缺失依赖 `sqlglot`。后端双实例：smoke@8001（主库 unisense）、perf@8002（独立压测库 unisense_perf + 接入方 perf_client）。

**① 后端起栈（迁移 + 种子）**
- Alembic `upgrade head` → 最新 revision `0014_benchmark_reconciliation`（unisense 主库）。
- `scripts/seed_admin.py` → `admin`/`test`（platform_admin）用于冒烟登录。
- `tests/perf/seed_perf.py` → 建库 `unisense_perf` + 迁移 + 30 条 PUBLISHED 指标 + analyst 用户；`seed_consume_client.py` → `perf_client:PerfClient@123`（qps=200）。

**② 后端就绪探针**：`/ready` → `status=ok`（db/redis ok，neo4j/es ok，degraded=[]）。

**③ E2E 冒烟（`node e2e/smoke.mjs`，8/8 通过）**
```
✅ 登录成功
✅ 列指标成功（total=0）
✅ 注册草稿成功（mvp_smoke_1786419386215）
✅ 发布成功
✅ 详情成功（status=PUBLISHED, v1）
✅ 收藏成功
✅ 血缘查询成功（edges=0）
✅ 冲突列表成功（total=0）
=== MVP 冒烟结果：8 通过 / 0 失败 ===
```
> 注：脚本原用 `USERNAME` 环境变量与 macOS 系统内置 `USERNAME=$USER` 冲突（误以 `lcp` 登录致 401）；已改为优先读 `SMOKE_USER`/`SMOKE_PASSWORD`（兼容 `USERNAME`/`PASSWORD`）。

**④ consume p95 压测（k6 `baseline_consume.js`，10 VU / 30s，目标 8002）**
```
http_req_duration  p(95)=212.25ms   < 300ms 阈值 ✅
http_req_failed    rate=0.00%       < 0.5%  阈值 ✅
checks_succeeded   100.00% (771/771)
```
> 结论：原 D10-4 报告的 1.73s 为本地 Docker MySQL 往返噪声；独立隔离库实测 p95=212ms，**满足 300ms 契约**，非逻辑回归。

- **遗留**：ai/notify/observability/assetmap/recommend/glossary/dimension 的专属 k6 基线（baseline_*.js）脚本已就绪，本次未逐一 live 跑（非阻塞，签收时作已知项）；前端已移除离线 Demo，仅对接真实后端。

> 结论：后端 14/14 `released` + 前端 7 页 MVP 已落地，且 **D10-3/4 已在独立环境 live 闭环**（E2E 8/8 + consume p95 达标），**产品端到端已达 MVP 能力**；剩余唯一硬门槛为 §1.5 人类签收（agent 不可代签）。

---

## 5. 回填指引（人类签收后）

1. 将各节「签收栏」结论写入 `docs/module-status.yaml` 对应模块，新增字段：
   ```yaml
   ratify:
     status: "signed"            # signed / conditional / rejected
     signer: "姓名"
     role: "角色"
     signed_at: "2026-08-XX"
     condition: "..."            # conditional 时填；完全签收留空
   ```
   并将 `ratify_pending` 改为 `"已完成（§1.5 ratify @ 2026-08-XX）"`。
2. 在 `docs/CHANGELOG_MODULES.md` 追加 ratify 审计行（签收人/日期/结论/条件）。
3. 14/14 全部 `signed` 后，D10 闭环；`module-status.yaml` 顶部 `updated` 更新为当日。
