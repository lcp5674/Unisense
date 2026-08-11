"""RBAC 读闸门回归测试（D6：列表/查询端点统一鉴权）。

D6 之前，collector/lineage/conflict/semantic/governance 的列表与查询端点
仅挂 ``guard_against_injection``（防注入），**未挂角色闸门**——任何匿名请求
都能读取目录/参考类数据。本测试固化修复：这些端点在无有效 token 时必须返回
401 UNAUTHORIZED，证明读闸门已生效。

注解：已正确加闸的服务（assetmap/recommend/notify/observability 及 glossary/
dimension/quality 的单条 GET）不在本文件重复断言，其行为由各自 security 测试覆盖。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

#: (方法, 路径) —— D6 新增读闸的列表/查询端点。
READ_GATED_ENDPOINTS = [
    ("GET", "/api/v1/terms"),
    ("GET", "/api/v1/terms/conflicts"),
    ("GET", "/api/v1/dimensions"),
    ("GET", "/api/v1/dimensions/mappings"),
    ("GET", "/api/v1/dimensions/reconciliations"),
    ("GET", "/api/v1/dimensions/DC1/members"),
    ("GET", "/api/v1/dimensions/M1/metric-dimensions"),
    ("GET", "/api/v1/quality/rules"),
    ("GET", "/api/v1/quality/events"),
    ("GET", "/api/v1/data-sources"),
    ("GET", "/api/v1/data-sources/S1"),
    ("GET", "/api/v1/catalogs"),
    ("GET", "/api/v1/lineage/edges"),
    ("GET", "/api/v1/lineage/impact"),
    ("GET", "/api/v1/conflicts"),
    ("GET", "/api/v1/conflicts/1/rulings"),
    ("GET", "/api/v1/metric-definitions"),
    ("GET", "/api/v1/metric-definitions/M1"),
    ("GET", "/api/v1/metric-definitions/M1/versions"),
    ("GET", "/api/v1/grants"),
]


def test_read_gated_endpoints_require_auth() -> None:
    """无 token 访问 D6 新增读闸端点必须返回 401。

    通过临时清空依赖覆盖，强制走真实 ``get_current_user`` 鉴权路径；
    鉴权在 DB 访问之前失败，故无需真实数据库连接。
    """
    saved = dict(app.dependency_overrides)
    app.dependency_overrides.clear()
    try:
        client = TestClient(app)
        for method, path in READ_GATED_ENDPOINTS:
            resp = client.request(method, path)
            assert resp.status_code == 401, (
                f"{method} {path} 期望 401（读闸门已生效），实际 {resp.status_code}"
            )
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved)
