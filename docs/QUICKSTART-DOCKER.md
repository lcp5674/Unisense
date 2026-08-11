# 启动开发环境（依赖 + 后端 + 前端）
docker compose up -d

# 查看服务状态
docker compose ps

# 查看后端日志
docker compose logs -f backend

# 查看前端日志
docker compose logs -f frontend

# 仅重建后端镜像（代码变更后）
docker compose up -d --build backend

# 停止所有服务
docker compose down

# 停止并清除数据卷（⚠️ 会丢失 MySQL/Redis 等数据）
docker compose down -v

# 访问地址
# 前端：      http://localhost:8080
# 后端 API：  http://localhost:8000/api/v1
# API 文档：  http://localhost:8000/docs
# 健康检查：  http://localhost:8000/health
# 指标：      http://localhost:8000/metrics

# 数据库连接（宿主机）
# MySQL:    localhost:3307  (user: unisense, pass: test)
# Redis:    localhost:16379
# Neo4j:    http://localhost:7474 (neo4j/test1234)
# ES:       http://localhost:19200

# 配置 LLM（在 .env 中设置，或启动时传入）
UNISENSE_LLM_BASE_URL=https://api.kilo.ai/api/gateway \
UNISENSE_LLM_API_KEY=eyJhbGci... \
UNISENSE_LLM_DEFAULT_MODEL=poolside/laguna-m.1:free \
docker compose up -d backend
