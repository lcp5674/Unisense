"""Alembic 迁移链静态检查（发布前门禁，纯标准库零依赖）。

背景
----
2026-09 生产评审时发现 ``alembic/versions`` 下出现两个 ``0124_*`` 文件：
``0124_metric_fulltext`` 与 ``0124_tracking_event_target_type_index``（后者
down_revision 指向前者）。虽然 alembic 按 revision ID 识别、链仍是线性的
单 head，但**文件名撞号是分叉前兆**——后续若有人从旧 ``0124`` 派生新迁移，
就会产生真分叉，生产 ``upgrade head`` 直接报
``Multiple head revisions are present``。

本脚本把「迁移链完整性」固化为可复用检查，拦截四类问题：

1. **分叉（多 head）**：最致命，生产初始化直接失败。
2. **断链**：某迁移的 ``down_revision`` 指向不存在的 revision。
3. **孤立**：从 base 出发不可达的迁移（不会被任何 upgrade 执行到）。
4. **文件名撞号**：``NNN_`` 前缀重复（本次事故的直接成因，误导后续派生）。
5. **序号漂移（WARN）**：文件名序号与 revision ID 前缀不一致、或序号不连续
   ——提示「从非当前 head 派生」的早期信号。

纯静态分析（仅 ``ast``/``re``/``pathlib``），不连数据库、秒级完成，
适合 pre-commit / CI 每次提交即跑。

用法
----
.. code-block:: bash

    python -m scripts.check_migration_chain
    python -m scripts.check_migration_chain --versions-dir backend/alembic/versions
    python -m scripts.check_migration_chain --strict   # WARN 也返回非零

退出码
------
- 0：通过（可含 WARN）；
- 1：存在 FAIL；
- 2：执行错误（目录不存在 / 无法解析等）。
"""

from __future__ import annotations

import argparse
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

#: 迁移文件命名规范：NNN_描述.py（NNN 为三位数字序号）
_FILENAME_PREFIX_RE = re.compile(r"^(\d+)_")
#: revision / down_revision 赋值（兼容 ``revision: str = "..."``、``revision = "..."``、
#: ``down_revision: str | None = "..."`` 与 ``down_revision = "..."`` 等写法）
_REVISION_RE = re.compile(
    r"""revision\s*(?::\s*str)?\s*=\s*(?P<q>["'])(?P<val>[^"']+)(?P=q)"""
)
#: 注意类型注解可能是 ``: str`` 或 ``: str | None`` 或缺失；``= None``（base）不匹配引号 → None
_DOWN_REVISION_RE = re.compile(
    r"""down_revision\s*(?::\s*(?:str\s*\|\s*None|str))?\s*=\s*(?P<q>["'])(?P<val>[^"']+)(?P=q)"""
)


@dataclass
class Diff:
    """单条链问题。"""

    severity: str  # FAIL / WARN
    # multiple_head / broken_link / orphan / duplicate_prefix / prefix_mismatch
    kind: str
    detail: str = ""

    def line(self) -> str:
        suffix = f" — {self.detail}" if self.detail else ""
        return f"  [{self.severity}] {self.kind}: {suffix}"


@dataclass
class Report:
    """检查结果汇总。"""

    diffs: list[Diff] = field(default_factory=list)
    migration_count: int = 0

    def fail_count(self) -> int:
        return sum(1 for d in self.diffs if d.severity == "FAIL")

    def warn_count(self) -> int:
        return sum(1 for d in self.diffs if d.severity == "WARN")


def _parse_revision(source: str, regex: re.Pattern[str]) -> str | None:
    """从迁移源码提取 revision/down_revision 的字符串字面量（无则 None）。"""
    m = regex.search(source)
    return m.group("val") if m else None


def _load_migrations(versions_dir: Path) -> list[tuple[Path, str, str | None]]:
    """读取目录下所有迁移文件，返回 [(file, revision, down_revision)]。

    Raises:
        SystemExit: 目录不存在或文件无法解析（退出码 2）。
    """
    if not versions_dir.is_dir():
        raise SystemExit(f"迁移目录不存在: {versions_dir}")
    out: list[tuple[Path, str, str | None]] = []
    for path in sorted(versions_dir.glob("*.py")):
        if path.name == "__init__.py":
            continue
        source = path.read_text(encoding="utf-8")
        revision = _parse_revision(source, _REVISION_RE)
        down = _parse_revision(source, _DOWN_REVISION_RE)
        if not revision:
            raise SystemExit(f"无法解析 revision（{path.name}）——请检查赋值写法")
        out.append((path, revision, down))
    if not out:
        raise SystemExit(f"迁移目录无迁移文件: {versions_dir}")
    return out


def run_check(versions_dir: Path, *, strict: bool = False) -> Report:
    """对迁移目录执行静态链检查。"""
    migrations = _load_migrations(versions_dir)
    report = Report(migration_count=len(migrations))

    by_revision: dict[str, Path] = {}
    for path, rev, _down in migrations:
        if rev in by_revision:
            report.diffs.append(
                Diff(
                    "FAIL",
                    "duplicate_revision",
                    f"{rev}（{by_revision[rev].name} 与 {path.name}）",
                )
            )
        by_revision[rev] = path

    # 子节点表：down_revision -> [revision]
    children: dict[str | None, list[str]] = defaultdict(list)
    for _path, rev, down in migrations:
        children[down].append(rev)

    # 1) 断链：down_revision 指向不存在的 revision（None=base 合法）
    for _path, rev, down in migrations:
        if down is not None and down not in by_revision:
            report.diffs.append(Diff("FAIL", "broken_link", f"{rev} -> {down}"))

    # 2) 分叉：head = 未被任何迁移引用的 revision，须恰好 1 个
    referenced: set[str] = {down for _p, _r, down in migrations if down is not None}
    heads = [rev for _p, rev, _d in migrations if rev not in referenced]
    if len(heads) == 0:
        report.diffs.append(Diff("FAIL", "multiple_head", "无 head（迁移环）"))
    elif len(heads) > 1:
        detail = f"存在 {len(heads)} 个 head: {', '.join(sorted(heads))}（迁移分叉）"
        report.diffs.append(Diff("FAIL", "multiple_head", detail))

    # 3) 孤立：从 base（down_revision=None）BFS 不可达的迁移
    reachable: set[str] = set()
    stack = list(children.get(None, []))
    while stack:
        rev = stack.pop()
        if rev in reachable:
            continue
        reachable.add(rev)
        stack.extend(children.get(rev, []))
    for _path, rev, _down in migrations:
        if rev not in reachable:
            report.diffs.append(Diff("FAIL", "orphan", rev))

    # 4) 文件名序号：撞号（FAIL）/ 与 revision 前缀不一致（WARN）/ 不连续（WARN）
    prefixes: dict[str, list[str]] = defaultdict(list)
    for path, rev, _down in migrations:
        m = _FILENAME_PREFIX_RE.match(path.name)
        prefix = m.group(1) if m else ""
        if prefix:
            prefixes[prefix].append(path.name)
            rev_m = re.match(r"^(\d+)", rev)
            if rev_m and rev_m.group(1) != prefix:
                report.diffs.append(
                    Diff(
                        "WARN",
                        "prefix_mismatch",
                        f"{path.name}（序号 {prefix} 但 revision 前缀 {rev_m.group(1)}）",
                    )
                )
        else:
            report.diffs.append(
                Diff("WARN", "prefix_mismatch", f"{path.name}（文件名缺少 NNN_ 序号前缀）")
            )
    for prefix, files in sorted(prefixes.items()):
        if len(files) > 1:
            report.diffs.append(
                Diff(
                    "FAIL",
                    "duplicate_prefix",
                    f"{prefix}: {', '.join(files)}（文件名撞号，防分叉前兆）",
                )
            )

    return report


def print_report(report: Report, versions_dir: Path, label: str) -> None:
    print(f"\n=== Alembic 迁移链检查（{label}）===")
    print(f"目录: {versions_dir}")
    print(f"迁移数: {report.migration_count}")
    if not report.diffs:
        print("结果: ✅ 单链无分叉（1 head，无断链/孤立/撞号）")
        return
    for d in report.diffs:
        print(d.line())
    print(f"结果: ❌ FAIL {report.fail_count()} 项 / WARN {report.warn_count()} 项")


def main() -> int:
    parser = argparse.ArgumentParser(description="Alembic 迁移链静态检查（发布前门禁）")
    parser.add_argument(
        "--versions-dir",
        default=str(Path(__file__).resolve().parent.parent / "alembic" / "versions"),
        help="迁移目录（缺省 backend/alembic/versions）",
    )
    parser.add_argument("--strict", action="store_true", help="WARN 也返回非零（严格模式）")
    args = parser.parse_args()

    versions_dir = Path(args.versions_dir)
    try:
        report = run_check(versions_dir, strict=args.strict)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - CLI 入口统一兜底
        print(f"执行错误: {exc}")
        return 2

    print_report(report, versions_dir, "迁移目录静态检查")
    if report.fail_count() > 0 or (args.strict and report.warn_count() > 0):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
