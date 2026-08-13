"""SEC-02 密钥轮换集成测试。"""
from app.core.key_rotation import KeyRotationManager


def test_rotation_moves_active_to_decrypt_list():
    mgr = KeyRotationManager()
    mgr.initialize()
    old_active = mgr._active_key
    mgr.rotate_key("new-rotation-key-material")
    assert mgr._active_key != old_active
    assert old_active in mgr._decrypt_keys

def test_decrypt_with_old_key_after_rotation():
    mgr = KeyRotationManager()
    mgr.initialize()
    old_fernet = mgr.active_fernet
    token = old_fernet.encrypt(b"secret-data")
    mgr.rotate_key("new-rotation-key-material")
    assert mgr.decrypt_with_any_key(token) == b"secret-data"

def test_needs_rotation_after_90_days():
    from datetime import UTC, datetime, timedelta
    mgr = KeyRotationManager()
    mgr.initialize()
    old_date = datetime.now(UTC) - timedelta(days=91)
    assert mgr.needs_rotation(old_date) is True

def test_no_rotation_needed_recent_key():
    from datetime import UTC, datetime
    mgr = KeyRotationManager()
    mgr.initialize()
    recent = datetime.now(UTC)
    assert mgr.needs_rotation(recent) is False
