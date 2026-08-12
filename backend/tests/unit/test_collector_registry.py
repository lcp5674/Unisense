"""CollectorRegistry 单元测试（对齐 US1 / FR-002）。

覆盖：
1. 7种类型注册 + list_types
2. build() 工厂方法
3. 未知类型报错
4. 装饰器注册 + 直接调用注册
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.core.exceptions import BusinessError
from app.services.collector.connectors.collector_registry import CollectorRegistry
from app.services.collector.spi import BaseCollector, CollectResult


class _StubCollector(BaseCollector):
    """测试用桩采集器。"""

    async def collect(self, source: object) -> CollectResult:
        return CollectResult(specs=[], failed_specs=[], source_id="test")


class TestCollectorRegistry:
    """CollectorRegistry 测试。"""

    def test_register_and_list_types(self):
        """注册采集器后 list_types 返回已注册类型。"""
        reg = CollectorRegistry()
        reg.register("mysql", lambda cfg: _StubCollector())
        reg.register("postgres", lambda cfg: _StubCollector())
        assert "mysql" in reg.list_types()
        assert "postgres" in reg.list_types()

    def test_list_types_sorted(self):
        """list_types 返回排序后的列表。"""
        reg = CollectorRegistry()
        reg.register("z_type", lambda cfg: _StubCollector())
        reg.register("a_type", lambda cfg: _StubCollector())
        assert reg.list_types() == ["a_type", "z_type"]

    def test_register_decorator(self):
        """使用 @registry.register 装饰器注册。"""
        reg = CollectorRegistry()

        @reg.register("test_type")
        def create_test_collector(cfg: dict) -> _StubCollector:
            return _StubCollector()

        assert "test_type" in reg.list_types()

    def test_register_decorator_preserves_function(self):
        """装饰器注册保留原函数。"""
        reg = CollectorRegistry()

        @reg.register("decorated")
        def create_collector(cfg: dict) -> _StubCollector:
            return _StubCollector()

        # 原函数仍可调用
        assert isinstance(create_collector({}), _StubCollector)

    def test_build_unknown_type_raises(self):
        """构建未注册类型时抛出 BusinessError。"""
        reg = CollectorRegistry()
        with pytest.raises(BusinessError, match="不支持的采集器类型"):
            reg.build("unknown_type", "encrypted_config")

    @patch("app.core.secrets.SecretManager.decrypt")
    def test_build_calls_factory_with_decrypted_config(self, mock_decrypt):
        """build() 解密配置后传给工厂函数。"""
        reg = CollectorRegistry()
        mock_factory = MagicMock(return_value=_StubCollector())
        reg.register("test_type", mock_factory)
        mock_decrypt.return_value = {"host": "localhost", "port": 3306}

        collector = reg.build("test_type", "encrypted_token")

        mock_decrypt.assert_called_once_with("encrypted_token")
        mock_factory.assert_called_once_with({"host": "localhost", "port": 3306})
        assert isinstance(collector, _StubCollector)

    def test_duplicate_register_overwrites(self):
        """重复注册同一类型会覆盖旧工厂。"""
        reg = CollectorRegistry()
        reg.register("mysql", lambda cfg: _StubCollector())

        new_collector = MagicMock()
        reg.register("mysql", new_collector)

        assert "mysql" in reg.list_types()
        # 第二次注册覆盖了第一次

    def test_seven_connector_types_registered(self):
        """验证全局 registry 注册了7种连接器类型。"""
        # 惰性导入触发注册
        from app.services.collector.connectors import registry

        types = registry.list_types()
        expected = ["clickhouse", "doris", "hive", "kafka", "mysql", "postgres", "starrocks"]
        assert sorted(types) == expected, f"Expected {expected}, got {sorted(types)}"
