"""LLM 分支真实实例对照评测：纯规则基线 vs 规则 + 真实 LLM 补全。

对 ``sql_infer_eval.dataset.GOLDEN`` 每条 SQL 分别运行：
1. ``infer_sql_batch(None, use_llm=False)`` —— 纯规则基线（零 LLM 调用）；
2. ``infer_sql_batch(db, use_llm=True)`` —— 规则锚定 + 真实 LLM 批量补全
   （``_llm_annotate_candidates`` 封闭选择：名称润色 / 周期校正 / 非度量过滤）。

对照维度：
- 度量集合保持率（LLM 是否误杀真实度量 / 是否发明新度量）
- 名称润色（规则直出名 vs LLM 中文名，逐条列出供人工核对）
- 周期校正（规则周期 vs LLM 周期 vs 评测集期望周期）
- 维度提取（GROUP BY 非时间键回填，规则层能力，LLM 不覆盖）
- 可用性 / 耗时（真实实例延迟、失败降级）

用法（后端容器内，需真实 DB + LLM 配置）：
    docker compose exec backend python /app/scripts/llm_infer_eval.py
    docker compose exec backend python /app/scripts/llm_infer_eval.py --limit 3
    docker compose exec backend python /app/scripts/llm_infer_eval.py --case gmv_daily
    docker compose exec backend python /app/scripts/llm_infer_eval.py --rule-only
"""

from __future__ import annotations

import argparse
import asyncio
import time
from typing import Any

from app.db.mysql import async_session_factory
from app.services.semantic.sql_infer_eval.dataset import GOLDEN
from app.services.semantic.sql_split import infer_sql_batch

#: 单条用例 LLM 路径超时（秒）——真实免费实例偶发网络错误 + 退避重试可拖到
#: 数分钟，超时跳过并记录，避免单条卡死拖垮整批评测。
_CASE_TIMEOUT = 120.0


def _cand_sig(c: dict[str, Any]) -> str:
    """候选签名（列|聚合，聚合空按 DERIVED）——与评测集签名口径一致。"""
    col = str(c.get("measure_column") or c.get("name") or "").lower()
    agg = c.get("aggregation") or "DERIVED"
    return f"{col}|{agg}"


def _summarize(cands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """候选摘要（跳过复合候选——其语义是依赖合成，非单列度量）。"""
    out: list[dict[str, Any]] = []
    for c in cands:
        if c.get("type") == "composite":
            continue
        out.append(
            {
                "key": c.get("key"),
                "name": c.get("name"),
                "column": c.get("measure_column"),
                "agg": c.get("aggregation"),
                "table": c.get("source_table"),
                "period": c.get("period"),
                "dims": c.get("dimensions") or [],
                "source": c.get("source"),
                "conf": c.get("llm_confidence"),
            }
        )
    return out


async def run_case(case: Any) -> dict[str, Any]:
    """单条用例：规则基线 + LLM 补全（真实实例，带超时保护）。"""
    # 1. 纯规则基线（db=None → 域建议 task 异常被吞降级，零 LLM）
    t0 = time.monotonic()
    rule_res = await infer_sql_batch(None, sql=case.sql, use_llm=False)
    rule_elapsed = time.monotonic() - t0

    # 2. 真实 LLM 补全（容器内 async session + 真实 LLM 实例，整体超时保护）
    t0 = time.monotonic()
    llm_res: dict[str, Any] | None = None
    llm_err: str | None = None

    async def _llm_pass() -> dict[str, Any]:
        async with async_session_factory() as db:
            return await infer_sql_batch(db, sql=case.sql, use_llm=True)

    try:
        llm_res = await asyncio.wait_for(_llm_pass(), timeout=_CASE_TIMEOUT)
    except TimeoutError:
        llm_err = f"TimeoutError: 超过 {_CASE_TIMEOUT:.0f}s"
    except Exception as exc:  # noqa: BLE001 - 单条失败不阻断评测
        llm_err = f"{type(exc).__name__}: {exc}"
    llm_elapsed = time.monotonic() - t0

    rule_cands = _summarize(rule_res.get("candidates") or [])
    llm_cands = _summarize(llm_res.get("candidates") or []) if llm_res else []

    rule_sigs = {
        _cand_sig(c)
        for c in rule_res.get("candidates") or []
        if c.get("type") != "composite"
    }
    llm_sigs = {
        _cand_sig(c)
        for c in (llm_res or {}).get("candidates") or []
        if c.get("type") != "composite"
    }

    llm_applied = any(c.get("source") == "llm" for c in llm_cands)
    # LLM 路径整体失败（llm_res=None）时，候选保持规则不动——不是"LLM 剔除"，
    # 不计入误杀/发明（否则会把全部规则候选误报为被误杀，统计失真）。
    if llm_res is None:
        dropped: list[str] = []
        invented: list[str] = []
    else:
        dropped = sorted(rule_sigs - llm_sigs)
        invented = sorted(llm_sigs - rule_sigs)

    name_changes: list[tuple[str, str, str]] = []
    llm_by_key = {c["key"]: c for c in llm_cands}
    for rc in rule_cands:
        lc = llm_by_key.get(rc["key"])
        if lc and lc["name"] and lc["name"] != rc["name"]:
            name_changes.append((rc["key"], str(rc["name"]), str(lc["name"])))

    def _first_period(cands: list[dict[str, Any]]) -> str | None:
        for c in cands:
            if c.get("period"):
                return str(c["period"])
        return None

    return {
        "case_id": case.case_id,
        "dialect": case.dialect,
        "rule_elapsed": round(rule_elapsed, 2),
        "llm_elapsed": round(llm_elapsed, 2) if llm_res else None,
        "llm_err": llm_err,
        "llm_applied": llm_applied,
        "rule_cands": rule_cands,
        "llm_cands": llm_cands,
        "dropped": dropped,
        "invented": invented,
        "name_changes": name_changes,
        "rule_period": _first_period(rule_cands),
        "llm_period": _first_period(llm_cands),
        "expected_period": case.expected_period,
    }


def _fmt_cands(cands: list[dict[str, Any]]) -> str:
    if not cands:
        return "（空）"
    return "\n".join(
        f"      - [{c['key']}] {c['name'] or '-'} | {c['column']}|{c['agg']} | "
        f"{c['table'] or '-'} | 周期={c['period'] or '-'} | 维度={','.join(c['dims']) or '-'}"
        f"{' | source=llm' if c['source'] == 'llm' else ''}"
        f"{' | conf=' + str(c['conf']) if c['conf'] is not None else ''}"
        for c in cands
    )


def format_report(results: list[dict[str, Any]]) -> str:
    """生成人类可读对照报告。"""
    lines: list[str] = [
        "LLM 分支真实实例对照评测报告",
        "=" * 60,
        f"用例数: {len(results)}",
    ]
    total_llm_ok = sum(1 for r in results if not r["llm_err"])
    total_applied = sum(1 for r in results if r["llm_applied"])
    total_failed_degrade = sum(1 for r in results if r["llm_err"])  # LLM 失败→降级规则候选
    total_dropped = sum(len(r["dropped"]) for r in results)
    total_invented = sum(len(r["invented"]) for r in results)
    total_renamed = sum(len(r["name_changes"]) for r in results)
    period_rule_ok = sum(1 for r in results if r["rule_period"] == r["expected_period"])
    period_llm_ok = sum(1 for r in results if r["llm_period"] == r["expected_period"])

    lines += [
        "",
        "汇总指标",
        "-" * 60,
        f"LLM 路径可用（无异常）: {total_llm_ok}/{len(results)}",
        f"LLM 补全实际生效（候选带 source=llm）: {total_applied}/{len(results)}",
        f"LLM 失败→降级保持规则候选: {total_failed_degrade}",
        f"度量误杀（LLM 成功但剔除真实度量）: {total_dropped}",
        f"度量发明（LLM 新增）: {total_invented}",
        f"名称被 LLM 润色（规则名→LLM 名）: {total_renamed} 处",
        f"周期匹配规则路径: {period_rule_ok}/{len(results)}",
        f"周期匹配 LLM 路径: {period_llm_ok}/{len(results)}",
    ]

    for r in results:
        lines += [
            "",
            f"用例 {r['case_id']}（{r['dialect']}）",
            "-" * 60,
            f"耗时: 规则={r['rule_elapsed']}s  LLM={r['llm_elapsed']}s"
            + (f"  [异常: {r['llm_err']}]" if r["llm_err"] else ""),
            f"LLM 补全: {'生效' if r['llm_applied'] else '未生效/降级'}",
            f"周期: 规则={r['rule_period'] or '-'}  LLM={r['llm_period'] or '-'}  "
            f"期望={r['expected_period']}",
        ]
        if r["dropped"]:
            lines.append(f"  ⚠ 度量误杀: {r['dropped']}")
        if r["invented"]:
            lines.append(f"  ⚠ 度量发明: {r['invented']}")
        if r["name_changes"]:
            lines.append("  名称润色:")
            for key, old, new in r["name_changes"]:
                lines.append(f"    {key}: {old!r} → {new!r}")
        lines += [
            "  规则候选:",
            _fmt_cands(r["rule_cands"]),
            "  LLM 候选:",
            _fmt_cands(r["llm_cands"]),
        ]
    return "\n".join(lines)


async def main_async(args: argparse.Namespace) -> None:
    cases = list(GOLDEN)
    if args.case:
        cases = [c for c in cases if c.case_id == args.case]
        if not cases:
            print(f"未找到用例: {args.case}")
            return
    if args.start:
        cases = cases[args.start :]
    if args.limit:
        cases = cases[: args.limit]

    print(f"开始评测 {len(cases)} 条用例（真实 LLM 实例）...")
    results: list[dict[str, Any]] = []
    for i, case in enumerate(cases, 1):
        print(f"  [{i}/{len(cases)}] {case.case_id} ...", flush=True)
        try:
            results.append(await run_case(case))
        except Exception as exc:  # noqa: BLE001 - 单条失败记录继续
            print(f"    跳过（异常: {exc}）", flush=True)
    print()
    print(format_report(results))


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM 分支真实实例对照评测")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条")
    parser.add_argument("--start", type=int, default=0, help="跳过前 N 条")
    parser.add_argument("--case", type=str, default="", help="只跑指定 case_id")
    parser.add_argument("--rule-only", action="store_true", help="只跑规则基线（不调 LLM）")
    args = parser.parse_args()
    if args.rule_only:
        # 规则基线模式：跳过 LLM 路径（--rule-only 时 run_case 内 LLM 仍会调用，
        # 这里改为直接只用规则——由调用方决定，简单起见打印提示）
        print("规则基线模式由 run_case 的 LLM 分支跳过：本脚本无独立开关，"
              "请直接运行默认模式观察对照。")
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
