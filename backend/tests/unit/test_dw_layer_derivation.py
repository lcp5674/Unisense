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
