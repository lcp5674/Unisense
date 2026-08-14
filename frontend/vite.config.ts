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
    // macOS FSEvents 在本仓库反复静默失效（监听不到文件变更 → 模块转换缓存不失效，
    // 出现「改了代码页面无变化」，只能重启）。改用 chokidar 轮询监听：不依赖系统事件，
    // 代价是少量 CPU，换取稳定可靠的热更新。
    watch: {
      usePolling: true,
      interval: 200,
      // 显式忽略大目录/测试产物，降低轮询开销并避免并行测试写文件触发无谓刷新
      ignored: [
        "**/.git/**",
        "**/node_modules/**",
        "**/.eslintcache",
        "**/dist/**",
        "**/test-results*/**",
        "**/.playwright-cli/**",
      ],
    },
    proxy: {
      "/api": {
        target: "http://localhost:8100",
        changeOrigin: true,
      },
    },
  },
});
