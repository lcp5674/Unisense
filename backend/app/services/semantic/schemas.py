"""指标 Pydantic Schema 定义。

对齐 TD §3 API 接口规范和 DEV_GUIDE §8a.1（Schema 命名 PascalCase + 后缀）。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from app.services.metric_mount.schemas import MetricMountInput, MetricMountResponse

# ---- 请求 Schema ----


def _normalize_table_list(v: Any, key: str) -> list[str]:
    """表名列表规范化（``source_tables``/``downstream_tables`` 共用）。

    非数组拒绝（422）；元素去空白、转字符串、去重，与 db_catalog/血缘节点约定一致。
    """
    if not isinstance(v, list):
        raise ValueError(f"{key} 必须为数据表名数组")
    seen: set[str] = set()
    cleaned: list[str] = []
    for t in v:
        name = str(t).strip()
        if name and name not in seen:
            seen.add(name)
            cleaned.append(name)
    return cleaned


def _validate_definition_json(v: dict[str, Any]) -> dict[str, Any]:
    """口径定义结构校验与规范化（FR-07 生产化）。

    1. ``sql``：若提供，用 sqlglot 做语法校验，非法 SQL 拒绝（422）。
    2. ``source_tables``（上游依赖表）与 ``downstream_tables``（下游使用表）：
       若提供，规范化为去重字符串数组（指标锚定的数据表，与 db_catalog/血缘节点
       约定一致）。
    3. ``pseudo_definition``（系统开发伪代码口径）与 ``dw_definition``
       （数仓开发详细口径）：去空白字符串规范化（纯文本，不做 SQL 强校验）。
    4. 仅做校验与规范化，不新增字段、不改变未提供字段。
    """
    sql = v.get("sql")
    if sql is not None:
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError("口径 SQL（definition_json.sql）必须为非空字符串")
        try:
            import sqlglot

            sqlglot.parse_one(sql)
        except Exception as exc:  # noqa: BLE001 - sqlglot 语法错误统一 422
            raise ValueError(f"口径 SQL 语法错误: {exc}") from exc

    for key in ("source_tables", "downstream_tables"):
        if v.get(key) is not None:
            v[key] = _normalize_table_list(v[key], key)
    for key in ("pseudo_definition", "dw_definition"):
        val = v.get(key)
        if val is not None:
            if not isinstance(val, str) or not val.strip():
                raise ValueError(f"{key} 必须为非空字符串")
            v[key] = val.strip()
    # 5. ``base_atomic``（基础原子指标编码）：派生指标的 OneData 基础原子绑定
    #    （派生 = 基础原子 + 业务限定 + 时间周期）。选填字符串——存在性/类型
    #    （必须为原子类型）在 service 层校验（需查 DB），此处仅做格式规范化。
    if v.get("base_atomic") is not None:
        if not isinstance(v["base_atomic"], str) or not v["base_atomic"].strip():
            raise ValueError("基础原子指标（definition_json.base_atomic）必须为非空字符串")
        v["base_atomic"] = v["base_atomic"].strip()
    return v


def _validate_guide_list(v: list[str], key: str) -> list[str]:
    """单组消费指南字符串数组校验（≤20 项/每项 ≤200 字符，去空白、去空项）。

    Args:
        v: 字符串列表。
        key: 字段名（recommended_usage/cautions/related_metrics，用于报错）。

    Returns:
        清洗后的列表。

    Raises:
        ValueError: 非数组、超项数或单项超长。
    """
    if not isinstance(v, list):
        raise ValueError(f"消费指南.{key} 必须为字符串数组")
    if len(v) > 20:
        raise ValueError(f"消费指南.{key} 最多 20 项")
    cleaned: list[str] = []
    for item in v:
        text = str(item).strip()
        if not text:
            continue
        if len(text) > 200:
            raise ValueError(f"消费指南.{key} 单项最多 200 字符")
        cleaned.append(text)
    return cleaned


def _validate_guide_payload(v: dict[str, Any] | None) -> dict[str, Any] | None:
    """消费指南结构校验与规范化（对齐 consumption_guide JSON 列语义）。

    recommended_usage/cautions/related_metrics 均为字符串数组：每项去空白、
    非空、≤200 字符、列表 ≤20 项；非数组或超长拒绝（422）。不改变未提供字段。
    """
    if v is None:
        return v
    if not isinstance(v, dict):
        raise ValueError("消费指南必须为对象")
    for key in ("recommended_usage", "cautions", "related_metrics"):
        val = v.get(key)
        if val is None:
            continue
        v[key] = _validate_guide_list(val, key)
    return v


class MetricCreateRequest(BaseModel):
    """创建指标请求。

    对齐 TD §3 POST /api/v1/metric-definitions。
    OneData（界限文档 §2.3，变体口径）：原子指标 = 逻辑度量（measure_id）+ 基础统计
    粒度（日），不含业务限定与时间周期，不绑物理表；派生 = 原子 + 业务限定 + 周期，
    粒度下沉挂载实体（granularity 可选，派生指标由 mount 承载）。
    """

    metric_code: str | None = Field(
        None,
        max_length=64,
        description="指标编码（4段式，缺省由系统按源表/度量列/周期自动生成）",
    )
    name: str = Field(..., max_length=128, description="指标名称")
    domain: str = Field(..., max_length=64, description="所属域")
    type: Literal["atomic", "derived", "composite"] = Field(
        ..., description="指标类型: atomic/derived/composite"
    )
    # OneData 粒度下沉：granularity 不再必填——派生指标由 mount（挂载实体）承载粒度，
    # 原子/复合不设粒度（界限文档 §2.3 第 3 条：粒度属挂载层，不进指标定义）。
    granularity: str | None = Field(
        None,
        max_length=64,
        description="粒度（已下沉挂载实体 metric_mount；派生创建时由 mount 回填）",
    )
    # OneData 原子层：原子指标关联逻辑度量目录（度量格式/单位/小数位/源头系统/同义词继承）。
    # 派生/复合不直接关联（继承自原子），可空。
    measure_id: int | None = Field(
        None,
        ge=1,
        description="关联逻辑度量 ID（原子指标 OneData 必填；派生/复合继承可空）",
    )
    # 挂载实体（OneData 挂载层）：派生指标专用——源表/源列/粒度/默认周期/业务域。
    # 创建后 service 自动落 metric_mount 并回填 metric.granularity。
    mount: MetricMountInput | None = Field(
        None, description="挂载实体（兼容保留：单挂载快捷字段，等价 mounts=[mount]）"
    )
    # 多变体挂载（2026-08-27 放开一指标一挂载）：派生指标一次创建可挂多行——
    # 每行 = 一个变体（粒度/业务限定/周期组合）。缺省取 mount 兼容字段。
    mounts: list[MetricMountInput] | None = Field(
        None, description="挂载实体列表（多变体：粒度×业务限定×周期组合）"
    )
    # 指标级业务限定兜底（OneData 派生 = 基础原子 + 业务限定 + 周期）：落
    # definition_json.business_filter，挂载行 business_filter 缺省继承。
    default_business_filter: str | None = Field(
        None, max_length=512, description="指标级业务限定兜底（挂载行缺省继承）"
    )
    unit: str | None = Field(
        None,
        max_length=32,
        description=(
            "单位（OneData：原子指标由逻辑度量 default_unit 继承，缺省则继承；"
            "派生/复合缺省用默认）"
        ),
    )
    currency: str | None = Field(None, max_length=16, description="币种")
    # 与字典种子对齐（10 值）：SUM/AVG/COUNT/COUNT_DISTINCT/LAST_VALUE/FIRST_VALUE
    # + MAX/MIN/MEDIAN/PERCENTILE（FIRST_VALUE 由 SQL 推断产出，如余额首值场景）
    aggregation: Literal[
        "SUM", "AVG", "COUNT", "COUNT_DISTINCT", "LAST_VALUE", "FIRST_VALUE",
        "MAX", "MIN", "MEDIAN", "PERCENTILE",
    ] = Field(
        ...,
        description="聚合方式: SUM/AVG/COUNT/COUNT_DISTINCT/LAST_VALUE/MAX/MIN/MEDIAN/PERCENTILE",
    )
    # 与字典种子对齐（6 值）：PERIOD/YTD/TTM/AVG + MOM/YOY
    time_semantics: Literal["PERIOD", "YTD", "TTM", "AVG", "MOM", "YOY"] | None = Field(
        None, description="时间语义（缺省 PERIOD）"
    )
    # 与字典种子对齐（4 值）：REALTIME/T0/T1/HOURLY
    freshness: Literal["REALTIME", "T0", "T1", "HOURLY"] | None = Field(
        None, description="新鲜度（缺省 T1）"
    )
    dw_layer: Literal["ODS", "DWD", "DWS", "ADS", "DM"] | None = Field(
        None, description="数仓分层（缺省 DWD）"
    )
    metric_tier: Literal["T1", "T2", "T3"] = Field("T3", description="指标分级: T1/T2/T3")
    serving_mode: Literal["BATCH_ONLY", "REALTIME_ONLY", "BATCH_REALTIME_DUAL"] = Field(
        "BATCH_ONLY", description="服务模式"
    )
    additivity: Literal["ADDITIVE", "SEMI_ADDITIVE", "NON_ADDITIVE"] = Field(
        "ADDITIVE", description="可加性: ADDITIVE/SEMI_ADDITIVE/NON_ADDITIVE"
    )
    non_additive_dimensions: list[str] | None = Field(None, description="不可加维度列表")
    definition_json: dict[str, Any] = Field(..., description="口径定义")
    pii_flag: bool = Field(False, description="是否含 PII")
    sla: str | None = Field(None, max_length=128, description="SLA 契约")
    # 口径三方责任（PRD 4.5 补充，均可空）：产品需求方/技术方/数仓开发（user.id）。
    # 与 owner_id 同模式——从"指标归谁管"细化为"口径从需求到落地谁负责"。
    product_owner_id: int | None = Field(
        None, description="产品需求方用户 ID（口径业务需求提出人）"
    )
    tech_owner_id: int | None = Field(None, description="技术方用户 ID（口径 ETL/SQL 实现人）")
    dw_developer_id: int | None = Field(None, description="数仓开发用户 ID（数仓建模/血缘维护人）")
    # 外部人员名称兜底（责任方非平台用户时直接填名称，id 为空）：展示优先级 id>name
    product_owner_name: str | None = Field(
        None, max_length=128, description="产品需求方名称（非平台用户直接填写）"
    )
    tech_owner_name: str | None = Field(
        None, max_length=128, description="技术方名称（非平台用户直接填写）"
    )
    dw_developer_name: str | None = Field(
        None, max_length=128, description="数仓开发名称（非平台用户直接填写）"
    )
    # 自动推断辅助字段（FR-010/FR-011）：传入后由 Service 层 auto_fill 补全缺失字段
    source_table: str | None = Field(
        None, max_length=256, description="源表名（用于自动推断编码和数仓层）"
    )
    measure_column: str | None = Field(
        None, max_length=128, description="度量列名（用于自动推断编码和指标类型）"
    )
    period: str | None = Field(
        None, max_length=32, description="统计周期（用于自动推断编码和粒度）"
    )
    # P0-C：批量注册批次 ID（可空）——批量创建的指标带 batch_id 可回溯整批（"这一批
    # 50 个"在创建后与单条可区分）；单条创建为 None。落 Metric.batch_id（有索引）。
    batch_id: str | None = Field(
        None, max_length=64, description="批量注册批次 ID（可空，单条创建为 None）"
    )
    # 口径溯源（生产就绪审查 P2）：SQL 批量/口径 SQL 模式创建时携带整句原始口径 SQL
    # （ETL 脚本原文切片），落 Metric.raw_sql——候选仅落聚合表达式，整句口径原文
    # 可据此反查（batch_id → 口径全文），存量/普通创建为 None。
    raw_sql: str | None = Field(None, description="原始口径 SQL（可空，供 batch_id 溯源）")
    # 消费指南（可选）：创建时随指标落库（guide_source=manual），结构校验见
    # _validate_guide_payload（三组字符串数组，≤20 项/每项 ≤200 字符）。
    consumption_guide: dict[str, Any] | None = Field(
        None,
        description=(
            "消费指南：recommended_usage/cautions/related_metrics 三组字符串数组"
        ),
    )

    @field_validator("metric_code")
    @classmethod
    def validate_code(cls, v: str | None) -> str | None:
        """校验指标编码格式: 域_业务对象_度量_统计周期（4 段式 + 保留词）。

        缺省（None）时由 Service 层按自动生成逻辑补全；显式提供时委托
        ConflictPrechecker.validate_code_format 做严格校验。
        """
        if v is None:
            return v
        from app.services.semantic.conflict_precheck import ConflictPrechecker

        valid, error = ConflictPrechecker.validate_code_format(v)
        if not valid:
            raise ValueError(error)
        return v

    @field_validator("definition_json")
    @classmethod
    def validate_definition(cls, v: dict[str, Any]) -> dict[str, Any]:
        """口径定义：SQL 语法校验 + source_tables 规范化。"""
        return _validate_definition_json(v)

    @field_validator("consumption_guide")
    @classmethod
    def validate_guide(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        """消费指南：结构校验与规范化（三组字符串数组）。"""
        return _validate_guide_payload(v)

    @model_validator(mode="after")
    def _merge_mount_into_mounts(self) -> MetricCreateRequest:
        """挂载兼容合并（2026-08-27 多变体）：旧字段 ``mount``（单数）并入 ``mounts``。

        - 仅传 ``mount`` → 等价 ``mounts=[mount]``（旧前端单挂载创建不受影响）；
        - 两者同时提供视为契约冲突（前端应只发 ``mounts``），422 拦截；
        - 仅派生指标可挂载（原子=逻辑度量不挂表、复合=派生组合不直接挂表），
          非派生提供挂载在 schema 层即拒绝，与 service 更新路径口径一致。
        """
        if self.mounts is None and self.mount is not None:
            self.mounts = [self.mount]
        elif self.mounts is not None and self.mount is not None:
            raise ValueError("mount 与 mounts 不能同时提供，请使用 mounts（多变体列表）")
        if self.mounts and self.type != "derived":
            raise ValueError(
                f"仅派生指标可挂载，当前类型 {self.type}（原子=逻辑度量不挂表，复合=派生组合不直接挂表）"
            )
        return self

    @model_validator(mode="after")
    def validate_definition_by_type(self) -> MetricCreateRequest:
        """按指标类型校验口径定义完整性（注册门禁，PRD 4.5 / TD §12.2 / OneData）。

        三类指标在生产中的配置差异：
        - ``atomic``：OneData 原子层 = 逻辑度量 + 基础统计粒度（日）变体标签（DEV_GUIDE
          §7a 共识，不含业务限定与时间周期），不绑物理表。注册须关联
          逻辑度量（``measure_id``）——度量格式/单位/小数位/源头系统/同义词从度量目录
          继承；技术口径（``expression``/``sql``）可选。兼容旧式物理来源（来源表 +
          度量列，批量注册等存量路径），保证渐进迁移不破坏既有流。
        - ``derived``：OneData 派生层 = 原子指标 + 业务限定 + 时间周期（月/周/季/年
          等）。依赖指标（``dependencies``）**可选**——纯周期/业务限定派生（如「本月
          活跃医生数」）可无依赖直建；带依赖时仍构建 ``DERIVED_FROM`` 血缘并在发布时
          做依赖 PUBLISHED 校验与环检测。派生可携带 ``mount``（挂载实体，承载源表/
          粒度），粒度不再进指标定义。
        - ``composite``：OneData 复合层 = 多指标四则运算/比率。**须声明依赖指标**
          （``dependencies`` 非空）+ 计算表达式（``expression``），发布时强校验。

        缺失关键配置的草稿无消费价值且血缘断链，注册即拦截（422）。
        """
        defn = self.definition_json or {}
        if self.type == "atomic":
            has_measure = self.measure_id is not None
            has_physical_source = bool(
                defn.get("source_table") or defn.get("source_tables") or self.source_table
            )
            has_field = bool(
                defn.get("measure_column")
                or defn.get("source_field")
                or defn.get("measures")
                or self.measure_column
            )
            # OneData：原子须关联逻辑度量；兼容旧式物理来源（渐进迁移，批量注册等存量路径）
            if not has_measure and not (has_physical_source and has_field):
                raise ValueError(
                    "原子指标必须关联逻辑度量（measure_id，OneData 原子层），"
                    "或兼容旧式物理来源（同时提供来源表 source_table 与度量列）"
                )
        else:
            deps = defn.get("dependencies")
            has_sql = bool(defn.get("sql"))
            expr = defn.get("expression")
            # 复合强制依赖（多指标运算）；派生依赖可选（OneData 派生 = 原子 + 业务限定 +
            # 时间周期，纯周期/业务限定派生可无依赖——「本月活跃医生数」不依赖其他指标）
            if self.type == "composite" and (not isinstance(deps, list) or not deps):
                raise ValueError(
                    "复合指标必须声明至少 1 个依赖指标（definition_json.dependencies）"
                )
            # SQL 模式口径（sql）本身即计算主体，表达式可缺省
            if not has_sql and (not isinstance(expr, str) or not expr.strip()):
                raise ValueError("派生/复合指标必须填写计算表达式（definition_json.expression）")
        return self


class MetricUpdateRequest(BaseModel):
    """更新指标请求。"""

    name: str | None = Field(None, max_length=128)
    granularity: str | None = Field(None, max_length=64)
    # OneData 原子层：更换逻辑度量属口径变更（破坏性，触发版本确认）。
    # 传 None 表示不修改；派生/复合可空（继承自原子）。
    measure_id: int | None = Field(
        None, ge=1, description="关联逻辑度量 ID（更换=破坏性口径变更）"
    )
    # 挂载实体（派生指标）：提供则 upsert metric_mount 并回填 granularity
    mount: MetricMountInput | None = Field(
        None, description="挂载实体（兼容保留：单挂载快捷字段，等价 mounts=[mount]）"
    )
    # 多变体挂载（2026-08-27 放开一指标一挂载）：传列表则全量 diff 对齐——
    # 有 id 更新、无 id 新增、未出现在请求的删除；传 [] 清空全部挂载。
    mounts: list[MetricMountInput] | None = Field(
        None, description="挂载实体列表（多变体全量 diff：传 [] 清空）"
    )
    # 指标级业务限定兜底：写 definition_json.business_filter，挂载行缺省继承。
    default_business_filter: str | None = Field(
        None, max_length=512, description="指标级业务限定兜底（挂载行缺省继承）"
    )
    unit: str | None = Field(None, max_length=32)
    currency: str | None = Field(None, max_length=16, description="币种（治理属性，非破坏性变更）")
    # ---- 治理属性（TD §12.1 治理补充，非破坏性变更——不触发版本递增/PENDING 期）----
    # 指标创建后治理字段（数仓分层/时效/时间语义/分级/聚合/服务模式/可加性）常需调整
    # （分层纠正、时效调整、分级晋升、币种修正等生产高频场景），此前只能重建指标。
    # 修复：更新请求支持治理字段，service 直接更新主表治理列（不改变口径定义，非破坏性）。
    aggregation: Literal[
        "SUM", "AVG", "COUNT", "COUNT_DISTINCT", "LAST_VALUE", "FIRST_VALUE",
        "MAX", "MIN", "MEDIAN", "PERCENTILE",
    ] | None = Field(None, description="聚合方式（治理属性）")
    time_semantics: Literal["PERIOD", "YTD", "TTM", "AVG", "MOM", "YOY"] | None = Field(
        None, description="时间语义（治理属性）"
    )
    freshness: Literal["REALTIME", "T0", "T1", "HOURLY"] | None = Field(
        None, description="新鲜度（治理属性）"
    )
    dw_layer: Literal["ODS", "DWD", "DWS", "ADS", "DM"] | None = Field(
        None, description="数仓分层（治理属性）"
    )
    metric_tier: Literal["T1", "T2", "T3"] | None = Field(None, description="指标分级（治理属性）")
    serving_mode: Literal["BATCH_ONLY", "REALTIME_ONLY", "BATCH_REALTIME_DUAL"] | None = Field(
        None, description="服务模式（治理属性）"
    )
    additivity: Literal["ADDITIVE", "SEMI_ADDITIVE", "NON_ADDITIVE"] | None = Field(
        None, description="可加性（治理属性）"
    )
    non_additive_dimensions: list[str] | None = Field(None, description="不可加维度（治理属性）")
    definition_json: dict[str, Any] | None = Field(None, description="口径定义")
    sla: str | None = Field(None, max_length=128)
    consumption_guide: dict[str, Any] | None = Field(None, description="消费指南")
    backup_owner_id: int | None = Field(None, description="副 Owner ID")
    # 口径三方责任（非破坏性变更，不触发版本确认）：产品需求方/技术方/数仓开发（user.id）
    product_owner_id: int | None = Field(
        None, description="产品需求方用户 ID（口径业务需求提出人）"
    )
    tech_owner_id: int | None = Field(None, description="技术方用户 ID（口径 ETL/SQL 实现人）")
    dw_developer_id: int | None = Field(None, description="数仓开发用户 ID（数仓建模/血缘维护人）")
    # 外部人员名称兜底（责任方非平台用户时直接填名称，id 为空）：展示优先级 id>name
    product_owner_name: str | None = Field(
        None, max_length=128, description="产品需求方名称（非平台用户直接填写）"
    )
    tech_owner_name: str | None = Field(
        None, max_length=128, description="技术方名称（非平台用户直接填写）"
    )
    dw_developer_name: str | None = Field(
        None, max_length=128, description="数仓开发名称（非平台用户直接填写）"
    )
    change_reason: str = Field(..., min_length=4, description="变更原因")
    row_version: int | None = Field(
        None,
        ge=1,
        description=(
            "乐观锁版本号（编辑时回传当前 row_version；"
            "不传则向后兼容，不启用跨请求乐观锁校验）"
        ),
    )

    @field_validator("definition_json")
    @classmethod
    def validate_definition(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        """口径定义：SQL 语法校验 + source_tables 规范化。"""
        return _validate_definition_json(v) if v is not None else v

    @model_validator(mode="after")
    def _merge_mount_into_mounts(self) -> MetricUpdateRequest:
        """挂载兼容合并（2026-08-27 多变体）：旧字段 ``mount``（单数）并入 ``mounts``。

        与创建请求同规则：仅 ``mount`` → ``mounts=[mount]``；两者同传 422；
        非派生指标携带挂载 → 422（原子/复合不挂表）。
        """
        if self.mounts is None and self.mount is not None:
            self.mounts = [self.mount]
        elif self.mounts is not None and self.mount is not None:
            raise ValueError("mount 与 mounts 不能同时提供，请使用 mounts（多变体列表）")
        return self


class MetricDescriptionUpdateRequest(BaseModel):
    """指标业务描述更新请求（治理补充 TD §12.1，不触发版本/不参与口径变更）。

    传空串表示清除描述。仅 metric_owner / 域管理员 / 平台管理员可操作。
    """

    description: str = Field(
        "", max_length=2000, description="指标业务描述（传空串清除）"
    )
    row_version: int | None = Field(
        None,
        ge=1,
        description=(
            "乐观锁版本号（编辑时回传当前 row_version；"
            "不传则向后兼容，不启用跨请求乐观锁校验）"
        ),
    )


class MetricTermBindRequest(BaseModel):
    """指标↔术语绑定请求（P2-11：术语绑定写路径）。

    绑定指标到已存在的业务术语（``metric.term_id``），传 None 解绑。
    仅 metric_owner / 域管理员 / 平台管理员可操作。
    """

    term_id: int | None = Field(
        None, ge=1, description="术语 ID（传 null 解绑）"
    )


class MetricPublishRequest(BaseModel):
    """发布指标请求（DRAFT → PUBLISHED）。"""

    version: int | None = Field(None, ge=1, description="待发布版本号（缺省为当前版本）")
    change_reason: str = Field(..., min_length=4, description="发布说明")


class MetricDeprecateRequest(BaseModel):
    """废弃指标请求（successor_code 选填，对齐 FR-039/FR-002）。

    替代指标选填：存在「指标因口径失效被下线、无替代」的合法场景。
    为空时表示无替代（后端不校验替代，指标直接废弃）。
    """

    successor_code: str | None = Field(
        default=None,
        max_length=64,
        description="替代指标编码（选填，须为已 PUBLISHED 指标；留空表示无替代）",
    )


class MetricSubmitRequest(BaseModel):
    """提交审核请求（DRAFT → REVIEW，对齐 FR-003）。

    评审指派（TD §13）：可指定评审用户（reviewer_type=user + reviewer_id）或
    域评审组（reviewer_type=domain + reviewer_domain，缺省用指标自身域）。
    均不传则未指派——由域管理员兜底评审。
    """

    change_reason: str = Field(..., min_length=4, description="提交审核说明")
    reviewer_id: int | None = Field(
        None, description="指定评审用户 ID（reviewer_type=user 时必填）"
    )
    reviewer_type: Literal["user", "domain"] | None = Field(
        None, description="评审指派类型: user(指定用户)/domain(域评审组)"
    )
    reviewer_domain: str | None = Field(
        None,
        max_length=64,
        description="域评审组所在域（reviewer_type=domain 时生效，缺省用指标自身域）",
    )

    @field_validator("reviewer_id", "reviewer_domain", mode="after")
    @classmethod
    def _empty_to_none(cls, v: Any) -> Any:
        """空字符串/0 归一为 None，前端未选择时传空串/0 不致校验失败。"""
        if v is None:
            return v
        if isinstance(v, str) and not v.strip():
            return None
        if isinstance(v, int) and v <= 0:
            return None
        return v


# ---- 批量操作 Schema（TD §13 批量治理：提交/通过/打回/下线，逐条收集结果不整体失败）----
# 提交（BatchSubmitRequest）/ 打回（BatchRejectRequest）/ 响应（BatchResponse）等
# 通用结构统一在 app.api.batch_common（四模块复用）；此处仅保留指标特有参数
# （approve 灰度发布 / deprecate 替代指标）。


class MetricBatchApproveRequest(BaseModel):
    """批量审核通过请求（REVIEW → PUBLISHED/EXPERIMENTAL，即批量发布）。"""

    metric_codes: list[str] = Field(..., min_length=1, max_length=100)
    mode: Literal["standard", "experimental"] = Field(
        "standard", description="发布模式: standard(全量)/experimental(灰度)"
    )
    gray_tenant_ids: list[int] | None = Field(None, description="灰度白名单租户 ID")


class MetricBatchDeprecateItem(BaseModel):
    """批量下线（废弃）的单条项。"""

    metric_code: str = Field(..., max_length=64, description="指标编码")
    successor_code: str | None = Field(
        None,
        max_length=64,
        description=(
            "替代指标编码（须已发布；无下游引用时选填，有下游引用未填将被 "
            "METRIC_REFERENCED 拦截）"
        ),
    )


class MetricBatchDeprecateRequest(BaseModel):
    """批量下线（废弃）请求。"""

    items: list[MetricBatchDeprecateItem] = Field(..., min_length=1, max_length=100)


class MetricBatchReactivateRequest(BaseModel):
    """批量重新启用已废弃指标请求（P2-1：DEPRECATED → DRAFT，对齐维度 batch-reactivate）。"""

    metric_codes: list[str] = Field(..., min_length=1, max_length=100)


class MetricDownstreamCheckRequest(BaseModel):
    """批量下线下游使用审查请求（批量下线弹窗预审用）。"""

    metric_codes: list[str] = Field(..., min_length=1, max_length=100)


class MetricDownstreamReferrer(BaseModel):
    """单个下游引用者。"""

    node: str = Field(..., description="引用者节点：metric:{code} 派生 / consumer:{name} 消费")
    edge_type: str = Field(..., description="边类型：DERIVED_FROM 派生 / CONSUMED_BY 消费")


class MetricDownstreamCheckResult(BaseModel):
    """单个指标的下游使用审查结果。"""

    metric_code: str = Field(..., description="指标编码")
    referrer_count: int = Field(..., description="活跃下游引用数量")
    referrers: list[MetricDownstreamReferrer] = Field(
        default_factory=list, description="下游引用者明细"
    )


class MetricApproveRequest(BaseModel):
    """审核通过请求（REVIEW → PUBLISHED/EXPERIMENTAL，对齐 FR-004）。"""

    mode: Literal["standard", "experimental"] = Field(
        "standard", description="发布模式: standard(全量)/experimental(灰度)"
    )
    gray_tenant_ids: list[int] | None = Field(
        None, description="灰度白名单租户 ID（仅 experimental 模式）"
    )
    target_version: int | None = Field(None, ge=1, description="待发布版本号（缺省为当前版本）")


class MetricRejectRequest(BaseModel):
    """审核驳回请求（REVIEW → DRAFT，对齐 FR-005）。"""

    reason: str = Field(..., min_length=4, description="驳回原因")


class MetricReviewRequest(BaseModel):
    """评审请求（FR-07，approve → PUBLISHED / reject → DRAFT）。"""

    approved: bool = Field(..., description="评审结论：True 通过并发布，False 打回 DRAFT")
    change_reason: str | None = Field(
        None, description="变更说明（通过时建议附口径变更理由，驳回时可为空）"
    )


class MetricSubmitReviewRequest(BaseModel):
    """提交评审请求（FR-07，DRAFT → REVIEW）。"""

    change_reason: str = Field(..., min_length=4, description="提交评审说明")


class MetricEmergencyPublishRequest(BaseModel):
    """紧急发布请求（DRAFT → PUBLISHED 跳过 REVIEW，对齐 FR-022）。"""

    reason: str = Field(..., min_length=10, description="紧急发布原因")
    target_version: int | None = Field(None, ge=1, description="待发布版本号（缺省为当前版本）")


class VersionConfirmRequest(BaseModel):
    """消费方确认版本请求（对齐 FR-007）。"""

    version: int = Field(..., ge=1, description="版本号")


class VersionRejectRequest(BaseModel):
    """消费方拒绝版本请求（对齐 FR-007）。"""

    version: int = Field(..., ge=1, description="版本号")
    reason: str = Field(..., min_length=4, description="拒绝原因")


class VersionExtendRequest(BaseModel):
    """版本确认延期请求（对齐 FR-008）。"""

    version: int = Field(..., ge=1, description="版本号")


class MetricCompareRequest(BaseModel):
    """指标对比请求（对齐 FR-029）。"""

    metric_codes: list[str] = Field(
        ..., min_length=2, max_length=2, description="待对比的两个指标编码"
    )


class MetricCompareMatrixRequest(BaseModel):
    """多指标矩阵对比请求（2~6 个，去重保序）。

    超限数量（>6）不在 schema 层用 max_length 拦截，改由 service 层
    ``compare_matrix`` 显式校验并抛中文 ``ValidationError``——否则 Pydantic
    返回无 message 的 422（前端只见「HTTP 422」，体验差）。
    """

    metric_codes: list[str] = Field(
        ..., min_length=2, description="待对比的指标编码（2~6 个，超限由 service 校验）"
    )


class MetricAutoSuggestRequest(BaseModel):
    """指标注册自动推断请求（对齐 FR-010/FR-011）。

    显式 schema 校验（此前为裸 dict）：``sql`` 类型化为 ``str | None``，防非字符串
    payload（数字/对象）进入 SQL 解析器触发 ``AttributeError`` → 500；FastAPI 对
    类型不匹配返回 422，而非服务端异常。
    """

    domain_code: str = Field(default="", max_length=64, description="所属域编码")
    source_table: str | None = Field(None, max_length=256, description="源表名")
    measure_column: str | None = Field(None, max_length=128, description="度量列")
    period: str | None = Field(None, max_length=16, description="统计周期")
    sql: str | None = Field(None, max_length=16384, description="指标定义 SQL")
    use_llm: bool = Field(
        False,
        description=(
            "是否启用 LLM 全字段推断（默认走程序规则推断；LLM 产出经枚举白名单校验，"
            "非法回退规则，不阻断）"
        ),
    )


class MetricSuggestDomainRequest(BaseModel):
    """业务域建议请求（FR-010 域建议增强）。

    输入 SQL 或源表（至少一个）→ 反向定位业务域：
    采集目录（DBCatalog→DataSource.domain）+ 挂载实体（MetricMount.domain）；
    均未命中（表未被采集，如大段 SQL 引用平台外实体）→ LLM 兜底推断。

    显式 schema 校验（对齐 auto-suggest）：``sql``/``source_table`` 类型化，
    防非字符串 payload 进入解析器触发 ``AttributeError`` → 500。
    """

    sql: str | None = Field(None, max_length=16384, description="指标定义 SQL（大段 SQL 场景）")
    source_table: str | None = Field(None, max_length=256, description="源表名（库.表 或 表名）")

    @model_validator(mode="after")
    def _at_least_one_source(self) -> MetricSuggestDomainRequest:
        """sql 与 source_table 至少提供一个，否则 422。"""
        if not (self.sql or self.source_table):
            raise ValueError("sql 与 source_table 至少提供一个")
        return self


class MetricRefineDefinitionRequest(BaseModel):
    """指标三层口径 LLM 增强请求（业务口径 / 伪代码口径 / 数仓SQL口径）。

    供编辑弹窗 / 注册向导的「AI 丰富增强 / AI 生成 / AI 优化」按钮调用：
    LLM 只生成文本回填，不落库、不创建版本（落库仍走既有编辑/提交流程），
    因此本端点不校验指标权限——凡具备写角色的用户即可对口径做 LLM 辅助。
    """

    field: Literal["business", "pseudo", "dw"] = Field(
        ..., description="目标口径层：business=业务口径 / pseudo=伪代码口径 / dw=数仓SQL口径"
    )
    action: Literal["enrich", "generate", "optimize"] = Field(
        ..., description="动作：enrich=丰富增强现有 / generate=从上下文生成 / optimize=优化现有"
    )
    current: str = Field(
        default="", max_length=16384, description="当前口径内容（generate 时可为空）"
    )
    metric_code: str | None = Field(None, max_length=64, description="指标编码（供 LLM 参考）")
    metric_name: str | None = Field(None, max_length=128, description="指标中文名（供 LLM 参考）")
    domain: str | None = Field(None, max_length=64, description="所属业务域（供 LLM 参考）")
    sql: str | None = Field(None, max_length=16384, description="技术口径 SQL（源业务库口径）")
    expression: str | None = Field(None, max_length=4096, description="计算表达式（MEL）")
    business_definition: str | None = Field(None, max_length=16384, description="现有业务口径")
    pseudo_definition: str | None = Field(None, max_length=16384, description="现有伪代码口径")
    dw_definition: str | None = Field(None, max_length=16384, description="现有数仓SQL口径")


class MetricBatchRegisterRequest(BaseModel):
    """批量注册请求（对齐 FR-030）。"""

    source_table: str = Field(..., description="源宽表名")
    measure_columns: list[str] = Field(..., min_length=1, description="度量列列表")
    dimension_mapping: dict[str, str] | None = Field(None, description="维度列映射")
    # 兼容保留参数（True/False 行为一致）：批量注册固定走 auto_fill 规则引擎自动推断
    # （与单条注册一致）；LLM 命名预填（auto_fill 的 llm_name 入参）为后续增强，当前未接线。
    llm_prefill: bool = Field(True, description="是否启用自动推断预填（规则引擎，兼容保留）")
    domain: str = Field(..., max_length=64, description="所属域")


class MetricSqlTablesRequest(BaseModel):
    """SQL 源表解析请求（注册向导：数仓SQL口径失焦自动回填依赖表）。

    轻量只读解析：输入数仓 SQL/建模口径 → 用 sqlglot 提取 FROM/JOIN/子查询/CTE 的
    源表清单（``source_tables``），供前端自动回填「依赖表（上游）」选项框。不落库、
    不触发 LLM，纯函数容错——非 SQL/解析失败返回空列表不报错。``sql`` 类型化防非
    字符串 payload 进解析器触发 ``AttributeError`` → 500。
    """

    sql: str = Field(..., max_length=65536, description="数仓 SQL/建模口径文本")


class MetricSqlParseRequest(BaseModel):
    """SQL 批量解析请求（FR-010 批量注册增强，场景A/B）。

    粘贴大段 SQL（含多指标）→ 按模式切分 + 逐语句推断候选清单（只读 + LLM 域建议，
    不落库）。``sql`` 类型化防非字符串 payload 进解析器触发 ``AttributeError`` → 500。
    """

    sql: str = Field(..., max_length=65536, description="大段 SQL 脚本（含多个指标）")
    split_mode: Literal["semicolon", "statement", "custom"] = Field(
        "statement",
        description=(
            "切分模式：semicolon（引号感知 ;）/ statement（CTE/INSERT 语义）/ "
            "custom（用户自定义规则）"
        ),
    )
    custom_rules: dict[str, Any] | None = Field(
        None, description="自定义切分规则：{delimiters: [正则], start_markers: [正则]}"
    )
    domain_code: str | None = Field(None, max_length=64, description="显式指定域（缺省自动建议）")
    synthesize_composite: bool = Field(
        False, description="单语句多度量时是否合成复合指标候选（依赖组内原子）"
    )
    use_llm: bool = Field(
        False,
        description=(
            "显式 LLM 模式：对规则解析出的候选做一次 LLM 批量补全（封闭选择："
            "中文名润色/周期校正/非度量过滤）+ 规范收敛（白名单/列名回映/稳定排序/"
            "置信度）；整段 SQL 只花 1 次调用，LLM 不可用自动回退规则候选，绝不阻断"
        ),
    )


class SqlBatchCreateCandidate(BaseModel):
    """SQL 批量创建候选（前端勾选微调后提交，创建端纯写不重跑 LLM）。"""

    key: str = Field(
        ...,
        max_length=128,
        description="稳定标识：{语句序号}:{度量列}（原子）/{语句序号}:composite（复合）",
    )
    metric_code: str = Field(..., max_length=64, description="指标编码（4 段式）")
    name: str = Field(..., max_length=128, description="指标名称")
    # 指标类型：解析器规则产出 atomic/composite；用户可在前端将原子候选在线改为
    # derived（原子指标 + 业务限定 + 时间周期，依赖可选）/ composite（多指标运算，
    # 强制依赖 + 计算表达式），创建端按派生指标（OneData 挂载层）处理。
    type: Literal["atomic", "derived", "composite"] = Field(..., description="指标类型")
    source_table: str | None = Field(None, max_length=256, description="源表名")
    measure_column: str | None = Field(None, max_length=128, description="度量列（复合为空）")
    aggregation: Literal[
        "SUM", "AVG", "COUNT", "COUNT_DISTINCT", "LAST_VALUE", "FIRST_VALUE",
        "MAX", "MIN", "MEDIAN", "PERCENTILE",
    ] | None = Field(None, description="聚合方式（复合为空）")
    unit: str | None = Field(None, max_length=32, description="单位")
    period: str | None = Field(None, max_length=16, description="统计周期")
    granularity: str | None = Field(
        None,
        max_length=64,
        description=(
            "粒度（由推断产出，批量创建落库；OneData 粒度下沉前旧式物理来源承载）"
        ),
    )
    measure_id: int | None = Field(None, ge=1, description="关联逻辑度量（原子可选）")
    # 口径溯源（生产就绪审查 P2）：候选所属语句的整句原始 SQL（原文切片），批量
    # 创建时透传落 Metric.raw_sql——候选口径仅表达式，原文可据此反查（batch_id 溯源）
    raw_sql: str | None = Field(None, description="候选所属语句原始 SQL（可空）")
    definition_json: dict[str, Any] = Field(
        ...,
        description=(
            "口径定义（原子：expression 模式；派生：expression + 可选 dependencies + "
            "挂载层；复合：expression + dependencies 必填）"
        ),
    )
    dependencies: list[str] | None = Field(None, description="依赖指标编码（复合必填、派生可选）")
    mount: MetricMountInput | None = Field(
        None, description="挂载实体（可选，创建时落 metric_mount）"
    )
    # P0-2：口径三方责任预设（复合指标批量创建补齐——详情页 OwnerChain 责任链完整；
    # 原子候选责任方通常随创建人/域默认，复合候选允许携带独立责任方）
    product_owner_id: int | None = Field(None, ge=1, description="产品需求方用户 ID")
    tech_owner_id: int | None = Field(None, ge=1, description="技术方用户 ID（口径 ETL 实现人）")
    dw_developer_id: int | None = Field(None, ge=1, description="数仓开发用户 ID（血缘维护人）")
    product_owner_name: str | None = Field(None, max_length=128, description="产品需求方名称兜底")
    tech_owner_name: str | None = Field(None, max_length=128, description="技术方名称兜底")
    dw_developer_name: str | None = Field(None, max_length=128, description="数仓开发名称兜底")


class MetricSqlBatchRegisterRequest(BaseModel):
    """从 SQL 解析候选批量注册请求（对齐 batch-register 模式，savepoint 逐条隔离）。"""

    domain: str = Field(..., max_length=64, description="所属域（批量域门禁与本域校验）")
    candidates: list[SqlBatchCreateCandidate] = Field(
        ..., min_length=1, description="候选清单（原子先行，复合在后）"
    )


class MetricTemplateCreateRequest(BaseModel):
    """模板创建请求（对齐 FR-041：Schema 校验替代裸 dict）。

    预设字段枚举与 ``MetricCreateRequest`` 对齐（方案A）：此前模板枚举字段为宽松
    ``str``，实例化时透传给 Literal 严格校验会契约漂移——模板作者预设非法值
    （如 ``serving_mode="REALTIME"``）在实例化/编辑时 422。收严为同源枚举，从源头
    拦截非法值；存量非法值由迁移 0085 清洗。
    """

    code: str | None = Field(
        None,
        max_length=64,
        pattern=r"^tpl_[a-z][a-z0-9_]*$",
        description="模板编码（缺省由系统自动生成 tpl_{domain}_{name} slug）",
    )
    name: str = Field(..., max_length=128, description="模板名称")
    domain: str = Field(..., max_length=64, description="适用域")
    description: str | None = Field(None, description="模板说明")
    defaults_json: dict[str, Any] = Field(default_factory=dict, description="预填字段默认值")
    required_fields: list[str] | None = Field(None, description="必填字段列表")
    type: Literal["atomic", "derived", "composite"] | None = Field(None, description="指标类型预设")
    granularity: str | None = Field(None, max_length=64, description="粒度预设")
    unit: str | None = Field(None, max_length=32, description="单位预设")
    aggregation: Literal[
        "SUM", "AVG", "COUNT", "COUNT_DISTINCT", "LAST_VALUE", "FIRST_VALUE",
        "MAX", "MIN", "MEDIAN", "PERCENTILE",
    ] | None = Field(None, description="聚合方式预设")
    time_semantics: Literal["PERIOD", "YTD", "TTM", "AVG", "MOM", "YOY"] | None = Field(
        None, description="时间语义预设"
    )
    freshness: Literal["REALTIME", "T0", "T1", "HOURLY"] | None = Field(
        None, description="数据新鲜度预设"
    )
    dw_layer: Literal["ODS", "DWD", "DWS", "ADS", "DM"] | None = Field(
        None, description="数仓分层预设"
    )
    serving_mode: Literal["BATCH_ONLY", "REALTIME_ONLY", "BATCH_REALTIME_DUAL"] | None = Field(
        None, description="服务模式预设"
    )
    additivity: Literal["ADDITIVE", "SEMI_ADDITIVE", "NON_ADDITIVE"] | None = Field(
        None, description="可加性预设"
    )
    metric_tier: Literal["T1", "T2", "T3"] | None = Field(None, description="指标分级预设")
    # OneData 原子层：原子指标预设逻辑度量（度量格式/单位/小数位/源头系统实例化时继承）
    measure_id: int | None = Field(None, ge=1, description="逻辑度量预设（原子指标）")
    # OneData 挂载层：派生指标预设挂载实体（源表/列/粒度/周期/域，实例化时落 metric_mount）
    mount: MetricMountInput | None = Field(None, description="挂载实体预设（派生指标）")
    # 口径三方责任预设（实例化时作为指标默认责任方，均可空；与 metric 字段命名一致）
    product_owner_id: int | None = Field(None, ge=1, description="产品需求方用户 ID 预设")
    tech_owner_id: int | None = Field(None, ge=1, description="技术方用户 ID 预设")
    dw_developer_id: int | None = Field(None, ge=1, description="数仓开发用户 ID 预设")
    product_owner_name: str | None = Field(None, max_length=128, description="产品需求方名称预设")
    tech_owner_name: str | None = Field(None, max_length=128, description="技术方名称预设")
    dw_developer_name: str | None = Field(None, max_length=128, description="数仓开发名称预设")
    owner_id: int | None = Field(None, ge=1, description="责任人（Owner）ID")


class MetricTemplateUpdateRequest(BaseModel):
    """模板更新请求（P2-13：模板编辑闭环，全字段可选 PATCH 语义）。

    对齐 ``MetricTemplateCreateRequest`` 的字段集，但全部可选——只更新传入字段。
    复用同一套 ``pattern``/``max_length``/枚举约束，保证编辑后的模板仍符合创建时的
    校验强度（避免「创建严格、编辑松懈」的契约漂移）。
    """

    name: str | None = Field(None, max_length=128, description="模板名称")
    domain: str | None = Field(None, max_length=64, description="适用域")
    description: str | None = Field(None, description="模板说明")
    defaults_json: dict[str, Any] | None = Field(None, description="预填字段默认值")
    required_fields: list[str] | None = Field(None, description="必填字段列表")
    type: Literal["atomic", "derived", "composite"] | None = Field(None, description="指标类型预设")
    granularity: str | None = Field(None, max_length=64, description="粒度预设")
    unit: str | None = Field(None, max_length=32, description="单位预设")
    aggregation: Literal[
        "SUM", "AVG", "COUNT", "COUNT_DISTINCT", "LAST_VALUE", "FIRST_VALUE",
        "MAX", "MIN", "MEDIAN", "PERCENTILE",
    ] | None = Field(None, description="聚合方式预设")
    time_semantics: Literal["PERIOD", "YTD", "TTM", "AVG", "MOM", "YOY"] | None = Field(
        None, description="时间语义预设"
    )
    freshness: Literal["REALTIME", "T0", "T1", "HOURLY"] | None = Field(
        None, description="数据新鲜度预设"
    )
    dw_layer: Literal["ODS", "DWD", "DWS", "ADS", "DM"] | None = Field(
        None, description="数仓分层预设"
    )
    serving_mode: Literal["BATCH_ONLY", "REALTIME_ONLY", "BATCH_REALTIME_DUAL"] | None = Field(
        None, description="服务模式预设"
    )
    additivity: Literal["ADDITIVE", "SEMI_ADDITIVE", "NON_ADDITIVE"] | None = Field(
        None, description="可加性预设"
    )
    metric_tier: Literal["T1", "T2", "T3"] | None = Field(None, description="指标分级预设")
    # OneData 原子层：逻辑度量预设（传 null 清除）
    measure_id: int | None = Field(None, ge=1, description="逻辑度量预设（原子指标）")
    # OneData 挂载层：挂载实体预设（传 null 清除）
    mount: MetricMountInput | None = Field(None, description="挂载实体预设（派生指标）")
    # 口径三方责任预设（传 null 清除）
    product_owner_id: int | None = Field(None, ge=1, description="产品需求方用户 ID 预设")
    tech_owner_id: int | None = Field(None, ge=1, description="技术方用户 ID 预设")
    dw_developer_id: int | None = Field(None, ge=1, description="数仓开发用户 ID 预设")
    product_owner_name: str | None = Field(None, max_length=128, description="产品需求方名称预设")
    tech_owner_name: str | None = Field(None, max_length=128, description="技术方名称预设")
    dw_developer_name: str | None = Field(None, max_length=128, description="数仓开发名称预设")
    owner_id: int | None = Field(None, ge=1, description="责任人（Owner）ID（传 null 解除）")
    is_active: bool | None = Field(None, description="是否启用（模板上/下架）")


class MetricListParams(BaseModel):
    """指标列表查询参数。"""

    domain: str | None = None
    status: str | None = None
    metric_tier: str | None = None
    # 指标类型过滤（OneData 派生指标「绑定基础原子指标」下拉）：服务端按类型精确
    # 过滤，前端无需在 ≤100 条页内再 filter(type)——原子指标即便超过单页容量也能靠
    # 关键词 + 类型条件收敛，不会因混合类型占满页而漏掉原子指标。
    metric_type: Literal["atomic", "derived", "composite"] | None = Field(
        None, description="指标类型过滤（atomic 原子 / derived 派生 / composite 复合）"
    )
    keyword: str | None = None
    # 责任人过滤（资产地图 Owner 视图下钻）
    owner_id: int | None = Field(None, ge=1, description="责任人（Owner）ID 过滤")
    # 审批人过滤（审批工作台「我审过的」视图）
    approver_id: int | None = Field(None, ge=1, description="审批人（Approver）ID 过滤")
    # 评审历史过滤（「我审过的」完整视图）：命中 审批通过(approver_id)
    # 或 驳回(reject_reviewer_id) 任一（审批工作台评审历史不丢驳回记录）
    reviewed_by: int | None = Field(
        None, ge=1, description="评审历史过滤（通过或驳回过该指标的用户 ID）"
    )
    # PII 过滤（热力指标视角下钻：PII 格子 / 非 PII 格子）
    pii_flag: bool | None = Field(None, description="仅 PII / 仅非 PII 指标")
    # 已删除过滤（回收站视角）：true 时仅查软删（deleted_at 置位）的草稿指标，供恢复
    deleted: bool = Field(False, description="仅查已软删（回收站）指标")
    # 批次过滤（生产就绪审查 P2）：按批量注册批次 ID 精确匹配——审核/列表页可按
    # "这一批"收敛（SQL 批量/宽表批量创建的指标带 batch_id，此前无筛选入口）
    batch_id: str | None = Field(
        None, max_length=64, description="批量注册批次 ID 精确过滤（可空）"
    )
    # 生命周期快筛（TD §13）：按创建/更新时间区间过滤（ISO 日期或 datetime）
    created_after: datetime | None = Field(None, description="创建时间 ≥ 该值（生命周期快筛）")
    created_before: datetime | None = Field(None, description="创建时间 ≤ 该值")
    updated_after: datetime | None = Field(None, description="更新时间 ≥ 该值")
    updated_before: datetime | None = Field(None, description="更新时间 ≤ 该值")
    sort_by: Literal["updated_at", "created_at", "version", "metric_code", "name"] = "updated_at"
    sort_order: Literal["asc", "desc"] = "desc"
    page: int = Field(1, ge=1, le=1000)
    page_size: int = Field(20, ge=1, le=100)


# ---- 响应 Schema ----


class MetricResponse(BaseModel):
    """指标详情响应。"""

    id: int
    metric_code: str
    name: str
    domain: str
    type: str
    # OneData：粒度已下沉挂载实体（metric_mount），存量/派生回填可空
    granularity: str | None = None
    # OneData 原子层：关联逻辑度量 ID（原子必填；派生/复合继承可空）
    measure_id: int | None = None
    # 逻辑度量展示信息（best-effort 填充，度量软删/查询失败时缺省）：详情页
    # 「逻辑度量」栏展示名称+编码，原子指标关联的权威继承源可读可追溯
    measure_code: str | None = None
    measure_name: str | None = None
    unit: str
    currency: str | None
    aggregation: str
    time_semantics: str
    freshness: str
    sla: str | None
    dw_layer: str
    metric_tier: str
    serving_mode: str
    additivity: str
    non_additive_dimensions: list[str] | None
    definition_json: dict[str, Any]
    version: int
    row_version: int
    status: str
    owner_id: int
    backup_owner_id: int | None
    # 口径三方责任（PRD 4.5 补充）：产品需求方/技术方/数仓开发（user.id，均可空）
    product_owner_id: int | None = None
    tech_owner_id: int | None = None
    dw_developer_id: int | None = None
    # 外部人员名称兜底（责任方非平台用户时直接填名称，id 为空）：展示优先级 id>name
    product_owner_name: str | None = None
    tech_owner_name: str | None = None
    dw_developer_name: str | None = None
    # 关联业务术语（P2-11：术语绑定，度量口径归属术语治理）
    term_id: int | None = None
    # 多变体挂载列表（2026-08-27 放开一指标一挂载）：详情接口回填全部挂载行；
    # 列表接口未回填时为 None（详情/编辑页按需调用 detail 端点）。
    mounts: list[MetricMountResponse] | None = None
    # 治理追溯：审批人 / 提交人，DB 模型已有，响应透出供目录页显示
    approver_id: int | None = None
    submitted_by: int | None = None
    # 评审指派（TD §13）：提交评审时指定的评审用户/域评审组，审批页据此校验与展示
    reviewer_id: int | None = None
    reviewer_type: str | None = None
    reviewer_domain: str | None = None
    # 驳回可追溯（FR-005 闭环）：DRAFT 详情页展示"上次驳回原因"引导提交人修改后重提
    reject_reason: str | None = None
    reject_reviewer_id: int | None = None
    # ORM metric.rejected_at 为 datetime（service reject 落库 datetime.now），
    # 声明 datetime 使 model_validate 通过，序列化输出 ISO 字符串（前端 formatCnTime 兼容）
    rejected_at: datetime | None = None
    # 审核通过时间（审批工作台「我审过的」展示处理时间）：metric 表无独立列，
    # 由 list 接口从当前生效版本 metric_version.published_at 批量填充（无迁移）；
    # 驳回场景用 rejected_at，通过场景用 approved_at
    approved_at: datetime | None = None
    # 指标业务描述（TD §12.1 治理补充，独立于口径/版本，资产地图抽屉展示/编辑）
    description: str | None = None
    description_source: str | None = None
    description_updated_by: int | None = None
    description_updated_at: datetime | None = None
    pii_flag: bool
    compliance_reviewed: bool
    effective_version: int | None
    consumption_guide: dict[str, Any] | None
    successor_code: str | None
    deprecated_at: datetime | None
    # P0-C：批量注册批次 ID（可空）——列表/详情/审核页展示批次可回溯整批
    batch_id: str | None = None
    # P1-1（第六轮）：原始口径 SQL（可空，批量创建透传落库）——此前 MetricResponse
    # 未声明该字段，API 永不返回、前端零展示（"写而不读"）；声明后详情页可反查
    # batch_id → 整句口径原文，候选仅表达式时也能核对全貌
    raw_sql: str | None = None
    # DB 列为 date（models/metric.py），序列化输出 ISO "YYYY-MM-DD"，前端 string 兼容
    sunset_until: date | None
    emergency_publish: bool = False
    emergency_reason: str | None = None
    emergency_reviewed_at: datetime | None = None
    gray_tenant_ids: list[int] | None = None
    pending_conflict: bool = False
    pending_conflict_detail: dict[str, Any] | None = None
    # 仲裁裁决标记（TD §12.4）：canonical（权威口径）/ coexist（已裁定共存），详情页据此展示
    arbitration_mark: dict[str, Any] | None = None
    # 版本待确认：PUBLISHED+破坏性变更后，消费方需在 14 天内确认
    pending_version: bool = False
    # 健康度信号：列表接口经 metric_health_score 批量回填（无记录时为 None，目录页显示"未评分"）
    health_score: int | None = None
    health_level: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class MetricListResponse(BaseModel):
    """指标列表响应。"""

    total: int
    page: int
    page_size: int
    items: list[MetricResponse]


class MetricVersionResponse(BaseModel):
    """指标版本响应。"""

    id: int
    metric_id: int
    version: int
    change_type: str
    definition_json: dict[str, Any]
    diff_json: dict[str, Any] | None
    status: str
    change_reason: str
    created_by: int
    published_at: datetime | None
    created_at: datetime
    # PENDING_CONFIRMATION 版本的确认截止时间（14 天 + 延期），前端展示超时语义
    pending_deadline: datetime | None = None
    # 多消费方确认进度：已确认 X / 共 N 个消费方（仅待确认版本填充，其余为 None）
    confirmed_count: int | None = None
    consumer_count: int | None = None

    model_config = {"from_attributes": True}


class ErrorResponse(BaseModel):
    """统一错误响应。"""

    code: str
    message: str
    trace_id: str
    detail: dict[str, Any] | None = None


class MetricHealthResponse(BaseModel):
    """指标健康度响应（五维评分）。"""

    metric_id: int
    score: int
    level: str
    completeness_score: int
    activity_score: int
    quality_score: int
    owner_response_score: int
    lineage_coverage_score: int
    missing_dimensions: list[str] | None
    calculated_at: datetime

    model_config = {"from_attributes": True}


class MetricSourceDroppedRequest(BaseModel):
    """数据源 DROP → 批量标记下游指标 DSD（采集侧触发，TD §12.3 / PRD R3-04④）。

    source_ids 为采集检测到已 DROP/不可达的数据源 ID 集合。
    """

    source_ids: list[str] = Field(..., min_length=1, max_length=200)


class MetricConsumptionGuideUpdateRequest(BaseModel):
    """更新指标消费指南请求（独立于指标状态机的轻量文档维护，对齐描述编辑）。

    三组列表均须为字符串数组（≤20 项/每项 ≤200 字符，结构校验复用
    _validate_guide_payload）；row_version 为可选乐观锁（与 update_metric 一致，
    防并发覆盖他人编辑）。
    """

    recommended_usage: list[str] = Field(
        default_factory=list, description="推荐使用场景"
    )
    cautions: list[str] = Field(default_factory=list, description="注意事项")
    related_metrics: list[str] = Field(default_factory=list, description="关联指标编码")
    row_version: int | None = Field(None, ge=1, description="乐观锁（指标当前 row_version）")

    @field_validator("recommended_usage", "cautions", "related_metrics")
    @classmethod
    def validate_lists(cls, v: list[str], info: ValidationInfo) -> list[str]:
        return _validate_guide_list(v, info.field_name)
