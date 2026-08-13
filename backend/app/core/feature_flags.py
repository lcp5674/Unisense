"""特性开关模块（OPS-09: 特性开关框架）。

职责：
1. 按 name 查询开关状态
2. 按域/用户灰度判断
3. 管理接口 CRUD
4. Redis 存储 + 内存缓存

对齐 R&D-08: Redis Hash 存储，30s 刷新间隔。
"""

from __future__ import annotations

import time
from typing import Any

from app.core.logging import get_logger

logger = get_logger("unisense.feature_flags")

# 内存缓存刷新间隔（秒）
_CACHE_TTL = 30.0


class FeatureFlag:
    """特性开关（内存模型）。"""

    __slots__ = ("name", "enabled", "target_domains", "target_users", "description")

    def __init__(
        self,
        name: str,
        enabled: bool = False,
        target_domains: list[str] | None = None,
        target_users: list[int] | None = None,
        description: str | None = None,
    ) -> None:
        self.name = name
        self.enabled = enabled
        self.target_domains = target_domains or []
        self.target_users = target_users or []
        self.description = description

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "name": self.name,
            "enabled": self.enabled,
            "target_domains": self.target_domains,
            "target_users": self.target_users,
            "description": self.description,
        }


class FeatureFlagManager:
    """特性开关管理器（内存缓存 + 可选 Redis 持久化）。"""

    def __init__(self) -> None:
        self._flags: dict[str, FeatureFlag] = {}
        self._cache_at: float = 0.0

    def register_flag(
        self,
        name: str,
        enabled: bool = False,
        target_domains: list[str] | None = None,
        target_users: list[int] | None = None,
        description: str | None = None,
    ) -> FeatureFlag:
        """注册特性开关。"""
        flag = FeatureFlag(
            name=name,
            enabled=enabled,
            target_domains=target_domains,
            target_users=target_users,
            description=description,
        )
        self._flags[name] = flag
        logger.info("feature_flag_registered", name=name, enabled=enabled)
        return flag

    def is_feature_enabled(
        self,
        name: str,
        domain: str | None = None,
        user_id: int | None = None,
    ) -> bool:
        """判断特性开关是否对当前上下文启用。

        规则：
        1. 开关不存在 → 默认 False（安全侧）
        2. enabled=False → 全局禁用
        3. enabled=True 且无定向配置 → 全局启用
        4. 有 target_domains → domain 在列表中才启用
        5. 有 target_users → user_id 在列表中才启用

        Args:
            name: 开关名称。
            domain: 当前请求的域。
            user_id: 当前用户 ID。

        Returns:
            是否启用。
        """
        flag = self._flags.get(name)
        if flag is None:
            return False
        if not flag.enabled:
            return False
        # 有定向配置时，需满足条件
        if flag.target_domains and (domain is None or domain not in flag.target_domains):
            return False
        return not (
            flag.target_users and (user_id is None or user_id not in flag.target_users)
        )

    def get_flag(self, name: str) -> FeatureFlag | None:
        """获取指定开关。"""
        return self._flags.get(name)

    def get_all_flags(self) -> list[FeatureFlag]:
        """获取所有开关。"""
        return list(self._flags.values())

    def update_flag(
        self,
        name: str,
        enabled: bool | None = None,
        target_domains: list[str] | None = None,
        target_users: list[int] | None = None,
        description: str | None = None,
    ) -> FeatureFlag | None:
        """更新特性开关。"""
        flag = self._flags.get(name)
        if flag is None:
            return None
        if enabled is not None:
            flag.enabled = enabled
        if target_domains is not None:
            flag.target_domains = target_domains
        if target_users is not None:
            flag.target_users = target_users
        if description is not None:
            flag.description = description
        logger.info("feature_flag_updated", name=name, enabled=flag.enabled)
        return flag

    def refresh_from_redis(self, redis_client: Any) -> None:
        """从 Redis 刷新特性开关缓存（30s 间隔）。"""
        now = time.monotonic()
        if now - self._cache_at < _CACHE_TTL:
            return
        try:
            # 从 Redis Hash 读取特性开关
            keys = redis_client.hkeys("unisense:feature_flags")
            for key in keys:
                key_str = key if isinstance(key, str) else key.decode("utf-8")
                value = redis_client.hget("unisense:feature_flags", key_str)
                if value:
                    import json

                    data = json.loads(value if isinstance(value, str) else value.decode("utf-8"))
                    self._flags[key_str] = FeatureFlag(
                        name=key_str,
                        enabled=data.get("enabled", False),
                        target_domains=data.get("target_domains"),
                        target_users=data.get("target_users"),
                        description=data.get("description"),
                    )
            self._cache_at = now
        except Exception:
            logger.warning("feature_flags_redis_refresh_failed", exc_info=True)


# 模块级单例
_manager: FeatureFlagManager | None = None


def get_feature_flag_manager() -> FeatureFlagManager:
    """获取特性开关管理器单例。"""
    global _manager
    if _manager is None:
        _manager = FeatureFlagManager()
    return _manager


def init_feature_flag_manager() -> FeatureFlagManager:
    """初始化特性开关管理器（lifespan 中调用）。"""
    global _manager
    _manager = FeatureFlagManager()
    return _manager


def is_feature_enabled(
    name: str,
    domain: str | None = None,
    user_id: int | None = None,
) -> bool:
    """便捷函数：判断特性开关是否启用。"""
    return get_feature_flag_manager().is_feature_enabled(name, domain, user_id)
