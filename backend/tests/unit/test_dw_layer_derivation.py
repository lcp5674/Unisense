"""数仓分层字典驱动派生单测（system_dict.layers）。

核心不变量：表节点分层以 ``dw_layer`` 字典 active 码为唯一事实源——库名后缀
命中即归层，字典未收录的分层（dim/mid/st/tmp…）在补录字典前保持「未分层」，
补录后自动归层（无需改码/重新采集）。
"""

from app.services.system_dict.layers import derive_dw_layer_from_catalog_name

# 种子字典 5 标准层
SEED = {"ods", "dwd", "dws", "ads", "dm"}


def test_db_suffix_layer_wins_over_table_name():
    # 库名携带分层（wedw_dwd）即使表名不带 dwd_ 前缀也归 dwd
    assert derive_dw_layer_from_catalog_name("wedw_dwd.tjhis_order", SEED) == "dwd"


def test_dict_extended_layer_dim_after_dict_added():
    # dim 未收录时未分层；补录字典后自动归层
    assert derive_dw_layer_from_catalog_name("wedw_dim.dim_hospital", SEED) is None
    with_dim = SEED | {"dim"}
    assert derive_dw_layer_from_catalog_name("wedw_dim.dim_hospital", with_dim) == "dim"


def test_dict_extended_layers_mid_st():
    codes = SEED | {"mid", "st"}
    assert derive_dw_layer_from_catalog_name("wedw_mid.mid_order", codes) == "mid"
    assert derive_dw_layer_from_catalog_name("wedw_st.st_yy", codes) == "st"


def test_unregistered_tmp_layer_returns_none():
    # tmp 未收录字典 → 未分层（不臆造分层）
    assert derive_dw_layer_from_catalog_name("wedw_tmp.tmp_x", SEED) is None


def test_table_prefix_fallback_when_db_has_no_layer():
    # 库名不带分层、表名 ods_ 前缀 → 兜底 ods
    assert derive_dw_layer_from_catalog_name("wedw.ods_xxx", SEED) == "ods"


def test_case_insensitive():
    assert derive_dw_layer_from_catalog_name("WEDW_DWD.XXX", SEED) == "dwd"
    assert derive_dw_layer_from_catalog_name("wedw_DWS.y", {"DWS"}) == "dws"


def test_empty_codes_or_name():
    assert derive_dw_layer_from_catalog_name("wedw_dwd.x", set()) is None
    assert derive_dw_layer_from_catalog_name("", SEED) is None
    assert derive_dw_layer_from_catalog_name("   ", SEED) is None


def test_rightmost_segment_wins_for_multi_segment_db():
    # 多段库名取最贴近表的分层段（从右往左）
    assert derive_dw_layer_from_catalog_name("wedw_dwd_ods.t", SEED) == "ods"


def test_plain_table_name_with_layer_prefix():
    # 无库名（纯表名）按 _ 拆段仍能命中分层
    assert derive_dw_layer_from_catalog_name("dwd_order_detail", SEED) == "dwd"


def test_no_layer_returns_none():
    assert derive_dw_layer_from_catalog_name("gdc.some_table", SEED) is None
    assert derive_dw_layer_from_catalog_name("airflow.abc", SEED) is None


# ---- 方案 B：整库名/库前缀形态（多段 code，如 wedw_dwd/wedw_ods）----

#: 整库名形态字典（用户实际配置：wedw_ods/wedw_dwd + 标准码 DWS/ADS/DM）
FULL_DB_CODES = {"wedw_ods", "wedw_dwd", "dws", "ads", "dm"}


def test_full_db_name_code_matches_same_db():
    # 字典 code 配整库名 wedw_dwd → 库名 wedw_dwd 直接命中（此前拆段找 dwd 而 miss）
    assert (
        derive_dw_layer_from_catalog_name("wedw_dwd.dw_order_df", FULL_DB_CODES)
        == "wedw_dwd"
    )
    assert (
        derive_dw_layer_from_catalog_name("wedw_ods.ods_his", FULL_DB_CODES)
        == "wedw_ods"
    )


def test_full_db_code_with_db_suffix():
    # 库名以整库名 code + "_" 开头（带子库后缀）同样命中
    assert (
        derive_dw_layer_from_catalog_name("wedw_dwd_bak.dw_order_df", FULL_DB_CODES)
        == "wedw_dwd"
    )


def test_full_db_and_segment_codes_coexist():
    # 整库名与分段码并存：整库名形态更具体优先（wedw_dwd.dw_x → wedw_dwd 而非 dwd）
    mixed = FULL_DB_CODES | {"dwd", "ods"}
    assert (
        derive_dw_layer_from_catalog_name("wedw_dwd.dw_order_df", mixed) == "wedw_dwd"
    )
    # 字典无整库名 wedw_dws 时，拆段命中分段码 dws
    assert (
        derive_dw_layer_from_catalog_name("wedw_dws.dw_ord_df", mixed) == "dws"
    )
    # 纯分段码形态库（无整库名前缀）仍走拆段
    assert derive_dw_layer_from_catalog_name("dwd.plain_table", mixed) == "dwd"


def test_full_db_case_insensitive():
    assert (
        derive_dw_layer_from_catalog_name("WEDW_DWD.DW_ORDER_DF", {"WEDW_DWD"})
        == "wedw_dwd"
    )


def test_full_db_unrelated_db_returns_none():
    # 整库名 code 只匹配自身库名/前缀，不误伤其它库
    assert (
        derive_dw_layer_from_catalog_name("other_db.some_table", FULL_DB_CODES) is None
    )
