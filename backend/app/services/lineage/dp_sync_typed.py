"""dp 调度非 SQL/跨方言节点类型解析器（纯函数，无 DB/LLM 依赖）。

``dispatch_task_step.task_step_type`` 分类解析——形态基于 2026-09 连 dp 元库
（dp_stable.dispatch_task_step.script_info）真实观测：

- **2 DataX 同步**：job JSON，reader/writer 插件可为 mysql/postgres/oracle/doris/
  hdfs…。DB 侧插件从 ``connection[].table``（支持 ``db.table`` 或 jdbcUrl 库拼接）
  与 ``connection[].querySql`` 取表；HDFS 侧插件从 ``parameter.path`` 反推 hive
  库表（仓库路径 ``/warehouse/{db}/{layer}/.../{table}`` → 库名按健康仓惯例拼
  ``{db}_{layer}``，如 ``wedw/dw/xxx`` → ``wedw_dw.xxx``；无法反推的 HDFS 端
  不产节点，宁缺毋滥）。产出 reader 表集 → writer 表集 的笛卡尔表级边。
- **3 Shell 脚本**：真实多为 sleep/注释/命令（无内嵌 SQL）→ no_flow；含
  ``hive/beeline/spark-sql/mysql/impala-shell -e "SQL"`` 时抽取内嵌 SQL 走 SQL
  解析（``-f file`` 引用的外部文件不在库内，忽略）。
- **4 SQL 执行脚本**（直连库 DML，样本为 Airflow 元库维护 update）→ mysql 方言
  走 SQL 解析（update 无源→自然 no_flow，降噪）。
- **5 清表脚本（TRUNCATE）**：语义上无输入（no_flow），走 SQL 解析。
- **6 Oracle SQL/PLSQL**：oracle 方言走 SQL 解析（样本多为 Oracle 系统表
  user_tables/dba_views 查询与 PL/SQL 建空表 → 自然 no_flow）。
- **7 Hive/Spark SQL**：hive 方言走 SQL 解析（历史主路径）。
- **9 上报配置节点**：script 为纯数字配置 ID → 无表级血缘信息 → no_flow
  （诚实标注，不编造表）。
- **15 接口同步配置**：JSON 声明 ``hiveDbName/hiveTableName`` +
  ``mysqlDbName/mysqlTableName`` + ``upType/downType`` → up（上传）为
  ``mysql表 → hive表``、down（下载）为 ``hive表 → mysql表`` 表级边。

统一入口 :func:`parse_dp_step_typed` 按 step_type 分发，返回
``StepParseOutcome``（与 SQL 解析器同构，DpSyncService 无需区分来源）。
"""

from __future__ import annotations

import json
import re
from typing import Any

import sqlglot

from app.services.lineage.dp_sync_parser import StepParseOutcome, parse_dp_step
from app.services.lineage.parser import TableEdge

#: 直连库/异构存储 DataX 插件的 DB 表提取位（connection[].table / querySql）。
#: 仅按插件名是否 hdfs 系区分 HDFS 端——DB 侧插件名形态多，凡不以 hdfs 开头且
#: 带 connection 的都按 DB 侧处理；无 connection 的 DB 插件（oracle 等）走空集。
_HDFS_PLUGIN_RE = re.compile(r"^hdfs", re.IGNORECASE)
#: jdbcUrl 中的默认库提取：``jdbc:mysql://host:3306/db``。
_JDBC_DB_RE = re.compile(r"jdbc:[A-Za-z0-9_]+://[^/]+/([A-Za-z0-9_]+)")
#: Shell 内嵌 SQL 抽取：`<cli> ... -e 'SQL'` / `-e "SQL"`。
_SHELL_E_SQL_RE = re.compile(
    r"(?P<cli>hive|beeline|spark-sql|spark-sql\s+\S+|mysql|impala-shell|clickhouse-client)"
    r"(?P<args>(?:\s+-\w+(?:\s+\S+)?)*?)\s+-e\s*(?P<q>['\"])(?P<sql>.*?)(?P=q)",
    re.IGNORECASE | re.DOTALL,
)
#: 常见 HDFS 文件系统目录名（不作为库/分层参与反推）。
_HDFS_FS_SEGMENTS = {"data", "user", "hive", "warehouse"}


def _clean_table(name: str) -> str:
    """清洗表名：去反引号/首尾空白/多余点，返回 ``db.table`` 或裸表名。"""
    text = (name or "").strip().strip("`\"'")
    text = re.sub(r"`", "", text)
    parts = [p for p in text.split(".") if p]
    if len(parts) > 2:
        parts = parts[:2]  # 只保留 库.表 两段（异常多段截断）
    return ".".join(parts) if parts else ""


def _jdbc_default_db(jdbc_url: Any) -> str | None:
    m = _JDBC_DB_RE.search(str(jdbc_url or ""))
    return m.group(1) if m else None


def _qualify(name: str, default_db: str | None) -> str:
    """裸表名补默认库（connection[].table 为 ``db.table`` 或 ``table``）。"""
    cleaned = _clean_table(name)
    if not cleaned:
        return ""
    if "." in cleaned:
        return cleaned
    if default_db:
        return f"{default_db}.{cleaned}"
    return cleaned


def _sql_read_tables(sql: str, default_db: str | None) -> list[str]:
    """纯 SELECT（DataX reader querySql）引用的表集合（无落点不成边）。"""
    try:
        ast = sqlglot.parse_one(sql)
    except Exception:  # noqa: BLE001 —— querySql 非 SQL 时跳过
        return []
    tables: list[str] = []
    for node in ast.walk():
        if isinstance(node, sqlglot.exp.Table):
            db = node.db or None
            name = node.name or ""
            if name:
                q = _clean_table(f"{db}.{name}" if db else name)
                if not q:
                    continue
                tables.append(q if "." in q or not default_db else f"{default_db}.{q}")
    return list(dict.fromkeys(t for t in tables if t))


def _db_plugin_tables(plugin: dict[str, Any]) -> list[str]:
    """DB 侧 DataX 插件表提取：connection[].table + querySql 解析。"""
    name = plugin.get("name") or ""
    if _HDFS_PLUGIN_RE.match(name):
        return []
    p = plugin.get("parameter") or {}
    tables: list[str] = []
    for conn in p.get("connection") or []:
        default_db = (
            _jdbc_default_db(conn.get("jdbcUrl"))
            or (str(conn.get("selectedDatabase") or "") or None)
        )
        for t in conn.get("table") or []:
            q = _qualify(t, default_db)
            if q:
                tables.append(q)
        qs = conn.get("querySql")
        if qs:
            sql_list = qs if isinstance(qs, list) else [qs]
            for one_sql in sql_list:
                tables.extend(_sql_read_tables(str(one_sql), default_db))
    # 去重保序
    return list(dict.fromkeys(t for t in tables if t))


def _hdfs_plugin_table(plugin: dict[str, Any]) -> str | None:
    """HDFS 侧 DataX 插件表：从 parameter.path 反推 hive 库表。

    仓库路径惯例：``/data/hive/warehouse/wedw/dw/xxx_df``（库拆两级目录
    wedw/dw → wedw_dw）或 ``/user/hive/warehouse/ods.db/tbl``（.db 目录）。
    """
    name = plugin.get("name") or ""
    if not _HDFS_PLUGIN_RE.match(name):
        return None
    p = plugin.get("parameter") or {}
    path = str(p.get("path") or "")
    # 多 path 取第一条
    path = path.split(",")[0].strip()
    parts = [s for s in path.split("/") if s and "=" not in s]
    # 去掉文件/临时段（含扩展名、纯数字分区、_SUCCESS/_temporary）
    while parts and (
        "." in parts[-1]
        or re.fullmatch(r"\d+", parts[-1])
        or parts[-1] in ("_SUCCESS", "_temporary")
    ):
        parts.pop()
    if not parts or "warehouse" not in parts:
        return None
    i = parts.index("warehouse") + 1
    if i >= len(parts):
        return None
    table = parts[-1]
    db = parts[i]
    if db in _HDFS_FS_SEGMENTS or not db:
        return None
    # 两级库目录（warehouse 后紧接 {db}/{layer}/.../{table}，layer != table）
    if i + 1 < len(parts) and parts[i + 1] != table:
        layer = parts[i + 1]
        if layer not in _HDFS_FS_SEGMENTS and "_" not in db:
            db = f"{db}_{layer}"
    elif db.endswith(".db"):
        db = db[:-3]
    return _clean_table(f"{db}.{table}") or None


def _datax_edges(content: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """DataX job.content 全部段合并 reader 表集与 writer 表集。"""
    sources: list[str] = []
    targets: list[str] = []
    for c in content:
        reader = c.get("reader") or {}
        writer = c.get("writer") or {}
        if not reader or not writer:
            continue
        if _HDFS_PLUGIN_RE.match(reader.get("name") or ""):
            src = _hdfs_plugin_table(reader)
            sources.extend([src] if src else [])
        else:
            sources.extend(_db_plugin_tables(reader))
        if _HDFS_PLUGIN_RE.match(writer.get("name") or ""):
            dst = _hdfs_plugin_table(writer)
            targets.extend([dst] if dst else [])
        else:
            targets.extend(_db_plugin_tables(writer))
    return (
        list(dict.fromkeys(s for s in sources if s)),
        list(dict.fromkeys(t for t in targets if t)),
    )


def _parse_datax(script: str) -> StepParseOutcome:
    """DataX job JSON → reader 表集 → writer 表集 表级边。"""
    if not script or not script.strip():
        return StepParseOutcome(status="no_flow", error="空 DataX 配置")
    try:
        job = json.loads(script)
    except Exception as exc:  # noqa: BLE001 —— 非 JSON 脚本视为异常，交人工
        return StepParseOutcome(
            status="failed",
            error=f"DataX job 非合法 JSON：{exc}",
        )
    content = ((job.get("job") or {}).get("content")) or job.get("content") or []
    if not content:
        return StepParseOutcome(status="no_flow", error="DataX job 无 content 段")
    sources, targets = _datax_edges(content)
    if not sources or not targets:
        return StepParseOutcome(
            status="no_flow",
            error=(
                "DataX 未抽到库表级信息（源/目标为文件或无法反推）："
                f"reader={sources} writer={targets}"
            ),
        )
    edges = [
        TableEdge(source=s, target=t)
        for s in sources
        for t in targets
        if s != t
    ]
    if not edges:
        return StepParseOutcome(status="no_flow", error="DataX 源表与目标表相同/无有效边")
    return StepParseOutcome(status="ok", table_edges=edges)


def _extract_shell_sqls(script: str) -> list[str]:
    """抽取 Shell 内嵌 ``<cli> -e 'SQL'`` 的 SQL 文本（去重保序）。"""
    sqls: list[str] = []
    for m in _SHELL_E_SQL_RE.finditer(script or ""):
        sql = (m.group("sql") or "").strip()
        if sql:
            sqls.append(sql)
    return list(dict.fromkeys(sqls))


def _parse_shell(
    script: str,
    dialect: str | None,
    exclude_patterns: list[str] | None,
    rules: dict | None,
    target_table: str | None,
) -> StepParseOutcome:
    """Shell 脚本：含内嵌 SQL → SQL 解析；否则 no_flow（等待/命令节点）。"""
    if not script or not script.strip():
        return StepParseOutcome(status="no_flow", error="空 Shell 脚本")
    sqls = _extract_shell_sqls(script)
    if not sqls:
        return StepParseOutcome(
            status="no_flow",
            error="Shell 脚本无内嵌 SQL（等待/命令/外部脚本引用节点，无库表血缘）",
        )
    return parse_dp_step(
        "\n;\n".join(sqls),
        dialect=dialect,
        exclude_patterns=exclude_patterns,
        rules=rules,
        target_table=target_table,
    )


def _parse_report_config(script: str) -> StepParseOutcome:
    """上报配置节点：script 为纯数字配置 ID，无表级血缘信息。"""
    text = (script or "").strip()
    return StepParseOutcome(
        status="no_flow",
        error=f"上报配置节点（配置 ID={text or '空'}），无表级血缘信息",
    )


def _parse_upload_config(script: str) -> StepParseOutcome:
    """接口同步配置 JSON：mysql 表 ↔ hive 表（按 up/down 方向）。"""
    if not script or not script.strip():
        return StepParseOutcome(status="no_flow", error="空接口同步配置")
    try:
        c = json.loads(script)
    except Exception as exc:  # noqa: BLE001 —— 非 JSON 交人工甄别
        return StepParseOutcome(
            status="failed",
            error=f"接口同步配置非合法 JSON：{exc}",
        )
    hive_t = _clean_table(
        f"{c.get('hiveDbName') or ''}.{c.get('hiveTableName') or ''}"
    )
    mysql_t = _clean_table(
        f"{c.get('mysqlDbName') or ''}.{c.get('mysqlTableName') or ''}"
    )
    if not hive_t or not mysql_t:
        return StepParseOutcome(
            status="no_flow",
            error=f"接口同步配置缺 hive/mysql 表名：{script[:200]}",
        )
    edges: list[TableEdge] = []
    if c.get("upType"):
        # 上传：mysql 业务库 → hive（数据入仓）
        edges.append(TableEdge(source=mysql_t, target=hive_t))
    if c.get("downType"):
        # 下载：hive → mysql 业务库（数据出仓）
        edges.append(TableEdge(source=hive_t, target=mysql_t))
    if not edges:
        return StepParseOutcome(
            status="no_flow",
            error="接口同步配置未声明 up/down 方向，无血缘可推断",
        )
    return StepParseOutcome(status="ok", table_edges=edges)


def parse_dp_step_typed(
    script: str,
    step_type: int | None,
    dialect: str | None = "hive",
    exclude_patterns: list[str] | None = None,
    rules: dict | None = None,
    target_table: str | None = None,
    schema_columns: dict[str, list[str]] | None = None,
) -> StepParseOutcome:
    """按节点类型分发解析 dp step 脚本（统一入口，供 service 调用）。

    Args:
        script: step.script_info 原文。
        step_type: dispatch_task_step.task_step_type；None/未知按 SQL(hive) 处理。
        dialect: SQL 类默认方言；type 4/6 自动切换 mysql/oracle。
        exclude_patterns/rules/target_table: 透传 SQL 解析器（DataX/接口同步等
            非 SQL 形态不使用但保留签名一致性）。
        schema_columns: 可选源表列清单（方案 3 star 展开），仅 SQL 类使用。
    """
    if step_type == 2:
        return _parse_datax(script)
    if step_type == 3:
        return _parse_shell(
            script, dialect, exclude_patterns, rules, target_table
        )
    if step_type == 9:
        return _parse_report_config(script)
    if step_type == 15:
        return _parse_upload_config(script)
    # SQL 类：4(直连库 DML 用 mysql)/6(Oracle 用 oracle)/5/7 及未知 → SQL 解析
    eff_dialect = {4: "mysql", 6: "oracle"}.get(step_type or 7, dialect or "hive")
    return parse_dp_step(
        script,
        dialect=eff_dialect,
        exclude_patterns=exclude_patterns,
        rules=rules,
        target_table=target_table,
        schema_columns=schema_columns,
    )
