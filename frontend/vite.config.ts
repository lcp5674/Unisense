import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 开发时代理 /api → Unisense 后端（默认 localhost:8100，docker-compose 端口避让）。
// 也可通过 .env 的 VITE_API_BASE_URL 直接指向后端（含 /api/v1）。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // 开发环境强制不缓存：避免浏览器沿用旧编译产物导致「改了代码页面无变化」。
    headers: {
      "Cache-Control": "no-store",
    },
    proxy: {
      "/api": {
        target: "http://localhost:8100",
        changeOrigin: true,
      },
    },
  },
});
