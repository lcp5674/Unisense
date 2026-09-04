"""数仓分层（dw_layer）字典驱动派生。

血缘/资产地图的表节点历史上仅按表名前缀（``ods_``/``dwd_``/…）硬编码推断分层，
库名携带分层（如 ``wedw_dwd.xxx``、``wedw_dim.dim_hospital``）的表大量落入
「未分层表」泳道。这里以 ``system_dict`` 的 ``dw_layer`` 字典为**唯一事实源**，
**同时支持两种字典 code 形态**：

- **整库名/库前缀形态**（多段 code，如 ``wedw_dwd``/``wedw_ods``）：库名以该 code
  开头（整库名本身或带子库后缀）即归属该层；
- **分段码形态**（单段 code，如 ``dwd``/``dim``）：库名按下划线拆段、从右往左命中
  首个 active 分段码即归属该层。

两种形态可并存、各匹配各的命名形态；字典未收录的段（dim/mid/st/tmp…）在管理员
补录字典项后自动归层，无需改码或重新采集。

与指标侧 ``Metric.dw_layer``（建指标时经字典校验落库）不同，表侧**不新增持久化
列**——分层在图谱读路径按节点 ``库.表`` 名实时派生，避免采集/同步链路改造与
存量回填，字典补录即时生效。
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.system_dict import SystemDict

#: dw_layer 字典类型编码（与 SystemDictService 种子/指标侧取值一致）
DW_LAYER_DICT_TYPE = "dw_layer"


def derive_dw_layer_from_catalog_name(
    entity_name: str,
    active_layer_codes: Iterable[str],
) -> str | None:
    """按 ``库.表`` 名派生数仓分层码（小写），命中字典 active 码才返回。

    判定顺序（支持整库名与分段码两种字典形态，均大小写不敏感）：

    1. **整库名/库前缀匹配**：遍历字典中含 ``_`` 的多段 code（形如
       ``wedw_dwd``/``wedw_ods`` 的整库名或库前缀），**最长优先**；库名等于该 code
       或以其加 ``_`` 开头（带子库后缀，如 ``wedw_dwd_bak``）即归属该层
       （``wedw_dwd.dw_order_df`` 且字典含 ``wedw_dwd`` → ``wedw_dwd``）。
    2. **分段码匹配**：库名（``.`` 前段）按 ``_`` 拆段，**从右往左**找首个命中
       单段分层码的段（``wedw_dwd.xxx`` 且字典含 ``dwd`` → ``dwd``；
       ``wedw_dim.dim_x`` 且字典含 ``dim`` → ``dim``）。从右往左是为了在
       ``wedw_dwd_ods`` 这类多段库里取最贴近表的分层段。
    3. 库名未命中时回退表名前缀（``ods_``/``dwd.`` 等），兼容库名不带分层但
       表名遵循分层命名惯例的源（``wedw.ods_xxx`` → ``ods``）。
    4. 都不命中返回 ``None``（保持「未分层」语义，前端归入未分层表泳道）。

    ``active_layer_codes`` 为 dw_layer 字典的 active 编码集合（调用方经
    :func:`load_active_dw_layer_codes` 一次性查入）；大小写归一化比较。
    """
    codes = {c.lower() for c in active_layer_codes if c}
    if not entity_name or not codes:
        return None
    full = entity_name.strip().lower()
    if not full:
        return None
    db_part, _, table_part = full.partition(".")
    # 1. 整库名/库前缀匹配（多段 code 优先，防短码如环境前缀提前命中）
    for code in sorted((c for c in codes if "_" in c), key=len, reverse=True):
        if db_part == code or db_part.startswith(f"{code}_"):
            return code
    # 2. 分段码：库名拆段从右往左
    for seg in reversed([s for s in db_part.split("_") if s]):
        if seg in codes:
            return seg
    # 3. 回退表名前缀
    for code in codes:
        if table_part.startswith(f"{code}_") or table_part.startswith(f"{code}."):
            return code
    return None


async def load_active_dw_layer_codes(db: AsyncSession) -> set[str]:
    """查 dw_layer 字典全部 active 编码（小写集合），供图谱读路径派生表分层。

    图谱每次构建调用一次（结果在单次请求内复用）；停用/软删的字典项不参与，
    保证「停用即不再归层」与指标侧字典校验口径一致。
    """
    rows = (
        await db.execute(
            select(SystemDict.code).where(
                SystemDict.dict_type == DW_LAYER_DICT_TYPE,
                SystemDict.status == "active",
                SystemDict.deleted_at.is_(None),
            )
        )
    ).all()
    return {(r[0] or "").lower() for r in rows if r[0]}
