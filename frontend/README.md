# Unisense Web（指标语义中台前端）

基于 **Vite + React 18 + TypeScript** 的指标语义定义管理界面，对接后端
`/api/v1/metric-definitions` 接口（详见 `backend/app/api/metrics.py`）。

## 功能（MVP 7 页）

- **指标目录**：搜索（编码/名称）、按业务域/状态/层级过滤、分页
- **注册指标**：新建草稿（含口径定义 JSON 校验）
- **指标详情**：业务/技术定义、口径 JSON、版本历史、发布 / PII 复核 / 废弃
- **审核工作台**：冲突列表 + 仲裁 / 升级（走统一响应信封，权限由后端 RBAC 强制）
- **待办中心**：未决冲突 + 草稿指标聚合视图
- **血缘视图**：按节点查下游影响 / 上游来源 / 双向（impact + edges 组合）
- **我的收藏**：收藏 / 取消收藏指标

> 说明：前端**仅对接真实后端**，已移除离线 Demo 模式。鉴权用 `Bearer` token（登录返回），
> 所有请求附带 `X-Api-Key`（Semantic API 网关，见 `.env.example`）。

## 开发

```bash
npm install
cp .env.example .env      # 可选：配置后端地址 / 开启 Demo
npm run dev               # http://localhost:5173
```

开发态默认通过 Vite 代理把 `/api` 转发到 `http://localhost:8000`，
需先启动后端：`cd ../backend && poetry run uvicorn app.main:app --reload --port 8000`。

## 对接真实后端

1. 启动并完成数据库迁移与种子账号（见后端 `scripts/seed_admin.py`）：
   ```bash
   cd ../backend
   export UNISENSE_DB_URL="mysql+aiomysql://unisense:unisense@localhost:3306/unisense?charset=utf8mb4"
   export UNISENSE_JWT_SECRET="<your-secret>"
   poetry run alembic upgrade head
   UNISENSE_SEED_ADMIN_PASSWORD=changeme123 python scripts/seed_admin.py
   poetry run uvicorn app.main:app --reload --port 8000
   ```
   默认管理员：`admin` / `changeme123`。
2. 前端界面右上角用「用户名 / 密码」登录（调用 `POST /api/v1/auth/login`），
   成功后自动拉取 `GET /api/v1/auth/me` 并缓存 JWT；「退出」清除本地令牌。
3. 或将 `.env` 中 `VITE_API_BASE_URL` 指向 `http://<host>:8000/api/v1`，
   并保持 `VITE_DEMO=false`。开发态默认经 Vite 代理 `/api` → `localhost:8000`。

## 离线 Demo

已移除（MVP 仅对接真实后端）。

## 端到端冒烟

栈起后运行 `Node ≥18` 冒烟脚本，覆盖 MVP 验收链路（登录→列指标→注册→发布→详情→收藏→血缘→冲突）：

```bash
# 先按上文启动后端（确保 :8000 可访问）
node e2e/smoke.mjs
# 可选环境变量：API_BASE / SEMANTIC_API_KEY / USERNAME / PASSWORD
```

## 构建

```bash
npm run build             # tsc --noEmit + vite build → dist/
npm run typecheck         # 仅类型检查
```
