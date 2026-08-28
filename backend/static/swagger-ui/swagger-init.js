// Swagger UI 初始化脚本（本地化，外置 inline config）。
//
// 设计目标：docs 页面 CSP 可收紧到 script-src 'self'（无 'unsafe-inline'），
// 因此初始化配置不写在 HTML 内联 <script>，而是由本文件从 <meta> 标签读取。
// meta 标签由 backend/app/main.py 的 _swagger_ui_html() 渲染（见其 swagger-config content）。
(function () {
  "use strict";

  function readConfig() {
    var meta = document.querySelector('meta[name="swagger-config"]');
    if (!meta) {
      return {};
    }
    try {
      return JSON.parse(meta.getAttribute("content") || "{}");
    } catch (err) {
      // eslint-disable-next-line no-console
      console.error("Unisense swagger-config meta 解析失败", err);
      return {};
    }
  }

  var config = readConfig();
  // swagger-ui-dist@5.18+ 默认 dom_id 不再指向 #swagger-ui，必须显式指定渲染容器
  config.dom_id = "#swagger-ui";
  config.presets = [SwaggerUIBundle.presets.apis];
  // swagger-ui-dist@5 起 bundle 不再内置 SwaggerUIStandalonePreset，需独立引入
  // swagger-ui-standalone-preset.js（挂载到全局 window.SwaggerUIStandalonePreset）。
  // 有则启用 StandaloneLayout（品牌顶栏 + 搜索）；缺失时回退 BaseLayout 保证可渲染。
  if (typeof window.SwaggerUIStandalonePreset !== "undefined") {
    config.presets.push(window.SwaggerUIStandalonePreset);
    // preset 仅注册 StandaloneLayout 组件，必须显式指定 layout 才会启用顶栏
    config.layout = "StandaloneLayout";
  } else {
    config.layout = "BaseLayout";
  }
  // 兜底：meta 缺失时仍指向标准 openapi 地址（本应用固定 /openapi.json）
  if (!config.url) {
    config.url = "/openapi.json";
  }
  window.ui = SwaggerUIBundle(config);

  // ---- 顶栏品牌化：替换默认 "swagger" 标题为产品名 + 副标题（custom.css 已改 logo 图）----
  // Swagger UI 的 topbar 由 StandaloneLayout 异步渲染，轮询等待其出现后注入品牌内容；
  // 回退 BaseLayout（standalone-preset 缺失）时无 topbar，跳过即可（其余美化不受影响）。
  function brandTopbar() {
    var link = document.querySelector(".swagger-ui .topbar .link");
    if (!link) {
      return false;
    }
    var span = link.querySelector("span");
    if (span) {
      span.textContent = "Unisense 指标语义中台";
      span.className = "un-brand-text";
    }
    if (!link.querySelector(".un-brand-sub")) {
      var sub = document.createElement("span");
      sub.className = "un-brand-sub";
      sub.textContent = "开放 API 文档 v0.1.0";
      link.appendChild(sub);
    }
    return true;
  }
  var brandTries = 0;
  var brandTimer = setInterval(function () {
    if (brandTopbar() || ++brandTries > 40) {
      clearInterval(brandTimer);
    }
  }, 100);
})();
