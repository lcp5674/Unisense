"""基础设施混沌测试。"""

import pytest


@pytest.mark.asyncio
async def test_redis_unavailable_degrades():
    pass


@pytest.mark.asyncio
async def test_neo4j_unavailable_degrades():
    pass
