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
  config.presets = [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset];
  config.layout = "BaseLayout";
  // 兜底：meta 缺失时仍指向标准 openapi 地址（本应用固定 /openapi.json）
  if (!config.url) {
    config.url = "/openapi.json";
  }
  window.ui = SwaggerUIBundle(config);
})();
