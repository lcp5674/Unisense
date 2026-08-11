"""governance 被遗忘权（D9 / R7-09③）单元测试。

聚焦 ``execute_erasure`` 的去标识化逻辑与 ``_scrub_pii`` 的递归抹除能力，
不依赖真实数据库，使用轻量 FakeDB 模拟 AsyncSession 的 ``execute/scalars/all`` 与 ``add``。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from app.models.audit import AuditLog
from app.models.erasure import ErasureRequest, ErasureStatus
from app.services.governance.service import GovernanceService, _scrub_pii


class _ScalarResult:
    def __init__(self, rows: list[AuditLog]) -> None:
        self._rows = rows

    def scalars(self) -> _ScalarResult:
        return self

    def all(self) -> list[AuditLog]:
        return self._rows


class _FakeDB:
    """最小 AsyncSession 替身：支持 erasure 路径所需的 DB 调用。

    ``subject`` 模拟 DB 层 ``WHERE actor_id == :subject`` 过滤——被遗忘权只应
    命中主体自身的审计行，过滤是 DB 的职责，service 只负责擦除拿到的行。
    """

    def __init__(self, audit_rows: list[AuditLog], subject: int | None = None) -> None:
        self._audit_rows = audit_rows
        self._subject = subject
        self.added: list[object] = []
        self.committed = 0

    async def execute(self, _stmt: object) -> _ScalarResult:
        if self._subject is None:
            return _ScalarResult(self._audit_rows)
        return _ScalarResult([r for r in self._audit_rows if r.actor_id == self._subject])

    def add(self, obj: object) -> None:
        # 模拟 TimestampMixin 在 flush 时由 DB 默认值填充 created_at
        if getattr(obj, "created_at", None) is None:
            obj.created_at = datetime.now(UTC)
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.committed += 1


def _audit_row(actor_id: int, *, detail: dict | None, ip: str) -> AuditLog:
    return AuditLog(
        actor_id=actor_id,
        action="VIEW",
        entity_type="metric",
        entity_id="m1",
        detail_json=detail,
        ip=ip,
        trace_id="t",
    )


async def test_execute_erasure_scrubs_only_subject_rows() -> None:
    """仅 actor_id == subject 的审计行被去标识化，其余保持原样。"""
    subject_row = _audit_row(
        42, detail={"email": "a@b.com", "ip": "10.0.0.1"}, ip="10.0.0.1"
    )
    other_row = _audit_row(
        99, detail={"email": "z@b.com", "ip": "192.168.1.1"}, ip="192.168.1.1"
    )

    db = _FakeDB([subject_row, other_row], subject=42)
    svc = GovernanceService(db)
    erasure = await svc.execute_erasure(subject_user_id=42, operator_id=1, reason="GDPR")

    token = "ANONYMIZED_" + __import__("hashlib").sha256(b"42").hexdigest()[:16]

    # subject 行被覆写（detail_json 中 email 与 IP 均被替换为令牌）
    assert subject_row.ip == token
    assert subject_row.detail_json == {"email": token, "ip": token}
    # 其他用户行不受影响
    assert other_row.ip == "192.168.1.1"
    assert other_row.detail_json == {"email": "z@b.com", "ip": "192.168.1.1"}

    # 台账落库且字段正确
    assert erasure.subject_user_id == 42
    assert erasure.requested_by == 1
    assert erasure.status == ErasureStatus.COMPLETED
    assert erasure.token == token
    assert erasure.token[:12] == token[:12]
    assert erasure.affected_rows == 1
    assert erasure.reason == "GDPR"
    assert any(isinstance(a, ErasureRequest) for a in db.added)


async def test_execute_erasure_no_matching_rows_still_records() -> None:
    """无命中行时仍生成 COMPLETED 台账（去标识化对象存在但 affected_rows=0）。"""
    db = _FakeDB([])
    svc = GovernanceService(db)
    erasure = await svc.execute_erasure(subject_user_id=7, operator_id=1)
    assert erasure.affected_rows == 0
    assert erasure.status == ErasureStatus.COMPLETED
    assert erasure.token.startswith("ANONYMIZED_")


def test_scrub_pii_recursively_masks_pii() -> None:
    """_scrub_pii 递归抹除主体 id / 邮箱 / IPv4，保持 JSON 合法。"""
    token = "ANONYMIZED_deadbeefcafe1234"
    detail = {
        "uid": 42,
        "contact": {"email": "user@corp.io", "ip": "172.16.0.5"},
        "note": "subject 42 accessed",
        "clean": "no-pii-here",
    }
    out = _scrub_pii(detail, 42, token)
    text = json.dumps(out, ensure_ascii=False)

    assert token in text
    assert "user@corp.io" not in text
    assert "172.16.0.5" not in text
    # 主体 id 与其出现位置均被替换
    assert '"uid": 42' not in text
    assert "subject 42 accessed" not in text
    assert "no-pii-here" in text
    # 输出仍为合法 JSON
    assert json.loads(text) == out


def test_scrub_pii_none_detail_returns_none() -> None:
    assert _scrub_pii(None, 42, "TOKEN") is None
