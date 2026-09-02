# Unisense 后端 Dockerfile
# 多阶段构建：第一阶段安装依赖，第二阶段精简运行时镜像
# 说明：cryptography 在 py3.11 有 manylinux 预编译 wheel，pip 直接装即可，
#       无需 gcc/libffi-dev/libssl-dev（避免依赖 Debian 源稳定性，加快构建）
# S-2（第九轮）：基础镜像 tag 为浮动（3.11-slim），发布前请在可联网机器
# 执行 scripts/pin_base_images.sh 固定 digest（生产供应链加固）。
FROM python:3.11-slim AS builder

WORKDIR /build

# 复制依赖清单（requirements.txt 由 poetry export 生成；过滤可编辑安装项）
COPY backend/requirements.txt ./requirements.txt
# Python 包镜像源：默认清华源（国内构建快、避免官方 PyPI 网络超时）。
# 需要切换时用 --build-arg PIP_INDEX_URL=<其他源> 覆盖（如公司内网源）。
ARG PIP_INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple"
ENV PIP_INDEX_URL=$PIP_INDEX_URL
# 过滤 -e git+ssh://... 可编辑安装（容器内无法 SSH 克隆）
RUN grep -v "^-e git+" requirements.txt > requirements.filtered.txt && \
    pip install --no-cache-dir --user -r requirements.filtered.txt

# ---- 运行时镜像 ----
FROM python:3.11-slim

# S-2：非 root 运行（容器内无 root shell 面；pip --user 装到 /root/.local，
# 以 appuser 身份复制到其 home 并设为 PATH）
RUN useradd --create-home --uid 10001 appuser
COPY --from=builder --chown=appuser:appuser /root/.local /home/appuser/.local
ENV PATH=/home/appuser/.local/bin:$PATH

WORKDIR /app

# 复制应用代码
COPY --chown=appuser:appuser backend/ /app/

# 复制 alembic 迁移配置
ENV PYTHONPATH=/app

# 暴露端口
EXPOSE 8000

# S-2：切到非 root 用户（服务仅监听 8000、写 stdout，无需 root）
USER appuser

# 健康检查（/health 端点，用 python 而非 curl，避免额外 apt 依赖）
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=15s \
    CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health',timeout=3).status==200 else 1)"

# 启动链（任一环失败即退出，由 compose restart: unless-stopped 自动重试）：
#   1) alembic upgrade head —— 表结构升到最新（迁移内含字典/词根/敏感规则等种子）
#   2) python -m scripts.bootstrap —— 部署自举，消除所有人工初始化动作：
#        阻塞：默认组织 + admin 账号、标准主题域 + 10 类业务字典
#        尽力：ES 索引创建/同步、Neo4j 资产与血缘首日同步
#      阻塞步骤失败 → 退出码 1 → 容器重启重试；尽力步骤失败仅告警不阻断
#   3) exec uvicorn —— exec 让 uvicorn 成为 PID 1，确保优雅接收 SIGTERM
# 说明：worker 服务用显式 command 覆盖 CMD（docker-compose.yml），不会重复自举。
CMD ["sh", "-c", "alembic upgrade head && python -m scripts.bootstrap && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
