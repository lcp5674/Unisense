# Unisense — OpenCode 约定

本仓库的**完整开发规范与交付追踪要求**集中在 `docs/DEV_GUIDE.md`，请先读取并严格遵守，包括但不限于：

- 最小改动、先读后写、真实验证、独立复核（§1）
- 12 道质量门禁与状态晋升规则（§2/§3）
- 文档同步规则与提交信息格式 `[服务] 动作：简述 (TD§x.y, FR-xx)`（§4/§5）
- 状态追踪文件 `docs/module-status.yaml` + `docs/CHANGELOG_MODULES.md`

关键事实源：
- PRD：`docs/proposal.md`
- TD：`docs/technical-design.md`（§19 开发规范 / §20 协作追踪）
- 门禁 CI：`.github/workflows/gateways.yml`
- 本地门禁：`.pre-commit-config.yaml`（`pre-commit install` 启用）

> 本项目也兼容 CodeBuddy（`AGENTS.md`）与 Claude Code（`CLAUDE.md`），三份文件均指向 `docs/DEV_GUIDE.md`。
