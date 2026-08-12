import { Card, Progress, Tag, Tooltip } from "antd";
import type { MetricHealth } from "../../types";

// 五维权重（对齐后端 health_scorer._WEIGHTS）
const DIMS: Array<{ key: keyof Omit<MetricHealth, "metric_id" | "score" | "level" | "missing_dimensions" | "calculated_at">; label: string; hint: string }> = [
  { key: "completeness_score", label: "口径完整度", hint: "一等字段（粒度/单位/聚合/SLA…）齐全率" },
  { key: "activity_score", label: "活跃度", hint: "近 30 天是否有变更/查询" },
  { key: "quality_score", label: "质量", hint: "合规审核与 PII 状态" },
  { key: "owner_response_score", label: "Owner 响应", hint: "是否配置了备份 Owner" },
  { key: "lineage_coverage_score", label: "血缘覆盖", hint: "口径是否声明依赖与表达式" },
];

const LEVEL_META: Record<string, { label: string; color: string; pct: number }> = {
  EXCELLENT: { label: "优", color: "green", pct: 100 },
  GOOD: { label: "良", color: "blue", pct: 75 },
  WARNING: { label: "警", color: "orange", pct: 50 },
  CRITICAL: { label: "危", color: "red", pct: 25 },
};

export function HealthCard({ health }: { health: MetricHealth }) {
  const meta = LEVEL_META[health.level] ?? LEVEL_META.CRITICAL;

  return (
    <div className="gauge-grid" style={{ marginBottom: 16 }}>
      <div className="gauge-cell" data-accent={meta.pct >= 75 ? "ok" : meta.pct >= 50 ? "warn" : "danger"}>
        <div className="g-label">健康度</div>
        <div className="g-value">
          <Progress
            type="dashboard"
            percent={health.score}
            size={110}
            strokeColor={meta.pct >= 75 ? "#0E7C86" : meta.pct >= 50 ? "#E8862D" : "#D64545"}
            format={() => (
              <span style={{ fontSize: 26, fontWeight: 700 }}>{health.score}</span>
            )}
          />
        </div>
        <div className="g-sub">
          <Tag color={meta.color}>{meta.label}</Tag>
          <span className="muted">总分 / 100</span>
        </div>
      </div>

      {DIMS.map((d) => {
        const value = health[d.key] ?? 0;
        return (
          <Tooltip key={d.key} title={d.hint}>
            <div
              className="gauge-cell"
              data-accent={value >= 75 ? "ok" : value >= 50 ? "warn" : "danger"}
            >
              <div className="g-label">{d.label}</div>
              <div className="g-value small">{value}</div>
              <Progress
                percent={value}
                showInfo={false}
                size="small"
                strokeColor={value >= 75 ? "#0E7C86" : value >= 50 ? "#E8862D" : "#D64545"}
              />
            </div>
          </Tooltip>
        );
      })}

      {health.missing_dimensions?.length ? (
        <Card size="small" style={{ gridColumn: "1 / -1" }}>
          <span className="muted">数据不足：</span>
          {health.missing_dimensions.map((m) => (
            <Tag key={m} color="orange">{m}</Tag>
          ))}
        </Card>
      ) : null}
    </div>
  );
}
