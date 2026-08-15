"""解析 DP 调度任务元数据（dp元数据.csv），提取 SQL 血缘并构建血缘视图数据。

输入：
- dp元数据.csv          —— 任务-节点-具体定义sql/脚本 的任务元数据
- dp_table_desc.txt     —— 字段结构描述（本脚本不依赖，仅作参考）

输出（写至 lineage_out/ 目录）：
- nodes.csv             —— 表节点（库.表）+ 业务属性（层次/域/责任人/调度周期/加工方式/源库类型）
- edges.csv             —— 血缘边（source -> target）+ 来源任务/节点/SQL/责任人等
- lineage.json          —— 前端血缘视图用（nodes + edges + meta）
- stats.json            —— 解析统计（成功/失败/未解析 SQL 样例）

血缘规则：
- INSERT [OVERWRITE] INTO table ... SELECT ... FROM a JOIN b   -> a,b -> table
- CREATE TABLE ... AS SELECT ... FROM a JOIN b                 -> a,b -> table
- SELECT ... FROM a JOIN b （无目标表时跳过，若该任务有 out_table 则 a,b -> out_table）
- CREATE TABLE / DROP TABLE / ALTER 等无 SELECT 的 DDL         -> 只登记输出表，无血缘边
- 同一任务多个节点按 nodeNo 串联，节点间不产生边（边统一为 输入表 -> 任务输出表）
"""

from __future__ import annotations

import csv
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp

CSV_PATH = Path("/System/Volumes/Data/data/GitCode/Unisense/dp元数据.csv")
OUT_DIR = Path("/System/Volumes/Data/data/GitCode/Unisense/lineage_out")

# 常见系统库/临时库，不作为血缘节点
_SKIP_DBS = {
    "information_schema", "performance_schema", "mysql", "sys",
    "wedw_dw", "wedw_ods",  # wedw_dw/wedw_ods 是项目内库，保留？——先保留，仅跳过系统库
}
_SKIP_DBS = {"information_schema", "performance_schema", "mysql", "sys", "default", "temp"}

# nodeType 含义（来自样例推断）
NODE_TYPE_LABEL = {2: "SQL", 3: "脚本", 4: "SQL", 6: "节点", 8: "节点", 10: "节点"}

# 调度周期
CYCLE_LABEL = {0: "日", 1: "月", 2: "周"}
FREQUENCE_LABEL = {0: "日", 1: "月", 2: "周", 3: "小时", 4: "分钟"}
CREATE_TYPE_LABEL = {0: "全量", 1: "增量", 2: "追加", 3: "拉链"}
DB_TYPE_LABEL = {0: "hive", 1: "pg", 3: "mysql", 4: "oracle"}


def clean_table(t: str) -> str | None:
    """规范化表名：去反引号/引号/空白，转小写，去前缀库名中的特殊符号。"""
    if not t:
        return None
    t = t.strip().strip("`'\"").strip()
    if not t or t.lower() in ("null", "none", "-99"):
        return None
    # 拆库.表，规范化库名
    parts = t.split(".")
    parts = [p.strip().strip("`'\"").strip() for p in parts if p.strip()]
    if not parts:
        return None
    if len(parts) == 1:
        db = None
        table = parts[0]
    else:
        db = ".".join(parts[:-1]).lower()
        table = parts[-1]
    table = table.lower()
    # 表名中的变量占位符（如 ${date}）视为无效
    if not table or " " in table or "(" in table or ")" in table:
        return None
    return f"{db}.{table}" if db else table


def extract_tables_from_expression(expr: exp.Expression) -> list[str]:
    """从 sqlglot 表达式提取所有引用的表（FROM/JOIN）。"""
    tables: list[str] = []
    for node in expr.find_all(exp.Table):
        name = node.name
        db = node.db or ""
        full = f"{db}.{name}" if db else name
        t = clean_table(full)
        if t:
            tables.append(t)
    return tables


_VAR_PAT = re.compile(r"\$\{[^}]*\}")


def preprocess_sql(sql: str) -> str:
    """SQL 预处理：剥离注释、替换 Hive 变量占位符、规范化空白/换行。

    DP 平台的大量 SQL 被块注释包裹（`/* insert into ... */ /* select ... */`），
    或含 `${tmp_tabname}` 等 Hive 变量，导致 sqlglot 解析失败。此处先行清理。
    """
    # 剥离块注释（/* ... */）与行注释（-- ... 到行尾）
    s = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    s = re.sub(r"(?m)--[^\r\n]*", " ", s)
    # Hive 变量替换为合法标识符占位
    s = _VAR_PAT.sub("var_placeholder", s)
    # 统一换行、合并空白
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return s.strip()


def split_sql_script(sql: str) -> list[str]:
    """将一段 SQL 脚本（可能含多条语句）拆分为单条 SQL 列表。"""
    cleaned = preprocess_sql(sql)
    if not cleaned:
        return []
    sqls: list[str] = []
    try:
        parsed = sqlglot.parse(cleaned, read="hive")
    except Exception:
        # 解析失败时按分号粗拆（忽略引号内分号）
        return [s.strip() for s in re.split(r";(?=(?:[^']*'[^']*')*[^']*$)", cleaned) if s.strip()]
    for p in parsed:
        if p is not None:
            sqls.append(p.sql(dialect="hive"))
    return [s for s in sqls if s.strip()]


_TARGET_PAT = re.compile(
    r"(?:insert\s+(?:overwrite|into)\s+(?:table\s+)?|create\s+(?:table\s+(?:if\s+not\s+exists\s+)?)?)"
    r"([`'\"]?[\w.]+[`'\"]?)",
    re.I,
)
_FROM_PAT = re.compile(r"\bfrom\s+([`'\"]?[\w.]+[`'\"]?)", re.I)
_JOIN_PAT = re.compile(r"\bjoin\s+([`'\"]?[\w.]+[`'\"]?)", re.I)


def regex_fallback_analyze(sql: str) -> dict[str, Any]:
    """正则降级分析：从无法用 sqlglot 解析的 SQL 文本中提取表引用。"""
    res: dict[str, Any] = {"kind": "regex", "target": None, "sources": []}
    m = _TARGET_PAT.search(sql)
    if m:
        res["target"] = clean_table(m.group(1))
    for pat in (_FROM_PAT, _JOIN_PAT):
        for mm in pat.finditer(sql):
            t = clean_table(mm.group(1))
            if t:
                res["sources"].append(t)
    res["sources"] = list(dict.fromkeys(res["sources"]))
    return res


def analyze_sql(sql: str) -> dict[str, Any]:
    """分析单条 SQL，返回 {kind, target, sources}。"""
    res: dict[str, Any] = {"kind": "unknown", "target": None, "sources": []}
    try:
        expr = sqlglot.parse_one(sql, read="hive")
    except Exception:
        res["kind"] = "parse_error"
        return res

    kind = expr.key.upper()
    sources: list[str] = []

    if isinstance(expr, exp.Insert):
        target = extract_tables_from_expression(expr.this)  # INTO table
        target = target[0] if target else None
        # SELECT 部分
        sel = expr.expression if isinstance(expr.expression, exp.Select) else None
        if sel is not None:
            sources = extract_tables_from_expression(sel)
        # 子查询/union 中的表
        sources += extract_tables_from_expression(expr.expression)
        sources = list(dict.fromkeys(sources))
        res = {"kind": "insert", "target": target, "sources": sources}
    elif kind in ("CREATE",):
        is_ctas = False
        create = expr
        target = extract_tables_from_expression(create.this)
        target = target[0] if target else None
        if create.expression is not None:
            is_ctas = True
            sources = extract_tables_from_expression(create.expression)
            sources = list(dict.fromkeys(sources))
        res = {"kind": "ctas" if is_ctas else "ddl", "target": target, "sources": sources}
    elif isinstance(expr, exp.Select):
        sources = extract_tables_from_expression(expr)
        sources = list(dict.fromkeys(sources))
        res = {"kind": "select", "target": None, "sources": sources}
    elif kind in ("DROP", "ALTER", "TRUNCATE", "COMMENT", "SET", "USE", "CREATE"):
        res = {"kind": kind.lower(), "target": None, "sources": []}
    else:
        res = {"kind": kind.lower(), "target": None, "sources": []}
    return res


# 数仓层推断（按库名前缀，作为 level_id 的补充直观维度）
def infer_layer(db: str) -> str:
    d = (db or "").lower()
    if not d:
        return "未知"
    if d.startswith("wedw_ods") or d.startswith("wedw_opendata") or d.startswith("ods"):
        return "ODS"
    if d.startswith("wedw_dwd") or d.startswith("dwd"):
        return "DWD"
    if d.startswith("wedw_dws") or d.startswith("dws"):
        return "DWS"
    if d.startswith("wedw_ads") or d.startswith("ads"):
        return "ADS"
    if d.startswith("wedw_dw") or d == "wedw":
        return "DW"
    if "tmp" in d or "temp" in d:
        return "临时"
    if d.startswith("sync") or d.startswith("src") or "source" in d:
        return "源端"
    return "其他"


def build_lineage_json(nodes: dict, edges: list, stats: Counter) -> dict[str, Any]:
    """构建前端血缘视图用的 lineage.json（nodes + edges + meta）。"""
    node_list: list[dict[str, Any]] = []
    for name, nd in sorted(nodes.items()):
        db = name.split(".")[0] if "." in name else ""
        table = name.split(".")[-1] if "." in name else name
        node_list.append({
            "id": name,
            "table": table,
            "db": db,
            "layer": infer_layer(db),
            "level_id": nd["level_id"] if nd["level_id"] != "-99" else "",
            "domain_id": nd["domain_id"] if nd["domain_id"] != "-99" else "",
            "director": nd["director"] if nd["director"] != "-99" else "",
            "cycle": CYCLE_LABEL.get(int(nd["cycle"]), nd["cycle"]) if str(nd["cycle"]).isdigit() else nd["cycle"],
            "frequence": FREQUENCE_LABEL.get(int(nd["frequence"]), nd["frequence"]) if str(nd["frequence"]).isdigit() else nd["frequence"],
            "create_type": CREATE_TYPE_LABEL.get(int(nd["create_type"]), nd["create_type"]) if str(nd["create_type"]).isdigit() else nd["create_type"],
            "db_type": DB_TYPE_LABEL.get(int(nd["db_type"]), nd["db_type"]) if str(nd["db_type"]).isdigit() else nd["db_type"],
            "is_task_output": bool(nd["is_task_output"]),
            "node_count": nd["node_count"],
        })
    edge_list: list[dict[str, Any]] = []
    for e in edges:
        edge_list.append({
            "source": e["source"],
            "target": e["target"],
            "task_id": e["task_id"],
            "task_name": e["task_name"],
            "sql_kind": e["sql_kind"],
            "sql_snippet": e["sql_snippet"],
            "director": e["director"],
        })
    return {
        "meta": {
            "source_file": "dp元数据.csv",
            "parsed_at": str(stats.get("parsed_at", "")),
            "total_tasks": stats["tasks"],
            "total_tables": len(nodes),
            "total_edges": len(edges),
        },
        "nodes": node_list,
        "edges": edge_list,
    }


def main() -> None:
    csv.field_size_limit(sys.maxsize)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    stats = Counter()
    parse_failures: list[str] = []
    nodes: dict[str, dict[str, Any]] = {}      # 表名 -> 节点
    edges: list[dict[str, Any]] = []           # 血缘边
    unparsed_sql_samples: list[str] = []

    with open(CSV_PATH, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            task_id = row.get("task_id", "")
            task_name = row.get("name", "")
            out_table_raw = row.get("out_table", "")
            director = row.get("director", "")
            level_id = row.get("level_id", "")
            domain_id = row.get("domain_id", "")
            topic_id = row.get("topic_id", "")
            cycle = row.get("cycle", "")
            frequence = row.get("frequence", "")
            create_type = row.get("create_type", "")
            db_type = row.get("db_type", "")
            target_db_type = row.get("target_db_type", "")
            task_state = row.get("task_state", "")
            check_status = row.get("check_status", "")

            task_out = clean_table(out_table_raw)

            try:
                td = json.loads(row["task_definition"])
            except Exception:
                stats["task_definition_json_fail"] += 1
                continue
            if not isinstance(td, list):
                continue
            stats["tasks"] += 1

            # 任务级：登记输出表节点
            if task_out:
                _ensure_node(nodes, task_out, director, level_id, domain_id,
                             cycle, frequence, create_type, db_type, target_db_type,
                             is_task_output=True)

            node_sources: list[str] = []
            for n in td:
                nt = n.get("nodeType")
                cmd = (n.get("command") or "").strip()
                if not cmd:
                    continue
                if nt in (3, 6, 8, 10):
                    # 脚本/非 SQL 节点：尝试从中提取表引用（python 脚本里的 sql 片段）
                    # 暂不深度解析，仅标记
                    stats["non_sql_nodes"] += 1
                    continue
                stats["sql_nodes"] += 1
                for one_sql in split_sql_script(cmd):
                    if not one_sql:
                        continue
                    try:
                        ana = analyze_sql(one_sql)
                    except Exception:
                        ana = {"kind": "parse_error", "target": None, "sources": []}
                    if ana["kind"] in ("parse_error", "command", "select"):
                        # 降级：正则提取表引用；select 无目标时用任务 out_table 兜底
                        rana = regex_fallback_analyze(one_sql)
                        if ana["kind"] == "select":
                            # 纯 SELECT：目标表取任务输出表，源用已解析的表
                            rana = {"kind": "select", "target": task_out, "sources": ana["sources"]}
                        elif ana["kind"] == "command" and not rana["sources"]:
                            stats["sql_command_no_table"] += 1
                        ana = rana
                    if ana["kind"] == "parse_error":
                        stats["sql_parse_fail"] += 1
                        if len(unparsed_sql_samples) < 10:
                            unparsed_sql_samples.append(one_sql[:300])
                        continue
                    stats[f"sql_{ana['kind']}"] += 1
                    if ana["target"]:
                        _ensure_node(nodes, ana["target"], director, level_id, domain_id,
                                     cycle, frequence, create_type, db_type, target_db_type,
                                     is_task_output=(ana["target"] == task_out))
                    for src in ana["sources"]:
                        _ensure_node(nodes, src, director, level_id, domain_id,
                                     cycle, frequence, create_type, db_type, target_db_type,
                                     is_task_output=False)
                        node_sources.append(src)

                    # 生成血缘边
                    target = ana["target"] or task_out
                    if target and ana["sources"]:
                        for src in dict.fromkeys(ana["sources"]):
                            if src == target:
                                continue
                            edges.append({
                                "source": src,
                                "target": target,
                                "task_id": task_id,
                                "task_name": task_name,
                                "node_type": nt,
                                "sql_kind": ana["kind"],
                                "sql_snippet": _snippet(one_sql),
                                "director": director,
                                "level_id": level_id,
                                "domain_id": domain_id,
                                "frequence": frequence,
                                "create_type": create_type,
                                "db_type": db_type,
                            })
            # 若任务有 out_table 但节点只解析出 select 无目标，把 select 源连向任务输出表
            if task_out and node_sources and not any(
                e["target"] == task_out for e in edges if e.get("task_id") == task_id
            ):
                pass

    # 血缘边去重：同一任务内 source->target 去重（保留第一个即 SQL 最完整）
    seen_edges: set[tuple[str, str, str]] = set()
    dedup_edges: list[dict[str, Any]] = []
    for e in edges:
        key = (e["task_id"], e["source"], e["target"])
        if key in seen_edges:
            continue
        seen_edges.add(key)
        dedup_edges.append(e)
    edges = dedup_edges

    # 写 nodes.csv
    with open(OUT_DIR / "nodes.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["table_name", "db_name", "is_task_output", "director", "level_id",
                    "domain_id", "topic_id", "cycle", "frequence", "create_type",
                    "db_type", "target_db_type", "node_count"])
        for name, nd in sorted(nodes.items()):
            db = name.split(".")[0] if "." in name else ""
            w.writerow([name, db, nd["is_task_output"], nd["director"], nd["level_id"],
                        nd["domain_id"], nd["topic_id"], nd["cycle"], nd["frequence"],
                        nd["create_type"], nd["db_type"], nd["target_db_type"], nd["node_count"]])

    # 写 edges.csv
    with open(OUT_DIR / "edges.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["source", "target", "task_id", "task_name", "node_type", "sql_kind",
                    "sql_snippet", "director", "level_id", "domain_id"])
        for e in edges:
            w.writerow([e["source"], e["target"], e["task_id"], e["task_name"],
                        e["node_type"], e["sql_kind"], e["sql_snippet"], e["director"],
                        e["level_id"], e["domain_id"]])

    # 写 stats.json
    stats_json = {
        "total_tasks": stats["tasks"],
        "sql_nodes": stats["sql_nodes"],
        "non_sql_nodes": stats["non_sql_nodes"],
        "sql_by_kind": {k: v for k, v in stats.items() if k.startswith("sql_")},
        "sql_parse_fail": stats["sql_parse_fail"],
        "task_definition_json_fail": stats["task_definition_json_fail"],
        "unique_tables": len(nodes),
        "lineage_edges": len(edges),
        "unparsed_sql_samples": unparsed_sql_samples,
    }
    with open(OUT_DIR / "stats.json", "w", encoding="utf-8") as f:
        json.dump(stats_json, f, ensure_ascii=False, indent=2)

    # 写 lineage.json（前端血缘视图用）
    lineage = build_lineage_json(nodes, edges, stats)
    with open(OUT_DIR / "lineage.json", "w", encoding="utf-8") as f:
        json.dump(lineage, f, ensure_ascii=False, indent=2)

    # 汇总输出
    print("=" * 60)
    print("解析完成。统计：")
    for k, v in stats_json.items():
        print(f"  {k}: {v}")
    print(f"\n节点文件: {OUT_DIR}/nodes.csv ({len(nodes)} 表)")
    print(f"边文件:   {OUT_DIR}/edges.csv ({len(edges)} 条血缘边)")
    if parse_failures:
        print("\n任务 definition 解析失败样例:")
        for s in parse_failures[:3]:
            print("  ", s)


def _ensure_node(nodes: dict, name: str, director: str, level_id: str, domain_id: str,
                 cycle: str, frequence: str, create_type: str, db_type: str,
                 target_db_type: str, is_task_output: bool = False) -> None:
    """登记表节点（合并属性）。"""
    if name not in nodes:
        nodes[name] = {
            "is_task_output": is_task_output, "director": director, "level_id": level_id,
            "domain_id": domain_id, "topic_id": "", "cycle": cycle, "frequence": frequence,
            "create_type": create_type, "db_type": db_type, "target_db_type": target_db_type,
            "node_count": 0,
        }
    nodes[name]["node_count"] += 1
    if is_task_output:
        nodes[name]["is_task_output"] = True
    # 属性补全（有值才覆盖）
    for key, val in (("director", director), ("level_id", level_id), ("domain_id", domain_id),
                     ("cycle", cycle), ("frequence", frequence), ("create_type", create_type),
                     ("db_type", db_type), ("target_db_type", target_db_type)):
        if val and val != "-99" and nodes[name][key] in ("", "-99", None):
            nodes[name][key] = val


def _snippet(sql: str, maxlen: int = 160) -> str:
    s = re.sub(r"\s+", " ", sql).strip()
    return s[:maxlen] + ("…" if len(s) > maxlen else "")


if __name__ == "__main__":
    main()
