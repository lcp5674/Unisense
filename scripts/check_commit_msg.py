#!/usr/bin/env python3
"""提交信息规范校验（pre-commit commit-msg hook）。

格式要求：[服务] 动作：简述 (TD§x.y, FR-xx)
示例：[quality] fix: PII豁免逻辑补全 (TD§12.8, FR-09)

服务名须为 14 个合法服务之一（从 docs/module-status.yaml 读取白名单）。
退出码非 0 = 提交被拒。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATUS_FILE = ROOT / "docs" / "module-status.yaml"

# 14 个合法服务名（与 module-status.yaml modules 对齐）
# 若 module-status.yaml 不可读则回退到此硬编码清单
FALLBACK_SERVICES = {
    "collector", "lineage", "semantic", "conflict", "quality",
    "governance", "consume", "ai", "notify", "observability",
    "assetmap", "recommend", "glossary", "dimension",
}

# 额外允许的非服务前缀（基础设施/文档/CI 变更）
EXTRA_PREFIXES = {"infra", "docs", "ci", "chore"}

PATTERN = re.compile(
    r"^\[([\w-]+)\]\s+"          # [service]
    r"(feat|fix|refactor|test|docs|chore|perf|style)(\([\w-]+\))?:\s+"  # 动作
    r".{4,}"                    # 简述
    r"(?:\s*\(TD§[\w.]+.*\))?"  # 可选 TD 章节
    r"$"
)


def load_services() -> set[str]:
    """从 module-status.yaml 读取合法服务名白名单。"""
    if not STATUS_FILE.exists():
        return FALLBACK_SERVICES
    try:
        import yaml
        data = yaml.safe_load(STATUS_FILE.read_text(encoding="utf-8"))
        modules = data.get("modules", {}) if data else {}
        services = set(modules.keys())
        return services if services else FALLBACK_SERVICES
    except Exception:
        return FALLBACK_SERVICES


def main() -> int:
    if len(sys.argv) < 2:
        return 0
    msg_path = sys.argv[1]
    try:
        msg = open(msg_path, encoding="utf-8").read().strip().splitlines()[0]
    except Exception:
        return 0
    if msg.startswith("Merge") or msg.startswith("Revert"):
        return 0

    match = PATTERN.match(msg)
    if not match:
        print("提交信息不符合规范：")
        print("  正确格式: [服务] 动作：简述 (TD§x.y, FR-xx)")
        print("  示例:     [quality] fix: PII豁免逻辑补全 (TD§12.8, FR-09)")
        print(f"  你的提交: {msg}")
        return 1

    # 校验服务名是否在白名单中
    service = match.group(1)
    valid_services = load_services() | EXTRA_PREFIXES
    if service not in valid_services:
        print(f"提交信息服务名 '{service}' 不在合法白名单中：")
        print(f"  合法服务: {', '.join(sorted(FALLBACK_SERVICES))}")
        print(f"  允许前缀: {', '.join(sorted(EXTRA_PREFIXES))}")
        print(f"  你的提交: {msg}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
