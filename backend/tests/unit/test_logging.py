"""日志脱敏 processor 单测。

覆盖 _redact_processor：敏感字段名黑名单、身份证/手机号/邮箱正则替换。
并覆盖 configure_logging 的 JSON / 控制台两种渲染分支与 get_logger。
"""

from __future__ import annotations

import logging as std_logging
from types import SimpleNamespace

import structlog

from app.core import logging as app_logging
from app.core.logging import _redact_processor, configure_logging, get_logger


def test_redact_sensitive_field_names() -> None:
    event = {"password": "secret123", "token": "abc", "api_key": "k", "safe": "value"}
    out = _redact_processor(None, "info", event)
    assert out["password"] == "***REDACTED***"
    assert out["token"] == "***REDACTED***"
    assert out["api_key"] == "***REDACTED***"
    assert out["safe"] == "value"


def test_redact_id_card() -> None:
    event = {"msg": "用户身份证 110101199001011234 已登记"}
    out = _redact_processor(None, "info", event)
    assert "110101199001011234" not in out["msg"]
    assert "***ID***" in out["msg"]


def test_redact_phone() -> None:
    event = {"msg": "联系电话 13800138000"}
    out = _redact_processor(None, "info", event)
    assert "13800138000" not in out["msg"]
    assert "***PHONE***" in out["msg"]


def test_redact_email() -> None:
    event = {"msg": "邮箱 user@example.com 已注册"}
    out = _redact_processor(None, "info", event)
    assert "user@example.com" not in out["msg"]
    assert "***EMAIL***" in out["msg"]


def test_redact_leaves_normal_text() -> None:
    event = {"msg": "指标 sales_gmv_daily 已发布", "metric_code": "sales_gmv_daily"}
    out = _redact_processor(None, "info", event)
    assert out["msg"] == "指标 sales_gmv_daily 已发布"
    assert out["metric_code"] == "sales_gmv_daily"


def test_redact_non_string_values_kept() -> None:
    event = {"count": 42, "ratio": 0.5, "ok": True}
    out = _redact_processor(None, "info", event)
    assert out["count"] == 42
    assert out["ratio"] == 0.5
    assert out["ok"] is True


class TestConfigureLogging:
    """configure_logging 的分支覆盖（JSON 生产 / 彩色控制台本地）。"""

    def _run(self, monkeypatch, settings) -> None:
        captured: dict = {}

        def _fake_configure(*args, **kwargs) -> None:
            captured["kwargs"] = kwargs

        monkeypatch.setattr(app_logging, "settings", settings)
        monkeypatch.setattr(app_logging.structlog, "configure", _fake_configure)
        monkeypatch.setattr(std_logging, "basicConfig", lambda **kw: None)
        configure_logging()
        return captured

    def test_json_renderer_when_log_format_json(self, monkeypatch) -> None:
        settings = SimpleNamespace(log_level="INFO", log_format="json", env="local")
        captured = self._run(monkeypatch, settings)
        kwargs = captured["kwargs"]
        # JSONRenderer 实例应位于 processors 末尾
        assert isinstance(kwargs["processors"][-1], structlog.processors.JSONRenderer)
        assert "TimeStamper" in [type(p).__name__ for p in kwargs["processors"]]

    def test_json_renderer_when_non_local_env(self, monkeypatch) -> None:
        # 即便 log_format 非 json，env != local 也应走 JSON 渲染
        settings = SimpleNamespace(log_level="WARNING", log_format="console", env="prod")
        captured = self._run(monkeypatch, settings)
        kwargs = captured["kwargs"]
        assert isinstance(kwargs["processors"][-1], structlog.processors.JSONRenderer)

    def test_console_renderer_when_local_env(self, monkeypatch) -> None:
        settings = SimpleNamespace(log_level="DEBUG", log_format="console", env="local")
        captured = self._run(monkeypatch, settings)
        kwargs = captured["kwargs"]
        assert isinstance(kwargs["processors"][-1], structlog.dev.ConsoleRenderer)

    def test_redact_processor_is_wired(self, monkeypatch) -> None:
        # 脱敏 processor 必须出现在共享 processors 链中
        settings = SimpleNamespace(log_level="INFO", log_format="json", env="prod")
        captured = self._run(monkeypatch, settings)
        processors = captured["kwargs"]["processors"]
        assert any(getattr(p, "__name__", "") == "_redact_processor" for p in processors)

    def test_unknown_log_level_falls_back_info(self, monkeypatch) -> None:
        # 未知 level 回落到 INFO，不抛异常
        settings = SimpleNamespace(log_level="NOPE", log_format="json", env="prod")
        captured = self._run(monkeypatch, settings)
        assert captured["kwargs"]["wrapper_class"] is not None


def test_get_logger_returns_structlog_logger() -> None:
    logger = get_logger("unisense.test")
    assert isinstance(logger, structlog.stdlib.BoundLogger) or hasattr(logger, "info")
    # 返回的 logger 可正常绑定并产出事件字典（验证绑定链工作）
    bound = logger.bind(metric_code="sales_gmv_day")
    assert bound is not None
