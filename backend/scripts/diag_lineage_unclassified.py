"""血缘图谱主图「未分层表」只读诊断 + 存量补码建议脚本。

用途
----
生产血缘视图主图（``/lineage/graph``）中仍有「未分层表」时，本脚本做只读分流，
回答两类问题：
  A. 已配层码（如 ``wedw_dwd`` active）却仍显示未分层 → 链路断点在哪一段；
  B. 剩余未分层表是哪些库、能不能补字典码一次归层、补哪些码、覆盖多少。

输出分段：
1. 后端下发：主图 API 返回的表节点是否带 ``dw_layer``（不带 → 后端/数据链路问题）；
2. 字典行：``system_dict.dw_layer`` 的 active 码（没读到/被软删/状态非 active 都不采用）；
3. 节点形态：同一库内「部分归层、部分未分层」的自动检出 + 样例 id
   （引号/大小写/缺库前缀都会让派生落空）；
4. 未分层全景：按库聚类 + **家族聚类**（di_tj* 这类共享前缀的库族一眼可辨）+ 动态结论
   ——不再硬编码 wedw_dwd/wedw_ods 两种"重点库"（生产上它们已全归层时结论会误导）。

动态结论逻辑：
- 某库名**已是字典 active 整库码**却仍有表未分层 → 链路/形态断点（非缺码），列样例；
- 家族与现有 active 码同前缀（如 wedw_* 已配 wedw_ods/wedw_dwd）→ 族内其余库属
  「部分配置」，按整库码补齐即可；
- 家族在字典中完全无覆盖（如 di_tj*）→ 候选新码族，逐库给出建议码 + 覆盖表数 + 样例，
  是否归层（或映射贴源层）由管理员决策。

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
    # 4) 关注别的库族时用 --focus-db（逗号分隔）替换默认 wedw_dwd,wedw_ods
"""

from __future__ import annotations

import argparse
import contextlib
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
                        help="部署环境文件（解析 UNISENSE_SEED_ADMIN_PASSWORD "
                             "与 UNISENSE_MYSQL_*）")
    parser.add_argument("--focus-db", default="wedw_dwd,wedw_ods",
                        help="链路自检用的重点库前缀，逗号分隔（默认 wedw_dwd,wedw_ods）")
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


def _common_prefix(a: str, b: str) -> str:
    """两字符串公共前缀（未分层库家族聚类用）。"""
    for i, (ca, cb) in enumerate(zip(a, b, strict=False)):
        if ca != cb:
            return a[:i]
    return a if len(a) <= len(b) else b


def _cluster_families(dbs: list[str], min_members: int = 2,
                      min_prefix_len: int = 3) -> list[dict]:
    """把库名按最长公共前缀聚成家族（di_tjnanshi/di_tjhepingfuyou…→族 di_tj*）。

    贪心：每轮取「覆盖成员最多（同覆盖取更长前缀）」的前缀，把命中库移出池；
    剩余单库各自成簇（singleton）。前缀长度 < min_prefix_len 不视为家族，
    避免把巧合同前缀的无关库并在一起。
    """
    pool = sorted(set(dbs))
    clusters: list[dict] = []
    while pool:
        best_prefix: str | None = None
        best_members: list[str] = []
        for i, anchor in enumerate(pool):
            for other in pool[i + 1:]:
                prefix = _common_prefix(anchor, other)
                if len(prefix) < min_prefix_len:
                    continue
                members = [x for x in pool if x.startswith(prefix)]
                if len(members) < min_members:
                    continue
                if (best_prefix is None
                        or len(members) > len(best_members)
                        or (len(members) == len(best_members)
                            and len(prefix) > len(best_prefix))):
                    best_prefix = prefix
                    best_members = members
        if best_prefix is None:
            clusters.extend(
                {"family": db, "members": [db], "singleton": True} for db in pool)
            break
        clusters.append({"family": best_prefix, "members": sorted(best_members),
                         "singleton": False})
        pool = [x for x in pool if x not in set(best_members)]
    return clusters


def _analyze(nodes: list[dict], focus_dbs: list[str]) -> dict:
    """按库统计 + 重点库逐库判定 + 部分未分层（形态嫌疑）检出。不落库、不打印敏感字段。"""
    tables = _table_nodes(nodes)
    stats: dict[str, dict] = {}
    for n in tables:
        db = _db_of_table_node(str(n.get("id") or "")) or "<空库名>"
        row = stats.setdefault(db, {"total": 0, "classified": 0, "unclassified": 0,
                                    "samples": []})
        row["total"] += 1
        if _is_unclassified(n):
            row["unclassified"] += 1
            if len(row["samples"]) < 10:
                row["samples"].append(str(n.get("id")))
        else:
            row["classified"] += 1
    unclassified_by_db = Counter({db: row["unclassified"]
                                  for db, row in stats.items() if row["unclassified"] > 0})
    partial = [{"db": db, **row} for db, row in stats.items()
               if row["unclassified"] > 0 and row["classified"] > 0]
    focus = []
    for db in focus_dbs:
        hit = [n for n in tables
               if _db_of_table_node(str(n.get("id") or "")).lower() == db.lower()]
        if not hit:
            continue
        bad = [n for n in hit if _is_unclassified(n)]
        focus.append({"db": db, "total": len(hit),
                      "classified": len(hit) - len(bad), "unclassified": len(bad),
                      "samples": [str(n.get("id")) for n in bad[:10]]})
    return {
        "view_nodes": len(nodes),
        "table_nodes": len(tables),
        "unclassified": sum(unclassified_by_db.values()),
        "unclassified_by_db": unclassified_by_db,
        "focus": focus,
        "partial": partial,
    }


def _print_focus_and_partial(result: dict) -> None:
    """打印重点库逐库状态与同库部分未分层（形态/链路嫌疑）。"""
    if result["focus"]:
        for item in result["focus"]:
            state = ("全部未分层" if item["unclassified"] == item["total"] and item["total"] > 0
                     else "全部已归层" if item["unclassified"] == 0 else "部分未分层")
            print(f"[{item['db']}] {state}：{item['classified']}/{item['total']} 已归层，"
                  f"{item['unclassified']} 未分层")
            for sample in item["samples"]:
                print(f"    未分层样例 id：{sample}")
    if result["partial"]:
        print(f"[形态/链路嫌疑] {len(result['partial'])} 个库同库内既有归层又有未分层"
              "（派生输入不一致），核对样例：")
        for p in result["partial"][:8]:
            print(f"    {p['db']}: {p['classified']} 已归层 / {p['unclassified']} 未分层")
            for sample in p["samples"][:3]:
                print(f"      样例 id：{sample}")


def _family_summary(cluster: dict, counts: Counter, active: set[str]) -> str:
    """家族一行摘要：成员计数 + 字典覆盖状态（已配/部分配/未覆盖/已是码仍未分层）。"""
    family = cluster["family"]
    suffix = "*" if not cluster["singleton"] else ""
    members = ", ".join(f"{m}({counts[m]})" for m in cluster["members"])
    if cluster["singleton"]:
        db = cluster["members"][0]
        if db in active:
            state = "★已是字典 active 整库码仍全未分层 → 链路/形态断点"
        else:
            matched = sorted(code for code in active
                             if len(_common_prefix(db, code)) >= 3)
            state = (f"族内部分配置（已有 {', '.join(matched[:5])}），"
                     "补该整库码即可归层" if matched else "候选补码")
    else:
        has_active_prefix = any(code.startswith(family) for code in active)
        in_family_active = [m for m in cluster["members"] if m in active]
        if in_family_active:
            state = f"★含已是 active 码的成员（{','.join(in_family_active)}）→ 链路/形态断点"
        elif has_active_prefix:
            shared = ",".join(sorted(code for code in active if code.startswith(family))[:5])
            state = f"族内部分配置（已有 {shared}），其余成员可补整库码"
        else:
            state = "字典完全未覆盖的库族，候选整库码/贴源映射（人工决策）"
    coverage = sum(counts[m] for m in cluster["members"])
    return (f"  {family}{suffix:<24} 未分层 {coverage:>4} 张 | "
            f"{members}  [{state}]")


def _print_api_report(view_name: str, result: dict, active: set[str], top: int) -> None:
    """打印单个视角（默认 / provenance=all）的证据摘要。"""
    print(f"\n===== 视角：{view_name}（表节点 {result['table_nodes']} / "
          f"未分层 {result['unclassified']}）=====")
    if result["table_nodes"] == 0:
        print("  该视角无表节点。")
        return
    _print_focus_and_partial(result)
    full = result["unclassified_by_db"]
    if full:
        print(f"全部未分层按库聚类（top {min(top, len(full))}）：")
        for db, count in full.most_common(top):
            print(f"  {db or '<空库名>':<28} {count}")
        clusters = _cluster_families(list(full))
        print(f"家族聚类（{len(clusters)} 族/单库，同前缀库族一眼可辨）：")
        for cluster in clusters:
            print(_family_summary(cluster, full, active))


def _print_action_plan(summary: dict, active: set[str] | None) -> None:
    """基于最大视角（通常 provenance=all）输出动态结论 + 可直接执行的补码清单。"""
    if summary["table_nodes"] == 0:
        return
    print("\n===== [结论与建议] =====")
    un = summary["unclassified_by_db"]
    if not un:
        print("该视角无未分层表，无需处理。")
        return
    if active is None:
        print("（未连库，无法核对字典 active 码；下述建议请在补录前先跑 DB 段核对。）")
    clusters = _cluster_families(list(un))
    for cluster in clusters:
        rows = cluster["members"]
        already = [m for m in rows if m in (active or set())]
        if already:
            print(f"\n★ 库名已是字典 active 整库码却仍有表未分层（{', '.join(already)}）")
            print("  → 非缺码，是链路/形态断点：核对上面样例 id 的大小写/引号/缺库前缀；")
            print("    若整库普遍如此，查生产后端是否跑着「table 节点统一派生 + graph 透传 "
                  "dw_layer」的版本（缺透传时主图表节点全落未分层）。")
        pending = [m for m in rows if m not in (active or set())]
        if not pending:
            continue
        print(f"\n建议补录（{len(pending)} 个整库码，共覆盖 "
              f"{sum(un[m] for m in pending)} 张未分层表）：")
        for m in pending:
            print(f"  code={m:<24} label=<中文层名>   覆盖 {un[m]:>4} 张  样例 {m}.<表名>")
    if active is None:
        print("\n判定口径：目标库对应码须 status='active' 且 deleted_at IS NULL，"
              "读路径才采用（软删不参与派生）。")


def _run_api_diagnostics(args: argparse.Namespace, password: str,
                         active: set[str]) -> list[dict]:
    """登录按 --provenance 视角拉图、分段判定，返回各视角分析结果（最大视角置尾便于汇总）。"""
    token = _api_login(args.api_base, args.username, password, insecure=args.insecure)
    print(f"登录成功（{args.username}），开始拉取血缘主图……")
    focus_dbs = [db.strip() for db in args.focus_db.split(",") if db.strip()]
    provenances: list[tuple[str, str | None]] = []
    if args.provenance in ("both", "empty"):
        provenances.append(("默认采集目录视角", None))
    if args.provenance in ("both", "all"):
        provenances.append(("provenance=all（血缘边完整表级）", "all"))
    reports = []
    for view_name, provenance in provenances:
        nodes = _fetch_graph_nodes(args.api_base, token, provenance, insecure=args.insecure)
        result = _analyze(nodes, focus_dbs)
        _print_api_report(view_name, result, active, args.top)
        reports.append(result)
    return reports


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


def _check_dict(args: argparse.Namespace, env: dict[str, str]) -> set[str] | None:
    """只读 SELECT system_dict，返回 active 码集合；缺依赖/连不上返回 None 并打印 SQL。"""
    if args.skip_db:
        return None
    try:
        import pymysql  # 延迟导入：仅本段需要
    except ImportError:
        _dict_sql_hint()
        return None
    host = args.mysql_host or "127.0.0.1"
    port = args.mysql_port or 3306
    user = env.get("UNISENSE_MYSQL_USER")
    password = env.get("UNISENSE_MYSQL_PASSWORD")
    database = env.get("UNISENSE_MYSQL_DATABASE")
    if not (user and password and database):
        print("[DB 字典检查被跳过] .env 缺 UNISENSE_MYSQL_USER/PASSWORD/DATABASE 键。")
        _dict_sql_hint()
        return None
    try:
        conn = pymysql.connect(host=host, port=port, user=user, password=password,
                               database=database, connect_timeout=5, charset="utf8mb4")
    except Exception as exc:  # noqa: BLE001 - 连接失败仅提示
        print(f"[DB 字典检查失败] 连接 {host}:{port} 失败：{exc}")
        _dict_sql_hint()
        return None
    active: set[str] = set()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT code, label, status, deleted_at FROM system_dict "
                        "WHERE dict_type='dw_layer' ORDER BY sort_order, code")
            rows = cur.fetchall()
        print(f"\n===== system_dict.dw_layer 字典（共 {len(rows)} 行）=====")
        for code, label, status, deleted_at in rows:
            is_active = status == "active" and deleted_at is None
            if is_active and code:
                active.add(code)
            state = ("生效" if is_active
                     else f"失效(status={status},deleted={deleted_at is not None})")
            print(f"  {(code or '<空>'):<20} {(label or ''):<20} {state}")
    finally:
        conn.close()
    return active


def main() -> None:
    # 规避 GBK 终端把中文标题打成乱码；reconfigure 不可用时忽略
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8")
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
    active = _check_dict(args, env)
    reports = _run_api_diagnostics(args, password, active or set())
    if reports:
        # 汇总用「表节点最多」的视角（provenance=all 通常最全），避免小视角误导
        summary = max(reports, key=lambda r: r["table_nodes"])
        _print_action_plan(summary, active)


if __name__ == "__main__":
    main()
