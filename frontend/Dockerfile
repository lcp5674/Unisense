# Unisense 前端 Dockerfile
# 多阶段构建：node 构建 → nginx 运行
# S-2（第九轮）：基础镜像 tag 浮动，发布前请用 scripts/pin_base_images.sh 固定 digest。
FROM node:22-alpine AS builder

WORKDIR /build

# 先复制 package 文件利用缓存
COPY frontend/package.json frontend/package-lock.json* ./

RUN npm ci || npm install

# 复制源码并构建
COPY frontend/ ./

# 构建参数：API 基础地址（运行时通过 nginx 反代，构建时设为空走相对路径）
ARG VITE_API_BASE_URL=""
ARG VITE_SEMANTIC_API_KEY="dev-semantic-key"
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
ENV VITE_SEMANTIC_API_KEY=$VITE_SEMANTIC_API_KEY

RUN npm run build

# ---- nginx 运行时 ----
FROM nginx:alpine

# 复制构建产物
COPY --from=builder /build/dist /usr/share/nginx/html

# pid 置 /dev/null 仅为兜底（防 pid 残留）；真正根治是 compose 层 `master_process off`
# 单进程模式（nginx 不再创建 pid 文件）——见 docker-compose.yml frontend 服务注释。
# 背景（铁证）：生产机 CentOS 7 内核 3.10 + Docker 26.1.4（官方不支持组合）上，nginx 写
# pid 文件（/tmp、/var/run、/dev/null 全部）均 EPERM 崩溃重启，而同容器内 shell
# `echo > /dev/null` 成功——nginx 写 pid 用 pwrite 系统调用（echo 用 write），拦截发生在
# syscall 层（seccomp/安全 Agent/内核兼容），与写到哪里无关，镜像层无法修复。
RUN chown -R nginx:nginx /var/cache/nginx /var/run && \
    sed -i 's|^pid .*|pid /dev/null;|' /etc/nginx/nginx.conf

# nginx 配置：SPA 路由 + API 反代到后端
RUN cat > /etc/nginx/conf.d/default.conf <<'EOF'
server {
    listen 8080;
    server_name _;

    # P2-2（第八轮）：静态产物安全头（对齐后端 SecurityHeadersMiddleware——
    # 后端 6 项安全头此前仅覆盖 /api 反代响应，静态页面/资源缺失）。
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "DENY" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
    add_header X-XSS-Protection "1; mode=block" always;
    # HSTS 仅 HTTPS 部署生效；内网 HTTP 访问时浏览器忽略，无副作用。
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    # CSP：宽松策略适配 Vite/antd 构建（style 需 unsafe-inline 支持动态样式注入），
    # frame-ancestors 'none' 防点击劫持；不引入 nonce 以免破坏第三方脚本。
    add_header Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data: https://fonts.gstatic.com; connect-src 'self'; frame-ancestors 'none'" always;

    # SPA：所有路由都回退到 index.html
    # index.html 必须 no-cache（否则浏览器缓存后用户看不到新功能）；
    # 静态资源（带 hash）继续长缓存。
    location = /index.html {
        root /usr/share/nginx/html;
        add_header Cache-Control "no-cache, no-store, must-revalidate" always;
        add_header Pragma "no-cache" always;
        add_header Expires "0" always;
    }
    location / {
        root /usr/share/nginx/html;
        try_files $uri $uri/ /index.html;
    }

    # API 反代到后端服务（动态 DNS 解析：backend 重建换 IP 后无需重启 frontend）
    resolver 127.0.0.11 valid=10s ipv6=off;
    location /api/ {
        set $backend_upstream http://backend:8000;
        proxy_pass $backend_upstream;
        # $http_host 保留端口（$host 会丢弃 :8180）——否则后端 307 重定向会回到 :80 导致 CORS 失败
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        # 批量 LLM 推断（逐字段并发调 LLM）可能长达几分钟——放宽反代读写超时，避免 504
        proxy_connect_timeout 30s;
        proxy_send_timeout 300s;
        proxy_read_timeout 300s;
    }

    # API 文档（Swagger UI / ReDoc / OpenAPI 规范）：反代到后端
    # Swagger/ReDoc 静态资源已本地化（后端 /static/，离线可用），后端对 /docs /redoc
    # 返回本地化精确 CSP（script-src 'self'）。此处隐藏后端 CSP 头并显式设置相同
    # 本地化策略——避免 nginx 继承 server 级宽松 CSP 形成「双 CSP 头交集」混乱。
    location ~ ^/(docs|redoc|openapi\.json)$ {
        set $backend_upstream http://backend:8000;
        proxy_pass $backend_upstream;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_hide_header Content-Security-Policy;
        # frame-ancestors 'self'：允许同源前端（/api-docs iframe）内嵌 Swagger/ReDoc，
        # 其余站点仍被阻止（点击劫持防护不削弱）；X-Frame-Options 由后端 SAMEORIGIN 透传。
        add_header Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-ancestors 'self'" always;
        proxy_connect_timeout 30s;
        proxy_read_timeout 60s;
        proxy_send_timeout 60s;
    }

    # 后端自托管文档静态资源（swagger-ui / redoc）反代——不走 SPA try_files，
    # 否则 /static/* 会被回退成 index.html 导致文档资源 404。
    # 使用 ^~ 前缀（优先于下方正则静态资源 location），确保 .js/.css 等命中此处。
    location ^~ /static/ {
        set $backend_upstream http://backend:8000;
        proxy_pass $backend_upstream;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        expires 1d;
        add_header Cache-Control "public, immutable" always;
    }

    # 静态资源缓存
    location ~* \.(js|css|png|jpg|jpeg|gif|ico|svg|woff|woff2)$ {
        root /usr/share/nginx/html;
        expires 30d;
        add_header Cache-Control "public, immutable";
    }
}
EOF

EXPOSE 8080

# S-2：nginx 官方形态 = master 以 root 运行（读配置/绑 80→8080），worker 由
# nginx.conf 的 `user nginx;` 自动降权处理请求。曾用 `USER nginx` 把 master 也降权
# （过度加固）导致 CentOS 7 老内核(3.10) 上写 pid EPERM 崩溃；经多轮验证（pid 改
# /tmp、/var/run、/dev/null 全 EPERM）确认根因在 syscall 层非目录/权限，最终由 compose
# `master_process off` 单进程模式根治（不再写 pid）。移除 USER nginx 恢复官方形态；
# worker 降权不变（安全面与官方镜像一致）。
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:8080/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
