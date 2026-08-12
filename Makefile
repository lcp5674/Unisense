.PHONY: help install dev lint type test unit integration security chaos perf contract docsync pre-commit setup-services teardown-services migrate-up migrate-down migrate-verify

help: ## 显示所有可用命令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ---- 依赖安装（Poetry，对齐 DEV_GUIDE §18）----
install: ## 安装后端依赖
	poetry install

dev: install ## 安装依赖 + 开发工具 + pre-commit
	poetry install --with dev
	poetry run pre-commit install

# ---- 代码质量 ----
lint: ## Lint + 格式检查
	poetry run ruff check backend && poetry run ruff format --check backend

type: ## 类型检查
	poetry run mypy --strict backend

# ---- 测试 ----
test: unit integration ## 运行所有测试

unit: ## 单元测试 + 覆盖率
	poetry run pytest backend/tests/unit --cov=backend --cov-report=term-missing --junitxml=report-unit.xml

integration: ## 集成测试（需 docker compose up）
	poetry run pytest backend/tests/integration --junitxml=report-integration.xml

security: ## 安全反向测试
	poetry run pytest backend/tests/security --junitxml=report-sec.xml

chaos: ## 混沌/韧性测试
	poetry run pytest backend/tests/chaos --junitxml=report-chaos.xml

perf: ## 性能基线（需 k6）
	poetry run k6 run backend/tests/perf/baseline.js

# ---- 契约校验 ----
contract: ## TD §3/§4 契约一致性
	poetry run python scripts/contract_check.py --mode contract

docsync: ## TD/状态文件文档同步
	poetry run python scripts/contract_check.py --mode doc_sync

gateways-verify: ## 门禁真实性校验
	poetry run python scripts/contract_check.py --mode gateways_verify

# ---- 全量门禁 ----
gateways: lint type unit integration security chaos contract docsync ## 运行全部门禁

# ---- Docker 服务 ----
setup-services: ## 启动本地依赖服务
	docker compose up -d

teardown-services: ## 停止本地依赖服务
	docker compose down

# ---- 数据库迁移 ----
migrate-up: ## 执行数据库迁移
	poetry run alembic upgrade head

migrate-down: ## 回退一步
	poetry run alembic downgrade -1

migrate-verify: ## 验证迁移可逆（up + down + up）
	poetry run alembic upgrade head && poetry run alembic downgrade -1 && poetry run alembic upgrade head
