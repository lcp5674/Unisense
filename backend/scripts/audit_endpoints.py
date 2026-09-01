"""端点全量审计脚本：枚举后端全部 API 端点及其鉴权方式与 service 调用。

纯标准库、零依赖、只读。输出 CSV 到 stdout：
  file, method, path, auth, auth_detail, handler, service_calls

鉴权分类：
  role      — require_roles / dependencies=... / 参数 Depends(require_roles)
  current   — CurrentUser 参数注解 / Depends(get_current_user)（任意登录）
  none      — 无任何鉴权（健康检查等）
  variable  — dependencies=<变量>（需人工确认变量内容）

用法：python scripts/audit_endpoints.py [--service-calls]
"""

from __future__ import annotations

import ast
import csv
import sys
from pathlib import Path

API_DIR = Path(__file__).resolve().parent.parent / "app" / "api"

# 鉴权相关名称
ROLE_DEPS = {"require_roles", "require_any_role", "require_all_roles"}
CURRENT_DEPS = {
    "get_current_user", "get_current_actor", "get_actor",
    "CurrentUser", "get_current_org",
}
# 自定义认证依赖（双通道等，有认证但可能无角色门槛）
CUSTOM_AUTH_DEPS = {
    "get_consume_or_internal_user", "get_api_client_or_user", "get_optional_user",
    "get_current_user_optional", "get_actor_or_none",
}
# 鉴权依赖变量名（装饰器 dependencies=<VAR>）
DEPS_VAR_PREFIXES = ("_", "DEPS", "deps")
# 常见鉴权依赖变量名
KNOWN_DEPS_VARS = {
    "AUTH_DEPS", "ADMIN_DEPS", "READ_DEPS", "WRITE_DEPS", "GOV_DEPS",
    "PII_READ_DEPS", "PII_WRITE_DEPS", "PII_DEPS", "MANAGE_DEPS",
    "CONFIG_ADMIN_DEPS", "SQL_PARSE_DEPS", "SQL_SUGGEST_DEPS", "SQL_QUERY_DEPS",
    "COLLECT_DEPS", "OWNER_DEPS", "REVIEW_DEPS", "CONSUME_DEPS", "EXPORT_DEPS",
    "INTERNAL_DEPS", "RESET_DEPS", "IMPORT_DEPS", "TEMPLATE_DEPS", "DOMAIN_ADMIN_DEPS",
}


def resolve_annot_default(node: ast.expr | None) -> str:
    """解析参数默认值/注解为可读字符串。"""
    if node is None:
        return ""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{resolve_annot_default(node.value)}.{node.attr}"
    if isinstance(node, ast.Call):
        func = resolve_annot_default(node.func)
        args = ",".join(resolve_annot_default(a) for a in node.args)
        kws = ",".join(f"{k.arg}={resolve_annot_default(k.value)}" for k in node.keywords)
        return f"{func}({args}{',' if args and kws else ''}{kws})"
    if isinstance(node, ast.Subscript):
        return f"{resolve_annot_default(node.value)}[{resolve_annot_default(node.slice)}]"
    if isinstance(node, ast.Constant):
        return repr(node.value)
    if isinstance(node, ast.Tuple):
        return "(" + ",".join(resolve_annot_default(e) for e in node.elts) + ")"
    if isinstance(node, ast.BinOp):
        return f"{resolve_annot_default(node.left)}+{resolve_annot_default(node.right)}"
    if isinstance(node, ast.List):
        return "[" + ",".join(resolve_annot_default(e) for e in node.elts) + "]"
    if isinstance(node, ast.Lambda):
        return "<lambda>"
    return f"<{type(node).__name__}>"


def parse_route_decorator(d: ast.Call) -> tuple[str, str] | None:
    """从 @router.get/post(...) 解析 (method, path)。"""
    if not isinstance(d.func, ast.Attribute):
        return None
    method = d.func.attr
    if method not in ("get", "post", "put", "patch", "delete", "head", "options"):
        return None
    path = ""
    if d.args and isinstance(d.args[0], ast.Constant):
        path = d.args[0].value
    elif d.args:
        path = resolve_annot_default(d.args[0])
    return method, path


def is_service_call(node: ast.Call) -> str:
    """判断是否调用 service/repository 方法，返回调用签名（截断）。"""
    if isinstance(node.func, ast.Attribute):
        if isinstance(node.func.value, ast.Name):
            name = f"{resolve_annot_default(node.func.value)}.{node.func.attr}"
        else:
            name = node.func.attr
    elif isinstance(node.func, ast.Name):
        name = node.func.id
    else:
        return ""
    # 只关注 service/repository 层调用
    if any(
        k in name
        for k in ("service.", "repository.", "repo.", "svc.", "collector.", "registry.")
    ):
        return name
    return ""


def extract_service_calls(fn: ast.FunctionDef) -> list[str]:
    """提取 handler 内所有 service 层调用。"""
    calls: list[str] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            name = is_service_call(node)
            if name and name not in calls:
                calls.append(name)
    return calls


def classify_auth(
    fn: ast.FunctionDef,
    decorator_deps: list[str],
    global_names: set[str],
) -> tuple[str, str]:
    """分类端点鉴权方式。返回 (auth_class, auth_detail)。"""
    # 1. 装饰器 dependencies=[Depends(require_roles(...))]
    for dep in decorator_deps:
        if "require_roles" in dep or "require_any" in dep or "require_all" in dep:
            return "role", dep
    # 2. 装饰器 dependencies=<变量或表达式>（非内联 require_roles）
    if decorator_deps:
        return "variable", "|".join(decorator_deps)
    # 3. 参数默认值 Depends(require_roles)
    for arg in fn.args.args:
        if arg.annotation and "require_roles" in resolve_annot_default(arg.annotation):
            return "role", resolve_annot_default(arg.annotation)
    for d in fn.args.defaults:
        ds = resolve_annot_default(d)
        if "require_roles" in ds or "require_any" in ds:
            return "role", ds
    # 4. CurrentUser 参数注解
    for arg in fn.args.args:
        ann = resolve_annot_default(arg.annotation) if arg.annotation else ""
        if any(c in ann for c in CURRENT_DEPS):
            return "current", ann
    # 5. 参数默认值 Depends(get_current_user) / 自定义认证依赖
    for d in fn.args.defaults:
        ds = resolve_annot_default(d)
        if any(c in ds for c in CURRENT_DEPS):
            return "current", ds
        if any(c in ds for c in CUSTOM_AUTH_DEPS):
            return "dual", ds
    # 6. 参数注解 Depends(...)
    for arg in fn.args.args:
        if arg.annotation:
            ann = resolve_annot_default(arg.annotation)
            if any(c in ann for c in CURRENT_DEPS) and "Depends" in ann:
                return "current", ann
    # 7. 参数名暗示
    for arg in fn.args.args:
        if arg.arg in ("user", "actor", "current_user") and arg.annotation is None:
            return "current", f"param:{arg.arg}"
    return "none", ""


def scan_file(path: Path, with_calls: bool) -> list[list[str]]:
    """扫描单个 API 文件，返回行记录。"""
    rows: list[list[str]] = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        rows.append([path.name, "SYNTAX_ERROR", str(exc), "", "", "", ""])
        return rows

    global_names = {n.id for n in tree.body if isinstance(n, ast.Name)}
    for n in tree.body:
        if isinstance(n, (ast.ImportFrom, ast.Import)):
            global_names |= {a.asname or a.name for a in n.names}
    # 收集模块级鉴权依赖变量定义
    for n in tree.body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    global_names.add(t.id)

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # 跳过非路由函数
        decorators = [d for d in node.decorator_list if isinstance(d, ast.Call)]
        route = None
        deps: list[str] = []
        for d in decorators:
            parsed = parse_route_decorator(d)
            if parsed:
                route = parsed
            # 提取 dependencies=...
            for kw in d.keywords:
                if kw.arg == "dependencies":
                    if isinstance(kw.value, ast.List):
                        for el in kw.value.elts:
                            deps.append(resolve_annot_default(el))
                    else:
                        deps.append(resolve_annot_default(kw.value))
        if not route:
            continue
        method, route_path = route
        auth, detail = classify_auth(node, deps, global_names)
        calls = extract_service_calls(node) if with_calls else []
        rows.append([path.name, method, route_path, auth, detail, node.name, "|".join(calls[:12])])
    return rows


def main() -> None:
    with_calls = "--service-calls" in sys.argv
    writer = csv.writer(sys.stdout)
    writer.writerow(["file", "method", "path", "auth", "auth_detail", "handler", "service_calls"])
    all_rows: list[list[str]] = []
    for f in sorted(API_DIR.glob("*.py")):
        all_rows.extend(scan_file(f, with_calls))
    for r in all_rows:
        writer.writerow(r)
    # 汇总统计到 stderr
    classes: dict[str, int] = {}
    for r in all_rows:
        if len(r) >= 4:
            classes[r[3]] = classes.get(r[3], 0) + 1
    print(f"\n# 统计: 共 {len(all_rows)} 端点 {classes}", file=sys.stderr)


if __name__ == "__main__":
    main()
