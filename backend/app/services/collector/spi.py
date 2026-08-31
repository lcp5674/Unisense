"""采集器 SPI（对齐 TD §12.1「SPI（多数据源适配器）」）。

设计要点：
- ``BaseCollector`` 抽象采集行为；``collect()`` 返回 ``CollectResult``
  （含成功 specs + 失败 failed_specs + source_id），实现单表跳过容错。
- ``build_collector`` 委托 ``CollectorRegistry`` 构建，支持插件式注册。
- 外部依赖（源库）失败统一转化为 ``ExternalDependencyError``（503 可重试），
  **不**静默吞没为 200。
- SQL 一律参数化（``Connector.query(sql, params)``），避免注入。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.services.collector.classifier import SensitivityClassifier
from app.services.collector.mojibake import contains_mojibake

logger = logging.getLogger(__name__)

#: 单次采样行数上限（与 API 端点 ``sample_rows`` 的 ``le`` 约束对齐）。
#: 采样是「取代表值」而非导出数据：行数越多对源库的扫描压力与 ``schema_json``
#: 存储膨胀越大，故统一封顶，避免配额路径绕过端点校验。
MAX_SAMPLE_ROWS = 200

#: 记录视图单元格上限（行数 × 列数）。宽表（数百列）按行截断，
#: 防止 ``schema_json.sample_rows`` 膨胀到拖慢列表接口。
MAX_SAMPLE_CELLS = 4000

#: 单个样本值最大保留长度（字符）。BLOB/TEXT/JSON/Array/Map 等大字段的
#: ``str()`` 可能达 MB 级，写入 ``sample_rows``/``sample`` 会撑大 ``schema_json``
#: （DB JSON 列 + API 响应 + 前端渲染），故统一截断——PII 识别只需格式特征
#: （手机/身份证/邮箱/银行卡均在 20-30 字符内），超长截断不影响识别精度。
MAX_SAMPLE_VALUE_LEN = 200

#: 采样熔断阈值：连续整表采样失败达到该次数后，本采集会话不再尝试采样。
#: 典型场景——Doris 对 ODBC 外表（``ODBC_SCAN_NODE`` 不支持）采样查询必败，
#: 若不熔断，数百张表 × 数十列会在「行对齐失败 → 逐列补采」中空转数千次
#: 失败查询，采集进度停在 start、看起来像卡死。
SAMPLE_FAIL_LIMIT = 3


@dataclass
class CatalogSpec:
    """采集到的实体元数据规格。"""

    entity_name: str
    entity_type: str
    schema_json: dict[str, Any]
    etl_sql: str | None = None
    description: str | None = None


@dataclass
class FailedSpec:
    """采集失败的实体记录（单表跳过容错）。"""

    entity_name: str
    error: str


@dataclass
class CollectResult:
    """采集结果（含成功与失败记录）。"""

    specs: list[CatalogSpec] = field(default_factory=list)
    failed_specs: list[FailedSpec] = field(default_factory=list)
    source_id: str = ""
    # 表级过滤统计（治理白/黑名单跳过；方案 B：采集结果/记录展示被过滤的表）
    filtered_count: int = 0
    filtered_names: list[str] = field(default_factory=list)


@dataclass
class ProbeResult:
    """连接探活结果（测试连接 / 实时健康检查）。"""

    ok: bool
    latency_ms: int
    error: str | None = None
    detail: dict[str, Any] | None = None


class Connector(Protocol):
    """源库查询协议（便于测试注入假连接器）。"""

    async def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """执行参数化查询，返回行字典列表。"""
        ...

    async def dispose(self) -> None:
        """释放连接（源库引擎等）。"""
        ...


class BaseCollector(ABC):
    """采集器基类。"""

    def __init__(self, classifier: SensitivityClassifier | None = None) -> None:
        self._classifier = classifier or SensitivityClassifier()
        self._include_patterns: list[str] | None = None
        self._exclude_patterns: list[str] | None = None
        self._databases: list[str] | None = None
        self._sampling_max_rows = 0
        #: 采样进度回调（service 层注入，供连接器内采样等阶段发进度事件）
        self._progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        #: 采样熔断状态：连续整表采样失败计数 + 是否已熔断本会话采样
        self._sampling_fail_streak = 0
        self._sampling_disabled = False
        #: 编码乱码标记（源端 U+FFFD 替换符/GBK 二次转码残留）：采样样本值命中
        #: 的字段名集合（采样时明文累计，供连接器组装 schema_json 时标记）。
        self._mojibake_fields: set[str] = set()
        #: 元数据注释命中乱码的字段名集合（连接器对 DESCRIBE 注释等检测后登记）。
        self._mojibake_comment_fields: set[str] = set()

    @abstractmethod
    async def collect(self, source: Any) -> CollectResult:
        """采集数据源，返回采集结果（含成功 specs 与失败 failed_specs）。"""
        ...

    async def collect_entity(self, source: Any, entity_name: str) -> CatalogSpec | None:
        """采集单个实体（单表元数据刷新，生产运维场景）。

        仅刷新目标表/实体，不触发全源扫描。返回该实体的最新 CatalogSpec；
        连接器不支持单实体采集（如 Hive 启动开销大）时返回 None，
        调用方应回退到全量采集后仅取目标实体。

        Args:
            source: 数据源 ORM 对象。
            entity_name: 目录实体名（形如 ``schema.table`` 或 ``table``）。

        Returns:
            最新 CatalogSpec，不支持时返回 None。
        """
        return None

    def set_incremental_context(self, mode: str, watermark_ts: Any | None = None) -> None:
        """注入增量采集上下文（P0-6：由 service 层在 collect 前调用）。

        ``mode`` 为 "INCREMENTAL" 且 ``watermark_ts`` 非空时，支持增量的连接器
        只采集水位之后发生变更的实体；默认实现保持全量（不支持增量降级为全量）。

        Args:
            mode: 采集模式（FULL/INCREMENTAL）。
            watermark_ts: 上次采集水位时间戳。
        """
        self._incremental_mode = mode
        self._incremental_watermark = watermark_ts

    def set_table_filter(
        self,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
    ) -> None:
        """注入表级采集过滤白黑名单（治理：include/exclude patterns）。

        由 service 层在 collect 前从 ``DataSource.include_patterns`` /
        ``DataSource.exclude_patterns`` 读取并注入；连接器按 fnmatch 风格过滤
        扫描到的实体名。默认实现仅保存（供子类 collect 时读取）。

        Args:
            include_patterns: 包含白名单（任一匹配即保留），空/None 表示不过滤。
            exclude_patterns: 排除黑名单（任一匹配即丢弃）。
        """
        self._include_patterns = include_patterns
        self._exclude_patterns = exclude_patterns

    def set_databases(self, databases: list[str] | None = None) -> None:
        """注入目标数据库列表（多库采集：逐库扫描指定库，None=采集全部非系统库）。

        由 service 层在 collect 前从 ``DataSource.databases`` 读取并注入；
        连接器在 ``collect`` 时优先采用该列表，否则枚举全部非系统库。
        连接库 ``connection_config.database`` 为纯连接凭据，不参与采集范围。
        """
        self._databases = databases

    def set_sampling(self, max_rows: int = 0) -> None:
        """注入样本采样配置（PII 精度增强：name+sample 双验证）。

        ``max_rows`` 为采样行数上限（0/负值=不采样）。由 service 层在
        collect 前从 ``DataSource.quota.sample_rows`` 读取并注入；连接器在
        采集到字段后按能力执行采样，样本打码写入 ``schema_json``。

        上限在此**统一收敛**为 ``MAX_SAMPLE_ROWS``：配额路径不经过 API 端点的
        ``le`` 校验，若只在上层拦截，保存 1000 行配额仍会打到源库。
        每次注入同时**重置采样熔断状态**——新采集会话重新尝试采样。
        """
        self._sampling_max_rows = max(0, min(int(max_rows or 0), MAX_SAMPLE_ROWS))
        self._sampling_fail_streak = 0
        self._sampling_disabled = False

    def set_progress_cb(
        self, cb: Callable[[dict[str, Any]], Awaitable[None]] | None = None
    ) -> None:
        """注入采集进度回调（service 层在 collect 前设置）。

        连接器在 ``collect`` 内扫描/采样等阶段调用 ``_notify_progress``
        发进度事件（如 ``phase=sampling + index/total + entity_name``），
        使前端在注册阶段之前也能看到进度在推进（而非停在 0%）。
        """
        self._progress_cb = cb

    async def _notify_progress(self, event: dict[str, Any]) -> None:
        """向采集进度回调发送事件（best-effort：回调失败不影响采集主流程）。"""
        cb = self._progress_cb
        if cb is None:
            return
        try:
            await cb(event)
        except Exception as exc:  # noqa: BLE001 - 进度是辅助能力
            logger.warning("collector_progress_cb_failed: %s", exc)

    async def sample_columns(
        self, entity_name: str, schema_json: dict[str, Any]
    ) -> dict[str, Any]:
        """对实体字段执行样本采样（可选能力，PII 识别精度增强）。

        默认不采样、原样返回 schema；支持采样的连接器覆盖本方法——对每列执行
        ``SELECT col ... LIMIT n`` 取代表值，经 ``_mask_sample`` 打码后写入
        ``columns[].sample``。连接器内部应在 ``collect`` 组装 schema 时调用，
        以复用已建立的源库连接（避免每表额外握手）。

        Args:
            entity_name: 实体（库.表）名。
            schema_json: 含 ``columns`` 列表的 schema 字典。

        Returns:
            写入 ``sample`` 后的 schema 字典（不支持采样时原样返回）。
        """
        return schema_json

    def _mask_sample(self, sample: str) -> str:
        """对样本值打码（委托 classifier：手机/身份证/邮箱/银行卡掩码）。"""
        return self._classifier.mask_sample(sample)

    def _note_mojibake_field(self, name: str, *, comment: bool = False) -> None:
        """登记字段乱码标记（连接器对样本明文/元数据注释检测命中后调用）。

        Args:
            name: 字段名。
            comment: True 表示命中在元数据注释（DESCRIBE comment），
                False 表示命中在采样样本值。
        """
        target = self._mojibake_comment_fields if comment else self._mojibake_fields
        if name:
            target.add(name)

    def _take_mojibake(self) -> dict[str, list[str]]:
        """取走并清空本连接器已登记的乱码标记（供连接器组装 schema_json 时写入）。

        Returns:
            ``{"sample_fields": [...], "comment_fields": [...]}``（均为空时返回 {}）。
        """
        sample = sorted(self._mojibake_fields)
        comment = sorted(self._mojibake_comment_fields)
        self._mojibake_fields.clear()
        self._mojibake_comment_fields.clear()
        result: dict[str, list[str]] = {}
        if sample:
            result["sample_fields"] = sample
        if comment:
            result["comment_fields"] = comment
        return result

    def _truncate_value(self, text: str) -> str:
        """截断超长样本值（保留前 ``MAX_SAMPLE_VALUE_LEN`` 字符 + ``…`` 标记）。

        大字段（BLOB/TEXT/JSON/Array/Map）的 ``str()`` 可达 MB 级，必须截断
        再入 ``sample_rows``/``sample``，否则 ``schema_json`` 膨胀拖慢列表接口。
        """
        if len(text) > MAX_SAMPLE_VALUE_LEN:
            return text[:MAX_SAMPLE_VALUE_LEN] + "…"
        return text

    def _sample_rule_id(self, sample: str) -> str | None:
        """判定样本明文命中的敏感类别（rule_id），供采样时随打码值落库。

        掩码会丢失格式特征（``138****1234`` 无法反推是手机还是身份证），
        故类别必须在打码前对明文判定并单独存储为 ``columns[].sample_rule``。
        """
        return self._classifier.classify_sample(sample)

    def _apply_samples(self, col: dict[str, Any], values: list[str]) -> None:
        """把采样值写入字段定义（打码 + 类别），各连接器共用。

        保留最多 ``_sampling_max_rows`` 条（按打码值去重）写入 ``columns[].sample``
        为列表；类别（``sample_rule``）记录首个明文命中的敏感类别——掩码不可逆，
        事后无法补判类别，故类别必须在打码前对明文判定。
        """
        seen: set[str] = set()
        masked: list[str] = []
        rule_id: str | None = None
        for v in values:
            s = self._truncate_value(str(v).strip())
            if not s or s == "NULL":
                continue
            # 源端编码乱码检测：打码前对明文判定（掩码会掩盖替换符形态，
            # 且 U+FFFD 属格式特征，打码后无法反推），命中即登记字段标记。
            if contains_mojibake(s):
                self._note_mojibake_field(col.get("name", ""))
            m = self._mask_sample(s)
            if m in seen:
                continue
            seen.add(m)
            masked.append(m)
            if rule_id is None:
                rule_id = self._sample_rule_id(s)
            if len(masked) >= self._sampling_max_rows:
                break
        if masked:
            col["sample"] = masked
            if rule_id:
                col["sample_rule"] = rule_id

    async def _sample_rows_aligned(
        self,
        entity_name: str,
        safe: list[tuple[dict[str, Any], str]],
        build_select_sql: Callable[[list[str], int], str],
        build_one_sql: Callable[[str, int], str],
        run_query: Callable[[str, list[str]], Awaitable[list[dict[str, Any]]]],
    ) -> list[dict[str, str]]:
        """**行对齐**采样：一次查询全部列，返回「一行 = 源库一条真实记录」的视图。

        与旧的逐列/分批采样不同，本方法刻意**不加** ``WHERE col IS NOT NULL``
        过滤：那类过滤会让各列各自取到不同行的非空值，拼出来的「第 i 条样本」
        在源库并不存在（``phone.sample[0]`` 来自第 1 行、``email.sample[0]``
        来自第 3 行）。前端若按行展示就会呈现**拼凑出来的假记录**——用户会
        误以为「该患者的手机号与邮箱是配套的」。故行视图必须整行读取。

        产出两项，语义分离：
        - 返回值 ``sample_rows``：行对齐的记录视图，NULL 保留为空串占位
          （不丢弃，否则列会错位），每格经 ``_mask_sample`` 打码。
        - ``columns[].sample``（经 ``_apply_samples`` 派生）：**列式**样本，
          取各列在这批行内的全部非空值——PII 识别要的是「每列尽可能多的
          非空形态」，与展示所需的「真实记录」目标不同，故二者并存。

        Args:
            entity_name: 实体名，仅用于日志定位。
            safe: ``(字段字典, 合法列名)`` 列表，列名须已通过标识符白名单校验。
            build_select_sql: 构造全列查询 SQL 的方言回调 ``(列名列表, 行数)``。
            build_one_sql: 构造单列补采 SQL 的方言回调 ``(列名, 行数)``。
            run_query: 执行 SQL 的方言回调 ``(SQL, 本次查询列顺序)``，返回行字典。
                传入列顺序是因为部分驱动（如 pyhive）返回**元组**而非字典，
                无此参数便无法把每格还原到正确的字段上（列错位）。

        Returns:
            打码后的行视图列表；整表查询失败时返回空列表（已自动降级逐列采样）。
        """
        names = [name for _, name in safe]
        n = self._effective_sample_rows(len(names))
        if not n or not names:
            return []
        # 采样熔断：本会话已确认源端不支持采样（如 Doris ODBC 外表），
        # 后续表直接跳过，不再发失败查询空转。
        if self._sampling_disabled:
            return []
        failed = False
        try:
            rows = await run_query(build_select_sql(names, n), names)
        except Exception as exc:  # noqa: BLE001 - 采样失败不拖垮采集，仅记录
            # 整表查询失败（如实测 Doris 某列的协议兼容问题）→ 降级逐列补采，
            # 隔离不可查的问题列，避免「一列出问题导致整表零样本」。
            logger.warning(
                "行对齐采样失败，降级逐列采样 entity=%s cols=%d error=%s",
                entity_name,
                len(names),
                exc,
            )
            rows = []
            failed = True
        # 防御：驱动/测试 mock 未严格服从 LIMIT 时按上限截断，保证行视图行数受控
        rows = rows[:n]
        sample_rows: list[dict[str, str]] = []
        per_column: dict[str, list[str]] = {name: [] for name in names}
        for raw in rows:
            row: dict[str, str] = {}
            for name in names:
                value = raw.get(name)
                text = "" if value is None else str(value)
                # NULL 保留空串占位：丢弃会让该行后续列整体左移（列错位）
                if text in ("", "NULL"):
                    row[name] = ""
                    continue
                # 大字段截断后再打码/落库（防 schema_json 膨胀）；per_column 存
                # 截断明文——类别判定只看前 200 字符内的格式特征，截断不损失。
                truncated = self._truncate_value(text)
                if contains_mojibake(truncated):
                    self._note_mojibake_field(name)
                row[name] = self._mask_sample(truncated)
                per_column[name].append(truncated)
            sample_rows.append(row)
        for col, name in safe:
            if per_column[name]:
                self._apply_samples(col, per_column[name])
        # 补采：① 整表查询失败 → 先探测单列，同因失败则跳过整表补采；② 有行但
        # 稀疏列全 NULL → 逐列补采（保 PII 召回）。空表（rows 为空且未失败）无需补采。
        if failed:
            if not await self._probe_single_column(
                entity_name, safe, build_one_sql, run_query, n
            ):
                # 探测列同样失败 → 源端协议不支持该表（同因），逐列补采只会
                # 重复失败查询（数百列 × 超时），跳过整表并累计熔断计数。
                self._sampling_fail_streak += 1
                if self._sampling_fail_streak >= SAMPLE_FAIL_LIMIT:
                    self._sampling_disabled = True
                    logger.warning(
                        "采样熔断：连续 %d 张表整表采样失败，本会话不再采样",
                        self._sampling_fail_streak,
                    )
                return sample_rows
            self._sampling_fail_streak = 0
        if failed or rows:
            await self._fill_empty_samples(entity_name, safe, build_one_sql, run_query)
        return sample_rows

    async def _probe_single_column(
        self,
        entity_name: str,
        safe: list[tuple[dict[str, Any], str]],
        build_one_sql: Callable[[str, int], str],
        run_query: Callable[[str, list[str]], Awaitable[list[dict[str, Any]]]],
        n: int,
    ) -> bool:
        """整表查询失败后探测第 1 列：判断失败是否为「整表不可查」而非个别列。

        若第 1 列也失败，说明源端协议/表级限制导致该表整体不可采样
        （如实测 Doris 的 ``ODBC_SCAN_NODE``），逐列补采必然全败——返回 False
        由调用方跳过整表补采并计入熔断。若第 1 列成功，说明仅部分列异常，
        返回 True 让调用方继续逐列补采（隔离问题列，健康列仍可采样）。
        """
        if not safe:
            return True
        _, first_name = safe[0]
        try:
            await run_query(build_one_sql(first_name, n), [first_name])
            return True
        except Exception as exc:  # noqa: BLE001 - 探测失败即视为整表不可采样
            logger.warning(
                "采样单列探测失败（整表跳过补采） entity=%s column=%s error=%s",
                entity_name,
                first_name,
                exc,
            )
            return False

    def _effective_sample_rows(self, column_count: int) -> int:
        """按行数上限与单元格上限取实际采样行数（宽表自动降行数）。"""
        n = min(self._sampling_max_rows, MAX_SAMPLE_ROWS)
        if n <= 0 or column_count <= 0:
            return 0
        return max(1, min(n, MAX_SAMPLE_CELLS // column_count))

    async def _fill_empty_samples(
        self,
        entity_name: str,
        safe: list[tuple[dict[str, Any], str]],
        build_one_sql: Callable[[str, int], str],
        run_query: Callable[[str, list[str]], Awaitable[list[dict[str, Any]]]],
    ) -> None:
        """对行视图内全 NULL 的列做单列补采（仅补列式 ``sample``，不改行视图）。

        行对齐查询不加 ``WHERE`` 过滤，前 n 行内稀疏列可能全为 NULL；此时该列
        PII 识别将失去样本依据。补采单列非空值可保住识别召回率，且**不写入**
        ``sample_rows``——补来的值不属于那 n 条记录，写进去就会重新引入假记录。
        """
        n = self._effective_sample_rows(len(safe))
        if not n:
            return
        for col, name in safe:
            if col.get("sample"):
                continue
            try:
                rows = await run_query(build_one_sql(name, n), [name])
            except Exception as exc:  # noqa: BLE001 - 单列失败仅跳过该列
                logger.warning(
                    "单列补采失败（跳过该列） entity=%s column=%s error=%s",
                    entity_name,
                    name,
                    exc,
                )
                continue
            values = [
                str(r[name])
                for r in rows
                if r.get(name) is not None and str(r[name]) not in ("", "NULL")
            ]
            if values:
                self._apply_samples(col, values)

    async def list_databases(self) -> list[str]:
        """枚举该实例下可采集的非系统数据库（创建数据源时选择目标库）。

        连接器不支持枚举（如 Kafka）时返回空列表，前端可回退为手填。
        """
        return []

    async def list_tables(self, databases: list[str] | None = None) -> dict[str, list[str]]:
        """枚举指定库下的表（按库分组，创建数据源时级联选表）。

        连接器不支持枚举表（如 Kafka）时返回空字典，前端隐藏表级选择区。
        """
        return {}

    async def probe(self) -> ProbeResult:
        """轻量连接探活（SELECT 1 或等价最小查询），供「测试连接 / 健康检查」使用。

        默认未实现；各连接器按自身协议覆盖。失败时返回 ``ok=False`` 而非抛出异常。
        """
        raise NotImplementedError(f"{type(self).__name__} 未实现 probe")

    async def dispose(self) -> None:
        """释放采集器持有的外部连接（如源库引擎）。默认无操作，子类按需实现。"""
        return None


def build_collector(collector_type: str, encrypted_config: str) -> BaseCollector:
    """按类型构建采集器（委托 CollectorRegistry）。

    已落库数据源的采集/探活路径：放行私有网段（生产库就在内网），
    但仍拒绝回环/链路本地/保留地址（SSRF 纵深防御）。

    Args:
        collector_type: 采集器类型（如 "mysql", "postgres" 等）。
        encrypted_config: DataSource.connection_config 密文。

    Returns:
        采集器实例。

    Raises:
        BusinessError: 类型未注册，或连接目标命中 SSRF 禁区。
    """
    # 惰性导入以确保连接器模块已注册
    from app.services.collector.connectors import registry  # noqa: F401

    return registry.build(collector_type, encrypted_config, allow_private=True)
