"""模型↔迁移一致性检查（发布前门禁脚本）。

用途
----
把「ORM 模型（Base.metadata）与数据库实际结构是否一致」固化为可复用检查，
拦截两类典型生产事故：

1. **迁移漏建表/漏加列**：迁移链能跑通，但模型里新增的表/列没有对应迁移
   （或迁移只建了部分列）→ 上线后 ORM 全列查询报 ``Unknown column 'xxx'`` 500。
   本项目真实案例：0120 迁移建的 3 张表漏了 ``deleted_at``/``updated_at``
   （模型基类 BaseModel 含 SoftDeleteMixin/TimestampMixin 要求三列齐全）。
2. **干净库初始化失败**：生产新环境从零 ``alembic upgrade head`` 是否一次到位。

两种运行模式
------------
- **模式 A（默认，快速）**：对 ``--db-url`` 指向的现有库，直接对比模型↔DB。
- **模式 B（--init-check，完整模拟生产初始化）**：
  建临时干净库 → ``alembic upgrade head`` → 对比 → 清理临时库。
  需要具备建库权限的连接（默认从 ``UNISENSE_MYSQL_ROOT_USER/PASSWORD`` 派生）。

用法
----
.. code-block:: bash

    python -m scripts.check_schema_consistency                  # 模式 A（settings.db_url）
    python -m scripts.check_schema_consistency --db-url mysql+pymysql://u:p@h:3306/db
    python -m scripts.check_schema_consistency --init-check     # 模式 B
    python -m scripts.check_schema_consistency --check-types --check-indexes --fail-on-warn

退出码
------
- 0：通过（可含警告）；
- 1：存在 FAIL（缺失表/缺失列等，CI 应拦截）；
- 2：执行错误（连接失败/无建库权限等）。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 将 backend/ 加入 sys.path，确保 CLI 直接执行时也能 import app
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import pymysql  # noqa: E402
from sqlalchemy.dialects import mysql  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db.mysql import Base  # noqa: E402
from app.models import *  # noqa: E402, F401, F403 — 注册全部模型到 Base.metadata

#: information_schema 中与 ORM 无关、允许存在的表（alembic 版本表等）
_IGNORED_DB_TABLES = {"alembic_version"}


@dataclass
class Diff:
    """单条结构差异。"""

    severity: str  # FAIL / WARN
    # missing_table / extra_table / missing_column / extra_column / type_mismatch / missing_index
    kind: str
    table: str
    detail: str = ""

    def line(self) -> str:
        suffix = f" — {self.detail}" if self.detail else ""
        return f"  [{self.severity}] {self.kind}: {self.table}{suffix}"


@dataclass
class Report:
    """检查结果汇总。"""

    diffs: list[Diff] = field(default_factory=list)
    checked_tables: int = 0
    checked_columns: int = 0

    def fail_count(self) -> int:
        return sum(1 for d in self.diffs if d.severity == "FAIL")

    def warn_count(self) -> int:
        return sum(1 for d in self.diffs if d.severity == "WARN")


# --------------------------------------------------------------------------- #
# 连接
# --------------------------------------------------------------------------- #
def _conn_params(url: str) -> dict[str, Any]:
    """从 SQLAlchemy DSN 解析 pymysql 连接参数（去掉驱动前缀）。"""
    u = make_url(url)
    return {
        "host": u.host or "localhost",
        "port": u.port or 3306,
        "user": u.username or "",
        "password": u.password or "",
        "database": u.database or "",
        "charset": "utf8mb4",
        "autocommit": True,
    }


def _connect(url: str, database: str | None = None) -> pymysql.connections.Connection:
    """建立 pymysql 连接（可覆盖目标库名）。"""
    p = _conn_params(url)
    if database is not None:
        p["database"] = database
    return pymysql.connect(**p)


# --------------------------------------------------------------------------- #
# DB 侧结构采集
# --------------------------------------------------------------------------- #
def _db_tables(conn: pymysql.connections.Connection, schema: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %s AND table_type = 'BASE TABLE'",
            (schema,),
        )
        return {row[0] for row in cur.fetchall()}


def _db_columns(conn: pymysql.connections.Connection, schema: str) -> dict[str, dict[str, str]]:
    """返回 {表名: {列名: column_type}}（仅当前 schema，小写归一化）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name, column_type FROM information_schema.columns "
            "WHERE table_schema = %s",
            (schema,),
        )
        out: dict[str, dict[str, str]] = {}
        for table, column, ctype in cur.fetchall():
            out.setdefault(table, {})[column.lower()] = ctype
        return out


def _db_indexes(conn: pymysql.connections.Connection, schema: str) -> dict[str, set[str]]:
    """返回 {表名: {索引名}}（仅当前 schema）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT table_name, index_name FROM information_schema.statistics "
            "WHERE table_schema = %s",
            (schema,),
        )
        out: dict[str, set[str]] = {}
        for table, index in cur.fetchall():
            out.setdefault(table, set()).add(index)
        return out


# --------------------------------------------------------------------------- #
# 类型归一化对比
# --------------------------------------------------------------------------- #
def _norm_type(col_type: str) -> str:
    """归一化 MySQL column_type：小写、去显示宽度、去 unsigned。

    ``bigint(20) unsigned`` → ``bigint``；``int(11)`` → ``int``；
    ``varchar(64)`` → ``varchar``（长度差异单独比较）。
    """
    t = col_type.lower().split(" ")[0]
    if "(" in t:
        t = t.split("(")[0]
    return t


def _model_type_compile(col_type: Any) -> str:
    """把 SQLAlchemy 列类型编译为 MySQL 方言字符串并归一化。"""
    raw = col_type.compile(dialect=mysql.dialect())
    return _norm_type(str(raw))


def _len_detail(db_type: str, model_type: Any) -> str:
    """提取长度/精度细节用于差异描述（如 varchar(64) vs varchar(128)）。"""
    db = db_type.lower()
    model = str(model_type.compile(dialect=mysql.dialect())).lower()
    return f"db={db} vs model={model}"


# --------------------------------------------------------------------------- #
# 主检查
# --------------------------------------------------------------------------- #
def run_check(
    url: str,
    *,
    check_types: bool,
    check_indexes: bool,
    report_extra: bool = False,
) -> Report:
    """对 url 指向的库执行模型↔DB 对比，返回差异报告。

    Args:
        report_extra: 是否报告「DB 有而模型无」的表/列（运行时自建表，默认隐藏
            以降噪；核心检查方向是「模型要求而 DB 缺失」）。
    """
    p = _conn_params(url)
    schema = p["database"]
    if not schema:
        raise SystemExit("目标库名缺失：--db-url 必须包含 database 段（如 .../unisense）")
    conn = _connect(url)

    report = Report()
    db_tables = _db_tables(conn, schema)
    db_cols = _db_columns(conn, schema)
    db_idx = _db_indexes(conn, schema) if check_indexes else {}

    model_tables = {name.lower(): tbl for name, tbl in Base.metadata.tables.items()}

    # 1) 表级：缺失 / 多余
    for name_lower in sorted(model_tables):
        if name_lower not in db_tables:
            report.diffs.append(Diff("FAIL", "missing_table", name_lower))
    if report_extra:
        for db_name in sorted(db_tables):
            if db_name.lower() not in model_tables and db_name not in _IGNORED_DB_TABLES:
                report.diffs.append(Diff("WARN", "extra_table", db_name))

    report.checked_tables = len(model_tables)

    # 2) 列级：缺失 / 多余 / 类型差异
    for name_lower, tbl in sorted(model_tables.items()):
        if name_lower not in db_tables:
            continue  # 表缺失已在上面报 FAIL，跳过列级
        db_col_map = db_cols.get(name_lower, {})
        for col_name, col in tbl.columns.items():
            col_lower = col_name.lower()
            report.checked_columns += 1
            if col_lower not in db_col_map:
                report.diffs.append(
                    Diff("FAIL", "missing_column", f"{name_lower}.{col_name}")
                )
                continue
            if check_types and col.type is not None:
                db_ctype = db_col_map[col_lower]
                if _norm_type(db_ctype) != _model_type_compile(col.type):
                    report.diffs.append(
                        Diff(
                            "WARN",
                            "type_mismatch",
                            f"{name_lower}.{col_name}",
                            _len_detail(db_ctype, col.type),
                        )
                    )
        model_col_names = {c.name.lower() for c in tbl.columns}
        if report_extra:
            for db_col in sorted(db_col_map):
                if db_col not in model_col_names:
                    report.diffs.append(Diff("WARN", "extra_column", f"{name_lower}.{db_col}"))

    # 3) 索引级：模型有而 DB 完全没有
    if check_indexes:
        for name_lower, tbl in sorted(model_tables.items()):
            if name_lower not in db_tables:
                continue
            db_idx_set = db_idx.get(name_lower, set())
            model_idx_names = {i.name.lower() for i in tbl.indexes if i.name}
            model_idx_names |= {uc.name.lower() for uc in tbl.unique_constraints if uc.name}
            for idx_name in sorted(model_idx_names):
                if idx_name not in {x.lower() for x in db_idx_set}:
                    report.diffs.append(
                        Diff("WARN", "missing_index", f"{name_lower}.{idx_name}")
                    )

    conn.close()
    return report


def print_report(report: Report, url: str, label: str) -> None:
    """打印报告并返回退出码。"""
    print(f"\n=== 模型↔迁移一致性检查（{label}）===")
    print(f"目标库: {url}")
    print(f"检查: {report.checked_tables} 表 / {report.checked_columns} 列")
    if not report.diffs:
        print("结果: ✅ 一致（无差异）")
        return
    for d in report.diffs:
        print(d.line())
    print(
        f"结果: ❌ FAIL {report.fail_count()} 项 / WARN {report.warn_count()} 项"
    )


def run_init_check(
    url: str, *, check_types: bool, check_indexes: bool, report_extra: bool
) -> Report:
    """模式 B：建临时干净库 → alembic upgrade head → 对比 → 清理。

    建库账号复用 ``--db-url`` 的账号（本项目 compose 默认 unisense 用户已具备
    ``CREATE/DROP ON *.*`` 全局权限，可建库并访问新库；若生产账号无建库权限，
    请把 ``--db-url`` 指向具备该权限的账号，如 root）。
    """
    p = _conn_params(url)
    p["database"] = ""
    conn = pymysql.connect(**p)
    tmp_db = f"unisense_init_check_{int(time.time())}"
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE `{tmp_db}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        print(f"已创建临时干净库: {tmp_db}")

        # 注入 UNISENSE_DB_URL 指向临时库，跑 alembic upgrade head。
        # 注意：str(URL) 会隐藏密码为 ***，必须 render_as_string(hide_password=False)。
        u = make_url(url)
        tmp_url = u.set(database=tmp_db)
        env = dict(os.environ)
        env["UNISENSE_DB_URL"] = tmp_url.render_as_string(hide_password=False)
        proc = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=str(_BACKEND),
            env=env,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            print("alembic upgrade head 失败：")
            print(proc.stdout[-3000:] if proc.stdout else "")
            print(proc.stderr[-3000:] if proc.stderr else "")
            raise SystemExit(2)

        report = run_check(
            tmp_url.render_as_string(hide_password=False),
            check_types=check_types,
            check_indexes=check_indexes,
            report_extra=report_extra,
        )
        return report
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute(f"DROP DATABASE IF EXISTS `{tmp_db}`")
            print(f"已清理临时库: {tmp_db}")
        except Exception:  # noqa: BLE001 - 清理失败不应掩盖主结果
            print(f"警告: 临时库 {tmp_db} 清理失败（请手工删除）")
        conn.close()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description="模型↔迁移一致性检查（发布前门禁）")
    parser.add_argument(
        "--db-url", default=settings.db_url, help="目标库 DSN（缺省 settings.db_url）"
    )
    parser.add_argument(
        "--init-check",
        action="store_true",
        help="模式 B：建临时干净库跑 alembic upgrade head 后对比（模拟生产初始化）",
    )
    parser.add_argument(
        "--check-types", action="store_true", help="比较列类型差异（默认仅缺表/缺列）"
    )
    parser.add_argument("--check-indexes", action="store_true", help="检查模型索引是否缺失")
    parser.add_argument(
        "--report-extra",
        action="store_true",
        help="也报告 DB 有而模型无的表/列（运行时自建表）",
    )
    parser.add_argument("--fail-on-warn", action="store_true", help="WARN 也返回非零（严格模式）")
    args = parser.parse_args()

    try:
        if args.init_check:
            report = run_init_check(
                args.db_url,
                check_types=args.check_types,
                check_indexes=args.check_indexes,
                report_extra=args.report_extra,
            )
            print_report(report, args.db_url, "干净库初始化模拟（--init-check）")
        else:
            report = run_check(
                args.db_url,
                check_types=args.check_types,
                check_indexes=args.check_indexes,
                report_extra=args.report_extra,
            )
            print_report(report, args.db_url, "现有库直查")
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - CLI 入口统一兜底
        print(f"执行错误: {exc}")
        return 2

    if report.fail_count() > 0 or (args.fail_on_warn and report.warn_count() > 0):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
