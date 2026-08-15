import { describe, it, expect } from "vitest";
import {
  TIME_SEMANTICS_LABEL,
  FRESHNESS_LABEL,
  METRIC_STATUS_LABEL,
  NOTIFY_STATUS_LABEL,
  QUALITY_SEVERITY_LABEL,
  RECONCILIATION_STATUS_LABEL,
} from "../utils/enums";

// 这些契约锁定「前端映射键 必须覆盖 后端权威枚举值」，防止跨服务漂移导致显示原始英文。
// 后端权威值域（backend/app/models/）：time_semantics 6 值 / freshness 4 值 / MetricStateEnum 6 态 /
// QualitySeverity P0/P1/P2 / NotifyStatus PENDING/SENT/FAILED。

describe("enums 映射契约（对齐后端权威值域，防跨服务漂移）", () => {
  it("TIME_SEMANTICS_LABEL 覆盖后端全部 6 值（含 MOM/YOY）", () => {
    for (const v of ["PERIOD", "YTD", "TTM", "AVG", "MOM", "YOY"]) {
      expect(TIME_SEMANTICS_LABEL[v], `time_semantics=${v}`).toBeTruthy();
    }
  });

  it("FRESHNESS_LABEL 覆盖后端 4 值（含 T0），且不含后端不存在的 T2/T3 幽灵键", () => {
    for (const v of ["REALTIME", "T0", "T1", "HOURLY"]) {
      expect(FRESHNESS_LABEL[v], `freshness=${v}`).toBeTruthy();
    }
    expect(FRESHNESS_LABEL.T2).toBeUndefined();
    expect(FRESHNESS_LABEL.T3).toBeUndefined();
  });

  it("METRIC_STATUS_LABEL 覆盖后端 MetricStateEnum 全部 6 态（含 DATA_SOURCE_DROPPED）", () => {
    for (const v of ["DRAFT", "REVIEW", "PUBLISHED", "EXPERIMENTAL", "DEPRECATED", "DATA_SOURCE_DROPPED"]) {
      expect(METRIC_STATUS_LABEL[v], `metric state=${v}`).toBeTruthy();
    }
  });

  it("NOTIFY_STATUS_LABEL 覆盖后端 NotifyStatus 3 态，不含幽灵 READ（已读是 read_at 字段非状态）", () => {
    for (const v of ["PENDING", "SENT", "FAILED"]) {
      expect(NOTIFY_STATUS_LABEL[v], `notify status=${v}`).toBeTruthy();
    }
    expect(NOTIFY_STATUS_LABEL.READ).toBeUndefined();
  });

  it("QUALITY_SEVERITY_LABEL 覆盖后端 QualitySeverity P0/P1/P2", () => {
    for (const v of ["P0", "P1", "P2"]) {
      expect(QUALITY_SEVERITY_LABEL[v], `quality severity=${v}`).toBeTruthy();
    }
  });

  it("RECONCILIATION_STATUS_LABEL 覆盖质量对账 OK 与维度对账 APPROVED（跨两服务值域）", () => {
    for (const v of ["OK", "WARN", "ALERT", "CONFIRMED", "PENDING", "APPROVED", "REJECTED"]) {
      expect(RECONCILIATION_STATUS_LABEL[v], `reconciliation=${v}`).toBeTruthy();
    }
  });
});
