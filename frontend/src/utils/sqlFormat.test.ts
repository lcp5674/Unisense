import { describe, it, expect } from "vitest";
import { formatSql } from "./sqlFormat";

describe("formatSql SQL 美化", () => {
  it("空输入原样返回", () => {
    expect(formatSql("")).toBe("");
    expect(formatSql("  ")).toBe("");
    expect(formatSql(null as unknown as string)).toBe("");
  });

  it("单列简单 SELECT：SELECT 独占行、FROM 换行", () => {
    expect(formatSql("SELECT amount FROM ods_order")).toBe(
      ["SELECT", "  amount", "FROM ods_order"].join("\n"),
    );
  });

  it("多列 SELECT：每列逐行缩进", () => {
    const out = formatSql("SELECT order_amount AS gmv, channel, dt FROM ods_e2e_order");
    expect(out).toBe(
      ["SELECT", "  order_amount AS gmv,", "  channel,", "  dt", "FROM ods_e2e_order"].join("\n"),
    );
  });

  it("WHERE 条件：AND/OR 换行缩进", () => {
    const out = formatSql("SELECT a, b FROM t WHERE is_active = 1 AND dt > '2026-01-01' OR flag = 0");
    expect(out).toBe(
      [
        "SELECT",
        "  a,",
        "  b",
        "FROM t",
        "WHERE is_active = 1",
        "  AND dt > '2026-01-01'",
        "  OR flag = 0",
      ].join("\n"),
    );
  });

  it("GROUP BY / ORDER BY / LIMIT 独立成行", () => {
    const out = formatSql(
      "SELECT channel, COUNT(*) AS cnt FROM ods_order GROUP BY channel ORDER BY cnt DESC LIMIT 10",
    );
    expect(out).toBe(
      [
        "SELECT",
        "  channel,",
        "  COUNT(*) AS cnt",
        "FROM ods_order",
        "GROUP BY channel",
        "ORDER BY cnt DESC",
        "LIMIT 10",
      ].join("\n"),
    );
  });

  it("COUNT(DISTINCT ...) 函数调用括号保持同行", () => {
    const out = formatSql("SELECT COUNT(DISTINCT user_id) AS u, dt FROM ods_user GROUP BY dt");
    expect(out).toBe(
      [
        "SELECT",
        "  COUNT(DISTINCT user_id) AS u,",
        "  dt",
        "FROM ods_user",
        "GROUP BY dt",
      ].join("\n"),
    );
  });

  it("函数内部逗号不换行（COUNT(a, b)）", () => {
    const out = formatSql("SELECT COUNT(a, b) FROM t");
    expect(out).toBe(["SELECT", "  COUNT(a, b)", "FROM t"].join("\n"));
  });

  it("JOIN 变体独立成行、ON 条件换行", () => {
    const out = formatSql(
      "SELECT a.id, b.name FROM ods_a a LEFT JOIN ods_b b ON a.id = b.id WHERE b.name IS NOT NULL",
    );
    expect(out).toBe(
      [
        "SELECT",
        "  a.id,",
        "  b.name",
        "FROM ods_a a",
        "LEFT JOIN ods_b b",
        "ON a.id = b.id",
        "WHERE b.name IS NOT NULL",
      ].join("\n"),
    );
  });

  it("字符串字面量内逗号/关键字不误伤", () => {
    const out = formatSql("SELECT a FROM t WHERE label = 'a, b AND c' AND x = 1");
    expect(out).toBe(
      [
        "SELECT",
        "  a",
        "FROM t",
        "WHERE label = 'a, b AND c'",
        "  AND x = 1",
      ].join("\n"),
    );
  });

  it("子查询括号缩进", () => {
    const out = formatSql("SELECT id FROM (SELECT id, name FROM t WHERE x = 1) sub WHERE y = 2");
    expect(out).toBe(
      [
        "SELECT",
        "  id",
        "FROM (",
        "  SELECT",
        "    id,",
        "    name",
        "  FROM t",
        "  WHERE x = 1)",
        "sub",
        "WHERE y = 2",
      ].join("\n"),
    );
  });

  it("UNION 拼接换行", () => {
    const out = formatSql("SELECT a FROM t1 UNION ALL SELECT a FROM t2");
    expect(out).toBe(
      ["SELECT", "  a", "FROM t1", "UNION ALL", "SELECT", "  a", "FROM t2"].join("\n"),
    );
  });

  it("已多行 SQL 重新规范排版", () => {
    const out = formatSql("SELECT a\nFROM t\nWHERE b = 1");
    expect(out).toBe(["SELECT", "  a", "FROM t", "WHERE b = 1"].join("\n"));
  });

  it("保留原大小写与列名原样", () => {
    const out = formatSql("select Amount, dt from ODS_Order where Flag = 1");
    expect(out).toBe(
      ["select", "  Amount,", "  dt", "from ODS_Order", "where Flag = 1"].join("\n"),
    );
  });
});
