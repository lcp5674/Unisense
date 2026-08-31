"""API 文档本地化（自托管 Swagger UI / ReDoc）测试。

覆盖：
- ``_swagger_ui_html`` 输出无 CDN 引用、无 inline script，全部指向本地 /static/。
- ``/docs``、``/redoc`` 路由返回本地化 CSP（``script-src 'self'``），其余安全头保持。
- ``/static/`` 静态资源（swagger-ui-bundle.js 等）可访问（离线可用）。
- ``/openapi.json`` 保持全局严格 CSP（API 响应不因文档页放宽）。
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from app.core.middleware import _DOCS_CSP, _SECURITY_HEADERS
from app.main import _swagger_ui_html, app


def _make_client() -> TestClient:
    # 不带 with 使用：不触发 lifespan（不连 DB/Redis），仅验证路由/中间件行为
    return TestClient(app)


def test_swagger_ui_html_uses_only_local_assets() -> None:
    html = _swagger_ui_html(
        openapi_url="/openapi.json", title="Unisense API 文档"
    ).body.decode()
    # 无任何 CDN 引用（jsdelivr / unpkg / fastapi 官方）
    assert "jsdelivr" not in html
    assert "unpkg" not in html
    assert "fastapi.tiangolo" not in html
    # 全部本地静态资源
    assert "/static/swagger-ui/swagger-ui-bundle.js" in html
    assert "/static/swagger-ui/swagger-ui-standalone-preset.js" in html
    assert "/static/swagger-ui/swagger-init.js" in html
    assert "/static/swagger-ui/swagger-ui.css" in html
    assert "/static/swagger-ui/custom.css" in html
    # 初始化配置经 meta 注入（由 swagger-init.js 读取）
    assert 'name="swagger-config"' in html
    # 无 inline script（CSP script-src 'self' 无需 'unsafe-inline'）
    scripts = re.findall(r"<script[^>]*>", html)
    assert scripts, "应存在脚本标签"
    assert all(s.startswith('<script src="') for s in scripts)


def test_docs_returns_localized_csp_and_local_assets() -> None:
    client = _make_client()
    resp = client.get("/docs")
    assert resp.status_code == 200
    assert resp.headers["content-security-policy"] == _DOCS_CSP
    # 文档页允许同源 iframe 内嵌（前端 /api-docs），X-Frame-Options 同步 SAMEORIGIN
    assert resp.headers["x-frame-options"] == "SAMEORIGIN"
    body = resp.text
    assert "cdn.jsdelivr.net" not in body
    assert "/static/swagger-ui/swagger-ui-bundle.js" in body


def test_redoc_returns_localized_csp_and_local_assets() -> None:
    client = _make_client()
    resp = client.get("/redoc")
    assert resp.status_code == 200
    assert resp.headers["content-security-policy"] == _DOCS_CSP
    body = resp.text
    assert "cdn.jsdelivr.net" not in body
    assert "unpkg" not in body
    assert "/static/redoc/redoc.standalone.js" in body


def test_static_swagger_assets_served_locally() -> None:
    client = _make_client()
    for path in (
        "/static/swagger-ui/swagger-ui-bundle.js",
        "/static/swagger-ui/swagger-ui.css",
        "/static/swagger-ui/custom.css",
        "/static/swagger-ui/swagger-ui-standalone-preset.js",
        "/static/swagger-ui/swagger-init.js",
        "/static/swagger-ui/favicon.png",
        "/static/redoc/redoc.standalone.js",
    ):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert resp.headers["content-type"], path


def test_openapi_json_keeps_strict_csp() -> None:
    client = _make_client()
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    # API 响应保持全局严格 CSP（不被文档页策略放宽）
    assert resp.headers["content-security-policy"] == _SECURITY_HEADERS["Content-Security-Policy"]
