# Unisense 前端 Dockerfile
# 多阶段构建：node 构建 → nginx 运行
FROM node:20-alpine AS builder

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

    # SPA：所有路由都回退到 index.html
    location / {
        root /usr/share/nginx/html;
        try_files $uri $uri/ /index.html;
    }

    # API 反代到后端服务
    location /api/ {
        proxy_pass http://backend:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
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
    CMD wget -q -O /dev/null http://localhost:80/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
