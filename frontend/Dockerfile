# Unisense 前端 Dockerfile
# 多阶段构建：node 构建 → nginx 运行
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

# nginx 配置：SPA 路由 + API 反代到后端
RUN cat > /etc/nginx/conf.d/default.conf <<'EOF'
server {
    listen 80;
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

    # 静态资源缓存
    location ~* \.(js|css|png|jpg|jpeg|gif|ico|svg|woff|woff2)$ {
        root /usr/share/nginx/html;
        expires 30d;
        add_header Cache-Control "public, immutable";
    }
}
EOF

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:80/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
