import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 开发时代理 /api → Unisense 后端（默认 localhost:8100，docker-compose 端口避让）。
// 也可通过 .env 的 VITE_API_BASE_URL 直接指向后端（含 /api/v1）。
export default defineConfig({
  plugins: [react()],
  build: {
    // P1-3（第八轮）：入口块过大（dist 入口 index-*.js ~913KB）——api.ts（3000+ 行 +
    // 错误码大表）被所有页面静态引入，把稳定第三方依赖拆成独立 chunk：
    // 浏览器长期缓存 + 首屏并行加载，显著降低入口块体积与加载时间。
    rollupOptions: {
      output: {
        manualChunks: {
          react: ["react", "react-dom", "react-router-dom"],
          antd: ["antd", "@ant-design/icons", "@ant-design/charts"],
          g6: ["@antv/g6"],
          state: ["zustand"],
          util: ["pinyin-pro"],
        },
      },
    },
  },
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
