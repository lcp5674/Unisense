# ADR-004: 冲突检测采用本地确定性相似度模型 + 仲裁状态机 + PII 路由治理

## 状态：Accepted
## 日期：2026-08-08
## 背景
需检测指标口径冲突并闭环仲裁（TD §12.4，PRD 4.7.2）。核心挑战：
1. 相似度计算成本与可测试性；
2. 冲突需可追踪、可闭环、可审计；
3. PII 冲突须隔离合规处理。

对应模块 `conflict`（状态 released，13/13 门禁）。

## 决策
1. **本地确定性相似度模型**：综合分 = `0.4×name_similarity + 0.4×definition_similarity + 0.2×lineage_overlap`；LLM embedding 不可用时退化为编辑距离（`SequenceMatcher`）+ 分词 Jaccard + 源表集合 Jaccard，**全程本地、无外部依赖**，硬冲突检测不依赖 LLM，可穷举单测。
2. **四类冲突 + PII 路由**：同名异义（硬，阻断发布）/ 同义异名（软，建议合并）/ 粒度单位 / 跨域同义；PII 冲突特殊路由至 `governance.pii_review`，**不进普通冲突表**。
3. **仲裁状态机**：`OPEN → NEGOTIATING → ESCALATED → RULED → CLOSED`；仅 `RULED` 可 `CLOSE`；裁决落 `RulingRecord` 知识库；事件发布（`conflict_open/ruled/escalated/pii_conflict`）为 best-effort 降级，不阻断主流程。

## 备选方案
- **LLM 语义相似度**：成本高、不可控、单测难、依赖外部服务 → 否决。
- **无状态机直接删冲突**：无闭环、无审计、无升级路径 → 否决。
- **PII 与普通冲突混流**：合规风险（未授权 PII 可被普通仲裁放行） → 否决。

## 后果
- **正面**：检测不依赖 LLM、可穷举单测；PII 隔离满足合规；冲突态可追踪闭环、可沉淀裁决知识。
- **负面**：文本深度同义在本地退化模型下为粗粒度匹配，深度同义识别待 LLM 补位（TD 已规划）；同义词/样本库需持续沉淀；事件发布降级意味着通知丢失需依赖重试/日志补偿。
