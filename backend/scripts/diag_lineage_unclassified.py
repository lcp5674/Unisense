"""血缘图谱主图「已配层码仍显示未分层」只读自检脚本。

用途
----
生产血缘视图主图（``/lineage/graph``）中出现「``system_dict.dw_layer`` 已配置
（如 ``wedw_dwd`` 整库码 active）却仍落在「未分层表」泳道」的表时，本脚本做只读
分流定位，把责任在四段之间钉死：

1. 后端下发：主图 API 返回的表节点是否带 ``dw_layer``（不带 → 后端/数据链路问题）；
2. 字典行：``dw_layer`` 的 active 码里到底有没有目标库（没读到/被软删/状态非 active）；
3. 节点形态：未分层节点 id 是否干净 ``table:wedw_dwd.xxx``（引号/大小写/缺库前缀都会落空）；
4. 前端消费：API 已正确下发时，问题在前端（跑旧版不认扩展层 / 浏览器缓存旧 chunk）。

只读保证
--------
- API：仅 ``POST /auth/login`` 与 ``GET /lineage/graph``；
- DB：仅 ``SELECT system_dict``（需 pymysql；缺失或不可连时打印等价 SQL，由用户在
  能连库的机器执行）。脚本不 import 项目包、不写任何表，可脱离 backend venv 直跑。

用法（在能访问生产 API 的机器执行，密码不落命令行）
----------------------------------------------------
    # 1) 密码取 .env.production 的 UNISENSE_SEED_ADMIN_PASSWORD（推荐）
    python3 diag_lineage_unclassified.py \\
        --api-base http://127.0.0.1:8100/api/v1 --env-file .env.production

    # 2) 只跑 API 分水岭，不连库（docker exec 里没 pymysql 时自动退化打印 SQL）
    python3 diag_lineage_unclassified.py --env-file .env.production --skip-db

    # 3) https 自签加 --insecure；MySQL 不在本机默认端口时加 --mysql-host/--mysql-port
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

#: 表节点 id 前缀（与后端 resolve_node_meta / graph_from_edges 一致）
TABLE_PREFIX = "table:"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", default="http://127.0.0.1:8100/api/v1",
                        help="后端 API 基址（含 /api/v1）")
    parser.add_argument("--username", default="admin", help="登录账号（默认 admin）")
    parser.add_argument("--password", default=None,
                        help="登录密码；缺省读 env 的 UNISENSE_SEED_ADMIN_PASSWORD，避免落命令行")
    parser.add_argument("--env-file", default=None,
                        help="部署环境文件（解析 UNISENSE_SEED_ADMIN_PASSWORD 与 UNISENSE_MYSQL_*）")
    parser.add_argument("--focus-db", default="wedw_dwd,wedw_ods",
                        help="重点核验的库前缀，逗号分隔（默认 wedw_dwd,wedw_ods）")
    parser.add_argument("--provenance", choices=("both", "all", "empty"), default="both",
                        help="主图视角：both=默认采集目录视角 + provenance=all 各查一次")
    parser.add_argument("--top", type=int, default=15, help="未分层按库聚类的展示条数")
    parser.add_argument("--skip-db", action="store_true", help="跳过 system_dict 字典检查")
    parser.add_argument("--insecure", action="store_true", help="https 自签证书时跳过校验")
    parser.add_argument("--mysql-host", default=None, help="MySQL 主机（缺省 127.0.0.1）")
    parser.add_argument("--mysql-port", type=int, default=None, help="MySQL 端口（缺省 3306）")
    return parser.parse_args()


def _load_env_file(path: str) -> dict[str, str]:
    """解析 KEY=VALUE 环境文件（容忍 export 前缀、引号、空行与 # 注释）。"""
    env: dict[str, str] = {}
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.removeprefix("export ").strip()
            value = value.strip().strip("\"'")
            if key:
                env[key] = value
    return env


def _resolve_password(args: argparse.Namespace, env: dict[str, str]) -> str:
    """密码来源：--password > env UNISENSE_SEED_ADMIN_PASSWORD > ADMIN_INITIAL_PASSWORD。"""
    for source in (args.password, env.get("UNISENSE_SEED_ADMIN_PASSWORD"),
                   env.get("ADMIN_INITIAL_PASSWORD")):
        if source:
            return source
    print("错误：未提供密码。请用 --password 或 --env-file 指向含 "
          "UNISENSE_SEED_ADMIN_PASSWORD 的部署环境文件（不打印明文）。")
    sys.exit(2)


def _request_json(url: str, method: str, headers: dict[str, str] | None = None,
                  payload: dict | None = None, *, insecure: bool = False,
                  timeout: int = 30) -> tuple[int, dict]:
    """发 JSON 请求并解析统一信封（{code,message,data,trace_id}），返回 (http状态, 信封)。"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    ctx = ssl._create_unverified_context() if insecure else None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 - 非 JSON 错误体仅透传状态
            body = {"message": exc.reason}
        return exc.code, body
    except urllib.error.URLError as exc:
        return 0, {"message": f"无法连接 {url}：{exc.reason}"}


def _api_login(base: str, username: str, password: str, *, insecure: bool) -> str:
    """登录并返回 access_token；totp_required 或凭据错误直接退出。"""
    status, body = _request_json(
        f"{base}/auth/login", "POST", payload={"username": username, "password": password},
        insecure=insecure)
    if status != 200 or not isinstance(body.get("data"), dict):
        print(f"登录失败（HTTP {status}）：{body.get('message') or body.get('detail') or body}")
        sys.exit(3)
    data = body["data"]
    if data.get("totp_required"):
        print("登录被 TOTP 二次校验拦截：本脚本不支持 2FA，请换非 2FA 账号或关闭后重试。")
        sys.exit(3)
    return data["access_token"]


def _fetch_graph_nodes(base: str, token: str, provenance: str | None, *,
                       insecure: bool) -> list[dict]:
    """拉取 /lineage/graph 节点列表（provenance=None 即默认采集目录视角）。"""
    params = {"limit": "5000"}
    if provenance:
        params["provenance"] = provenance
    url = f"{base}/lineage/graph?{urllib.parse.urlencode(params)}"
    status, body = _request_json(url, "GET", headers={"Authorization": f"Bearer {token}"},
                                 insecure=insecure)
    if status != 200:
        print(f"拉取 {url} 失败（HTTP {status}）：{body.get('message') or body.get('detail')}")
        sys.exit(3)
    return (body.get("data") or {}).get("nodes") or []


def _table_nodes(nodes: list[dict]) -> list[dict]:
    """筛出表节点（type==table 或 id 以 table: 开头）。"""
    result = []
    for node in nodes:
        node_id = str(node.get("id") or "")
        if node.get("type") == "table" or node_id.startswith(TABLE_PREFIX):
            result.append(node)
    return result


def _db_of_table_node(node_id: str) -> str:
    """从表节点 id 提取库名：table:wedw_dwd.181_test_1_df -> wedw_dwd。"""
    rest = node_id.split(":", 1)[1] if ":" in node_id else node_id
    return rest.split(".", 1)[0]


def _is_unclassified(node: dict) -> bool:
    """未分层 = 未下发 dw_layer 或为空。"""
    layer = node.get("dw_layer")
    return layer is None or str(layer).strip() == ""


def _analyze(nodes: list[dict], focus_dbs: list[str], top: int) -> dict:
    """按四段证据组装统计结果（不落库、不打印敏感字段）。"""
    tables = _table_nodes(nodes)
    unclassified = [n for n in tables if _is_unclassified(n)]
    by_db = Counter(_db_of_table_node(str(n.get("id") or "")) for n in unclassified)
    focus = []
    for db in focus_dbs:
        hit = [n for n in tables if _db_of_table_node(str(n.get("id") or "")).lower() == db.lower()]
        if not hit:
            continue
        bad = [n for n in hit if _is_unclassified(n)]
        focus.append({
            "db": db,
            "total": len(hit),
            "classified": len(hit) - len(bad),
            "unclassified": len(bad),
            "samples": [str(n.get("id")) for n in bad[:10]],
        })
    return {
        "total_nodes": len(nodes),
        "table_nodes": len(tables),
        "unclassified": len(unclassified),
        "unclassified_by_db": by_db.most_common(top),
        "focus": focus,
    }


def _print_api_report(view_name: str, result: dict) -> None:
    """打印单个视角（默认/ provenance=all）的四段证据摘要。"""
    print(f"\n===== 视角：{view_name}（表节点 {result['table_nodes']} / "
          f"未分层 {result['unclassified']}）=====")
    if not result["focus"]:
        print("重点库在图中无表节点（无数据或库名不符），跳过分段判定。")
        return
    for item in result["focus"]:
        state = ("全部未分层" if item["unclassified"] == item["total"] and item["total"] > 0
                 else "全部已归层" if item["unclassified"] == 0 else "部分未分层")
        print(f"[{item['db']}] {state}：{item['classified']}/{item['total']} 已归层，"
              f"{item['unclassified']} 未分层")
        if item["samples"]:
            print("  未分层样例 id（核对形态/大小写/引号）：")
            for sample in item["samples"]:
                print(f"    {sample}")
    if result["unclassified_by_db"]:
        print(f"全部未分层按库聚类（top {len(result['unclassified_by_db'])}）：")
        for db, count in result["unclassified_by_db"]:
            print(f"  {db or '<空库名>'}: {count}")


def _run_api_diagnostics(args: argparse.Namespace, password: str) -> None:
    """登录并按 --provenance 指定的视角拉图、分段判定、打印报告。"""
    token = _api_login(args.api_base, args.username, password, insecure=args.insecure)
    print(f"登录成功（{args.username}），开始拉取血缘主图……")
    focus_dbs = [db.strip() for db in args.focus_db.split(",") if db.strip()]
    provenances: list[tuple[str, str | None]] = []
    if args.provenance in ("both", "empty"):
        provenances.append(("默认采集目录视角", None))
    if args.provenance in ("both", "all"):
        provenances.append(("provenance=all（血缘边完整表级）", "all"))
    for view_name, provenance in provenances:
        nodes = _fetch_graph_nodes(args.api_base, token, provenance, insecure=args.insecure)
        _print_api_report(view_name, _analyze(nodes, focus_dbs, args.top))


def _dict_sql_hint() -> None:
    """无 pymysql / 连不上时打印等价只读 SQL，供用户在能连库的机器执行。"""
    print("""
[DB 字典检查被跳过/不可用] 请在能连库的机器执行以下只读 SQL 核对 dw_layer active 码：

    SELECT code, label, status, deleted_at, updated_at
    FROM system_dict
    WHERE dict_type = 'dw_layer'
    ORDER BY sort_order, code;

容器内示例：docker exec <mysql容器> mysql -u<user> -p<pass> <db> -e "上面SQL"
判定：目标库对应的码须 status='active' 且 deleted_at IS NULL；有任意一项不满足，
字典行即不被读路径采用（软删不参与派生，见 load_active_dw_layer_codes）。
""")


def _check_dict(args: argparse.Namespace, env: dict[str, str]) -> None:
    """只读 SELECT system_dict 核对 dw_layer active 码；缺依赖/连不上则打印 SQL 提示。"""
    if args.skip_db:
        return
    try:
        import pymysql  # 延迟导入：仅本段需要
    except ImportError:
        _dict_sql_hint()
        return
    host = args.mysql_host or "127.0.0.1"
    port = args.mysql_port or 3306
    user = env.get("UNISENSE_MYSQL_USER")
    password = env.get("UNISENSE_MYSQL_PASSWORD")
    database = env.get("UNISENSE_MYSQL_DATABASE")
    if not (user and password and database):
        print("[DB 字典检查被跳过] .env 缺 UNISENSE_MYSQL_USER/PASSWORD/DATABASE 键。")
        _dict_sql_hint()
        return
    try:
        conn = pymysql.connect(host=host, port=port, user=user, password=password,
                               database=database, connect_timeout=5, charset="utf8mb4")
    except Exception as exc:  # noqa: BLE001 - 连接失败仅提示
        print(f"[DB 字典检查失败] 连接 {host}:{port} 失败：{exc}")
        _dict_sql_hint()
        return
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT code, label, status, deleted_at FROM system_dict "
                        "WHERE dict_type='dw_layer' ORDER BY sort_order, code")
            rows = cur.fetchall()
        print(f"\n===== system_dict.dw_layer 字典（共 {len(rows)} 行）=====")
        for code, label, status, deleted_at in rows:
            state = "生效" if status == "active" and deleted_at is None else f"失效(status={status},deleted={deleted_at is not None})"
            print(f"  {code or '<空>':<20} {label or '':<20} {state}")
    finally:
        conn.close()


def main() -> None:
    args = _parse_args()
    env: dict[str, str] = {}
    if args.env_file and os.path.exists(args.env_file):
        env = _load_env_file(args.env_file)
    else:
        for candidate in (".env.production", "../.env.production",
                          os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "..", "..", ".env.production")):
            if os.path.exists(candidate):
                env = _load_env_file(candidate)
                print(f"已从 {candidate} 读取部署配置。")
                break
    password = _resolve_password(args, env)
    _run_api_diagnostics(args, password)
    _check_dict(args, env)
    print("""
===== 分流结论 =====
- 重点库「全部已归层」→ 后端/数据无罪，请硬刷新（Cmd+Shift+R）血缘页，仍复现则查前端
  运行产物是否含「扩展层自动成带」逻辑（老版只认硬编码单段白名单，整库码会落未分层）。
- 重点库「全部未分层」→ 后端或字典链路问题：核对上方 dw_layer active 码；码在且 active，
  则查生产后端是否跑着透传 dw_layer 的版本（graph_from_edges 缺透传时主图表节点全落未分层）。
- 重点库「部分未分层」→ 节点形态问题：看未分层样例 id 是否带引号/大小写/缺库前缀。
""")


if __name__ == "__main__":
    main()
