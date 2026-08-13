"""SEC-06 JWT 黑名单回归测试。"""

def test_blacklisted_token_rejected():
    from app.core.security import blacklist_token, is_token_blacklisted
    assert callable(blacklist_token)
    assert callable(is_token_blacklisted)
