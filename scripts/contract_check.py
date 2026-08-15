#!/usr/bin/env python3
"""Unisense 契约 / 文档同步 / 门禁真实性校验。

模式：
  --mode contract       校验 TD §3/§4 与代码实现是否偏离（无代码时仅校验状态自洽）
  --mode doc_sync       校验 PR 描述填了具体 TD 影响章节 + 状态文件与 TD §12 服务清单一致
  --mode gateways_verify 校验 status 的 evidence_path 真实存在非空 + 安全/混沌/可观测测试源含 must_include 关键字

退出码非 0 = CI 失败（对应 CI/.gateways.yml 门禁）。
"""
import argparse
import os
import re
import sys
from pathlib import Path

if sys.version_info[0] < 3:
    # 仓库系统默认 python 可能是 Python 2（实测 macOS 上 /usr/bin/python 为 Py2），
    # 直接报错而非误跑产生假阳性，避免契约门禁被 Python 2 语法错误悄悄跳过。
    raise SystemExit("contract_check.py 需要 Python 3，请用 python3 运行（仓库系统默认 python 为 Python 2）")

ROOT = Path(__file__).resolve().parent.parent
TD = ROOT / "docs" / "technical-design.md"
STATUS = ROOT / "docs" / "module-status.yaml"
GATEWAYS = ROOT / "CI" / ".gateways.yml"

TD_SERVICE_SECTIONS = {
    "collector": "§12.1", "lineage": "§12.2", "semantic": "§12.3",
    "conflict": "§12.4", "governance": "§12.5", "consume": "§12.6",
    "ai": "§12.7", "quality": "§12.8", "notify": "§12.9",
    "observability": "§12.10", "assetmap": "§12.11", "recommend": "§12.12",
    "glossary": "§12.14", "dimension": "§12.15",
}

# =====================================================================
# 契约逐一校验台账：TD 接口路径声明 ↔ 后端真实路由
#
# 旧实现：从 TD 抓取 9 个"路径"（含前端页面路由/外部端点/前缀说明），
# 只要 backend 中命中任 1 个即通过——口径过宽。
# 新实现：只提取 TD 中带 HTTP 方法的接口声明，逐一与后端真实路由校验；
# 每个路径必须有明确去向：直接命中 / 语义映射命中 / 豁免，否则失败。
#
# TD 语义路径 → 后端实现路径（TD 用产品语义名，后端用模块实现名）。
# 说明：路径参数 {param} 与 :param 归一化为同一模式匹配。
TD_SEMANTIC_ALIASES: list[tuple[str, str, str]] = [
    ("/api-clients", "/api/v1/consume/api-clients", "TD 省略 consume 模块前缀"),
    ("/asset-map/*", "/api/v1/assetmap/*", "TD 旧命名 asset-map → 后端 assetmap"),
    ("/asset-map/heatmap", "/api/v1/assetmap/heatmap", "同上 asset-map→assetmap"),
    ("/asset-map/overview", "/api/v1/assetmap/summary", "TD overview 总览 → 后端 summary"),
    ("/asset-map/owner/{uid}", "/api/v1/assetmap/owner-view", "TD 责任人视图 → owner-view"),
    ("/asset-map/{entity}", "/api/v1/assetmap/entities/{entity_id}", "TD 资产实体 → entities/{id}"),
    ("/benchmarks/*", "/api/v1/quality/benchmarks*", "TD 省略 quality 模块前缀"),
    ("/benchmarks/import", "/api/v1/quality/benchmarks/import", "同上"),
    ("/benchmarks/{id}/bind", "/api/v1/quality/benchmarks/{benchmark_id}/bind", "同上"),
    ("/dimension-mappings", "/api/v1/dimensions/mappings", "TD 连字符命名 → dimensions/mappings"),
    ("/feedback", "/api/v1/observability/feedback", "TD 省略 observability 前缀"),
    ("/lineage/edges/{edge_id}/confirm", "/api/v1/lineage/stale/{edge_id}/confirm", "TD 边确认 → 失效边确认"),
    ("/lineage/field/{id}", "/api/v1/lineage/graph", "TD 字段级血缘 → graph（返回含 field_edges）"),
    ("/lineage/impact/{id}", "/api/v1/lineage/impact", "TD impact 详情 → impact"),
    ("/lineage/table/{id}", "/api/v1/lineage/graph", "TD 表级血缘 → graph（返回含 table_edges）"),
    ("/llm/models/{id}/test", "/api/v1/ai/config/test", "TD LLM 模型连通测试 → ai/config/test"),
    ("/metrics/batch-register", "/api/v1/metric-definitions/batch-register", "TD 指标语义名 → metric-definitions 实现"),
    ("/metrics/compare", "/api/v1/metric-definitions/compare", "同上"),
    ("/metrics/dashboard", "/api/v1/semantics/dashboard", "TD 驾驶舱 → semantics/dashboard"),
    ("/metrics/{code}", "/api/v1/metric-definitions/{metric_code}", "TD 指标语义名 → metric-definitions 实现"),
    ("/metrics/{code}/promote", "/api/v1/metric-definitions/{metric_code}/promote", "同上"),
    ("/metrics/{code}/snapshots", "/api/v1/consume/metrics/{code}/snapshots", "TD 指标快照 → consume 快照"),
    ("/nl2sql", "/api/v1/ai/nl2sql", "TD 语义名 → ai/nl2sql"),
    ("/notifications", "/api/v1/notify/notifications", "TD 省略 notify 前缀"),
    ("/quality-events", "/api/v1/quality/events", "TD 连字符命名 → quality/events"),
    ("/quality-rules", "/api/v1/quality/rules", "TD 连字符命名 → quality/rules"),
    ("/query", "/api/v1/consume/query", "TD 语义名 → consume/query"),
    ("/query/dry-run", "/api/v1/consume/query/dry-run", "同上"),
    ("/reconciliation-records", "/api/v1/quality/reconciliation-records", "TD 省略 quality 前缀"),
    ("/reconciliation-records/*", "/api/v1/quality/reconciliation-records*", "同上"),
    ("/reconciliation-records/{id}/confirm", "/api/v1/quality/reconciliation-records/{record_id}/confirm", "同上"),
    ("/sources", "/api/v1/data-sources", "TD 语义名 → data-sources"),
    ("/sources/{id}/bulk-deprecate", "/api/v1/catalogs/bulk-deprecate", "TD 源表批量废弃 → 采集目录批量弃用"),
    ("/versions/{id}/confirm|reject", "/api/v1/consume/versions/{version_id}/confirm|reject", "TD 省略 consume 前缀"),
]

#: TD 声明但非后端 API / TD §12 状态表标注「待做·二期·未落地」的路径（豁免并注明理由）。
TD_EXEMPT_PATHS: dict[str, str] = {
    "/help": "前端帮助页路由，非后端 API",
    "/dags/{dag_id}": "Airflow 外部 API（airflow_host），非 Unisense 后端",
    "/compliance/report": "TD §12.5 合规报表自动化在「待做」列",
    "/governance/domain-comparison": "TD §12.3 域间对比在「待做」列",
    "/governance/benchmark": "TD §12.3 标杆机制在「待做」列",
    "/lineage/external-dependency": "TD §12.2 外部依赖边登记在「待做」列",
    "/drift-scans/{id}/confirm": "TD §12.4 口径漂移巡检完整流程在「待做」列",
    "/policy": "TD §12.5 PDP 策略表化「未落地」（TD 已明确标注）",
    "/query/batch": "TD §12.6 /query/batch 在「待做」列",
    "/query/{id}/cancel": "TD 执行保护设计（在途查询取消），未列入已实现",
    "/snapshots": "TD 结果快照存证标注「L3，P1」规划中",
    "/sources/{id}/import": "TD 通道 B 人工 import（DDL/JSON）未落地",
    "/ops-cost/dashboard": "TD §12.10 成本归因看板在「待做」列",
    "/platform-health": "TD §12.10 平台健康仪表盘在「待做」列",
    "/embed/quickbi": "TD §12.6 QuickBI 嵌入在「待做」列",
    "/embed/quickbi/card": "TD §12.6 QuickBI 嵌入在「待做」列",
}

#: TD 中「接口路径声明」的提取正则：反引号包裹、以 HTTP 方法开头。
_TD_PATH_RE = re.compile(r"`((?:GET|POST|PUT|PATCH|DELETE)\s+/\S+?)`")


def _collect_backend_routes() -> set[str]:
    """从 backend 源码解析全部注册路由（main.py include_router + 各模块 APIRouter 前缀 + 方法路径）。"""
    backend = ROOT / "backend"
    api_dir = backend / "app" / "api"
    main_txt = (backend / "app" / "main.py").read_text(encoding="utf-8")
    # import 映射：include 变量名 -> (模块文件名, 文件内 router 变量名)
    import_map: dict[str, tuple[str, str]] = {}
    for m in re.finditer(r"from app\.api\.(\w+) import ([^#\n]+)", main_txt):
        module = m.group(1)
        for name in m.group(2).split(","):
            name = name.strip()
            if not name:
                continue
            parts = name.split(" as ")
            imported = parts[0].strip()
            alias = parts[1].strip() if len(parts) > 1 else imported
            import_map[alias] = (module, imported)
    inc_prefix: dict[str, str] = {}
    for m in re.finditer(r'include_router\((\w+), prefix=["\']([^"\']*)["\']\)', main_txt):
        inc_prefix[m.group(1)] = m.group(2)
    for m in re.finditer(r"include_router\((\w+)\)", main_txt):
        inc_prefix.setdefault(m.group(1), "")

    routes: set[str] = set()
    for inc_var, prefix in inc_prefix.items():
        if inc_var not in import_map:
            continue
        module, internal_var = import_map[inc_var]
        txt = (api_dir / f"{module}.py").read_text(encoding="utf-8")
        base = prefix
        m = re.search(
            re.escape(internal_var) + r' = APIRouter\((?:prefix=["\']([^"\']*)["\'])?', txt
        )
        if m and m.group(1):
            base += m.group(1)
        for r in re.finditer(
            r"@" + re.escape(internal_var) + r"\.(get|post|put|patch|delete)\([\s]*[\"']([^\"']*)",
            txt,
        ):
            routes.add((base + r.group(2)).rstrip("/") or base)
    return routes


def _path_pattern(path: str) -> str:
    """将路径归一化为匹配模式：参数段（{param}/:param）→ [^/]+，`|` 分隔的备选拆开。"""
    path = path.rstrip("/") or "/"
    p = re.sub(r"\{[^}]+\}|:[A-Za-z_]+", "[^/]+", path)
    return p


def _pattern_eq(path: str, pattern: str) -> bool:
    """两个「模式」是否等价（双向归一化精确匹配）。

    含 `*` 通配的模式仅当两侧字面相同（同为 `*` 通配）时等价；
    具体路径模式（如 `/asset-map/{entity}`）不被 `/asset-map/*` 顶替。
    尾斜杠变体（/api/v1/metrics vs /api/v1/metrics/）视为等价。
    """
    path = path.rstrip("/") or "/"
    pattern = pattern.rstrip("/") or "/"
    if "*" in path or "*" in pattern:
        return path == pattern
    rx = re.compile("^" + _path_pattern(pattern) + "$")
    return rx.match(path) is not None


def _matches_any(path: str, routes: set[str]) -> bool:
    """模式路径是否命中后端路由（支持 /api/v1 前缀补全、通配 `*` 结尾、`|` 备选）。"""
    for part in path.split("|"):
        part = part.strip().rstrip("/") or "/"
        if not part:
            continue
        candidates: list[re.Pattern[str]] = []
        if part.endswith("*"):
            candidates.append(re.compile("^" + _path_pattern(part[:-1])))
            if not part.startswith("/api/v1"):
                candidates.append(re.compile("^" + _path_pattern("/api/v1" + part[:-1])))
        else:
            candidates.append(re.compile("^" + _path_pattern(part) + "$"))
            if not part.startswith("/api/v1"):
                candidates.append(re.compile("^" + _path_pattern("/api/v1" + part) + "$"))
        for rx in candidates:
            for r in routes:
                if rx.match(r):
                    return True
    return False


def _alias_matches(path: str, routes: set[str]) -> tuple[str, str] | None:
    """语义映射匹配：TD 路径与映射表 TD 模式双向等价，且映射目标在后端真实存在。"""
    for td_pat, impl_pat, _note in TD_SEMANTIC_ALIASES:
        td_parts = [x.strip() for x in td_pat.split("|") if x.strip()]
        path_parts = [x.strip() for x in path.split("|") if x.strip()]
        if not td_parts or not path_parts:
            continue
        fwd = all(any(_pattern_eq(pp, tp) for tp in td_parts) for pp in path_parts)
        bwd = all(any(_pattern_eq(tp, pp) for pp in path_parts) for tp in td_parts)
        if fwd and bwd and _matches_any(impl_pat, routes):
            return td_pat, impl_pat
    return None


def check_contract_with_code(td_text: str) -> None:
    backend = ROOT / "backend"
    if not backend.exists():
        print("[contract] backend/ 不存在，跳过代码级契约比对（仅状态自洽已校验）")
        return
    td_paths: set[str] = set()
    for m in _TD_PATH_RE.finditer(td_text):
        raw = m.group(1).strip()
        if "?" in raw:
            raw = raw.split("?", 1)[0]
        _meth, _sep, path = raw.partition(" ")
        td_paths.add(path.strip().rstrip("/") or "/")

    routes = _collect_backend_routes()
    direct: list[str] = []
    aliased: list[tuple[str, str]] = []
    exempted: list[tuple[str, str]] = []
    missing: list[str] = []
    for p in sorted(td_paths):
        if _matches_any(p, routes):
            direct.append(p)
        elif (hit := _alias_matches(p, routes)) is not None:
            aliased.append((p, hit[1]))
        elif p in TD_EXEMPT_PATHS:
            exempted.append((p, TD_EXEMPT_PATHS[p]))
        else:
            missing.append(p)

    print(
        f"[contract] TD 接口路径声明 {len(td_paths)} 个（逐一校验）："
        f"直接命中 {len(direct)} / 语义映射 {len(aliased)} / 豁免 {len(exempted)} / 未命中 {len(missing)}"
    )
    for td_p, impl_p in aliased:
        print(f"  [映射] {td_p} → {impl_p}")
    for p, reason in exempted:
        print(f"  [豁免] {p}（{reason}）")
    for p in missing:
        errors.append(f"[contract] TD 声明的接口路径在后端无实现且未在映射/豁免台账: {p}")

errors: list[str] = []


def load_yaml(path: Path) -> dict:
    try:
        import yaml
    except ImportError:
        raise SystemExit("PyYAML 未安装：pip install pyyaml")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def check_status_self_consistency(status: dict) -> None:
    """状态自洽 + evidence 必须真实存在非空（防假证据）。"""
    valid = {"planned", "dev", "implemented", "verified", "released", "blocked"}
    for name, m in status.get("modules", {}).items():
        st = m.get("status")
        if st not in valid:
            errors.append(f"[status] {name}.status 非法: {st}")
            continue
        ev = m.get("evidence_path")
        if st in ("verified", "released", "implemented"):
            if not ev:
                errors.append(f"[status] {name} 为 {st} 但 evidence_path 为空（禁止空口声明）")
            else:
                p = ROOT / ev
                if not p.exists():
                    errors.append(f"[status] {name} evidence_path 指向不存在文件: {ev}")
                elif p.stat().st_size == 0:
                    errors.append(f"[status] {name} evidence_path 文件为空(0字节): {ev}")
                elif p.stat().st_size < 50:
                    errors.append(f"[status] {name} evidence_path 疑似占位过小(<50字节): {ev}")
        if st == "blocked" and not m.get("blocker", m.get("rollback_reason")):
            errors.append(f"[status] {name} 为 blocked 但无 blocker 原因")
        sec = m.get("td_section", "")
        if sec not in TD_SERVICE_SECTIONS.values() and not sec.startswith("§12"):
            errors.append(f"[status] {name}.td_section 异常: {sec}")


def check_doc_sync(status: dict) -> None:
    td_text = TD.read_text(encoding="utf-8") if TD.exists() else ""
    for name, sec in TD_SERVICE_SECTIONS.items():
        num = sec.lstrip("§")  # "12.5"
        # TD 章节标题形如 "## 12.5" 或含 "§12.5"，两种都算存在
        found = bool(re.search(rf"^###\s+{re.escape(num)}\b", td_text, re.M)) or (sec in td_text)
        if not found:
            errors.append(f"[doc_sync] TD 缺少服务章节 {sec}（{name}）")
        if name not in status.get("modules", {}):
            errors.append(f"[doc_sync] module-status.yaml 缺少服务 {name}（应对应 {sec}）")
    pr_body = os.environ.get("PR_BODY", "")
    if pr_body:
        # 必须含具体章节号（§ + 数字），禁止"无"/空
        if not re.search(r"TD影响章节:\s*§\d", pr_body) and "TD §" not in pr_body:
            errors.append("[doc_sync] PR 描述未填写具体 TD 影响章节（格式：TD影响章节: §12.x，禁止填'无'）")


def check_gateways_verify(status: dict, gateways: dict) -> None:
    """校验 evidence 真实 + 安全/混沌/可观测测试源含 must_include 关键字（防空壳测试）。"""
    backend = ROOT / "backend"
    if not backend.exists():
        print("[gateways_verify] backend/ 不存在，跳过测试源关键字校验（编码启动后必跑）")
        # 仍校验 evidence 路径（上面已做）；此处仅提示
        return
    gw = gateways.get("gateways", {})
    dir_map = {
        "security_reverse": "security",
        "chaos": "chaos",
        "observability": "observability",
    }
    for gname, gcfg in gw.items():
        must = gcfg.get("must_include")
        kws = gcfg.get("keywords")
        if not must or not kws:
            continue
        test_dir = backend / "tests" / dir_map.get(gname, gname)
        if not test_dir.exists():
            errors.append(f"[gateways_verify] {gname} 要求反向/韧性测试，但 {test_dir} 不存在")
            continue
        src = ""
        for f in test_dir.rglob("*.py"):
            src += f.read_text(encoding="utf-8", errors="ignore") + "\n"
        for kw in kws:
            if kw not in src:
                errors.append(f"[gateways_verify] {gname} 测试源缺少关键字 '{kw}'（疑似空壳测试，must_include 未覆盖）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["contract", "doc_sync", "gateways_verify"], required=True)
    args = ap.parse_args()

    if not STATUS.exists():
        print(f"缺失 {STATUS}", file=sys.stderr)
        return 2
    status = load_yaml(STATUS)
    check_status_self_consistency(status)

    if args.mode == "contract":
        td_text = TD.read_text(encoding="utf-8") if TD.exists() else ""
        check_contract_with_code(td_text)
    elif args.mode == "doc_sync":
        check_doc_sync(status)
    elif args.mode == "gateways_verify":
        if not GATEWAYS.exists():
            print(f"缺失 {GATEWAYS}", file=sys.stderr)
            return 2
        gateways = load_yaml(GATEWAYS)
        check_gateways_verify(status, gateways)

    if errors:
        print("=== 校验失败 ===")
        for e in errors:
            print(" -", e)
        return 1
    print(f"[{args.mode}] 校验通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
