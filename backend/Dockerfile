# Unisense 后端 Dockerfile
# 多阶段构建：第一阶段安装依赖，第二阶段精简运行时镜像
FROM python:3.11-slim AS builder

# 安装构建依赖（cryptography 等需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libffi-dev libssl-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /build

# 复制依赖清单（requirements.txt 由 poetry export 生成；过滤可编辑安装项）
COPY backend/requirements.txt ./requirements.txt
# 过滤 -e git+ssh://... 可编辑安装（容器内无法 SSH 克隆）
RUN grep -v "^-e git+" requirements.txt > requirements.filtered.txt && \
    pip install --no-cache-dir --user -r requirements.filtered.txt

# ---- 运行时镜像 ----
FROM python:3.11-slim

# 安装运行时依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl && \
    rm -rf /var/lib/apt/lists/*

# 从 builder 复制已安装的 Python 包
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH

WORKDIR /app

# 复制应用代码
COPY backend/ /app/

# 复制 alembic 迁移配置
ENV PYTHONPATH=/app

# 暴露端口
EXPOSE 8000

# 健康检查（/health 端点）
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=15s \
    CMD curl -f http://localhost:8000/health || exit 1

# 启动脚本：先迁移再启动
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
