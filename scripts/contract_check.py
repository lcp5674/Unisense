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


def check_contract_with_code(td_text: str) -> None:
    backend = ROOT / "backend"
    if not backend.exists():
        print("[contract] backend/ 不存在，跳过代码级契约比对（仅状态自洽已校验）")
        return
    td_paths = set(re.findall(r"`(/(?:api|v1|metrics|lineage)[^`]*)`", td_text))
    route_hits = 0
    for py in backend.rglob("*.py"):
        txt = py.read_text(encoding="utf-8", errors="ignore")
        if any(p in txt for p in td_paths):
            route_hits += 1
            break
    if td_paths and route_hits == 0:
        errors.append(f"[contract] TD §3 定义了 {len(td_paths)} 个接口路径，backend 中未找到任何匹配实现")
    else:
        print(f"[contract] TD 接口路径 {len(td_paths)} 个，代码命中 {route_hits} 个")


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
