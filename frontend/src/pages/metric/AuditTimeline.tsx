import { useEffect, useState } from "react";
import { Collapse, Empty, Spin, Tag, Timeline, Typography } from "antd";
import { listAudit } from "../../api";
import type { AuditEntry } from "../../types";
import { AUDIT_FIELD_LABEL, auditValueText, entityTypeLabel } from "../../utils/auditI18n";

const ACTION_COLOR: Record<string, string> = {
  CREATE: "green",
  UPDATE: "blue",
  PUBLISH: "purple",
  SUBMIT: "gold",
  APPROVE: "cyan",
  REJECT: "red",
  DEPRECATE: "default",
  DELETE: "red",
  PROMOTE: "green",
  ROLLBACK: "orange",
  EMERGENCY_PUBLISH: "volcano",
  PII_REVIEW: "magenta",
  CONFIRM_VERSION: "geekblue",
  REJECT_VERSION: "red",
  EXTEND_VERSION: "lime",
};

// 将 detail_json 渲染为「中文字段名: 值」可读摘要，避免用户直面原始 JSON
function DetailSummary({ detail }: { detail: Record<string, unknown> }) {
  const entries = Object.entries(detail).filter(([, v]) => v !== null && v !== undefined);
  if (!entries.length) return null;
  return (
    <div style={{ margin: "4px 0 0" }}>
      {entries.map(([k, v]) => (
        <span key={k} className="muted" style={{ fontSize: 12, marginRight: 12, display: "inline-block" }}>
          <span style={{ color: "var(--text)" }}>{AUDIT_FIELD_LABEL[k] ?? k}:</span>{" "}
          <span className="mono">{auditValueText(v)}</span>
        </span>
      ))}
    </div>
  );
}

export function AuditTimeline({ metricCode }: { metricCode: string }) {
  const [items, setItems] = useState<AuditEntry[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    listAudit({ entity_id: metricCode, page_size: 30 })
      .then((res) => {
        if (alive) setItems(res.items);
      })
      .catch(() => {
        if (alive) setItems([]);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [metricCode]);

  if (loading) {
    return (
      <div style={{ textAlign: "center", padding: 24 }}>
        <Spin />
      </div>
    );
  }

  if (!items.length) {
    return <Empty description="暂无该指标的变更审计记录" />;
  }

  return (
    <Timeline
      items={items.map((it) => ({
        key: it.id,
        color: ACTION_COLOR[it.action] ?? "gray",
        children: (
          <div>
            <Typography.Text strong>
              {/* 优先展示后端 enrich 的中文描述；缺省回退英文 action */}
              {it.action_desc ?? it.action}
              <Tag color={ACTION_COLOR[it.action] ?? "default"} style={{ marginLeft: 8 }}>
                {it.action}
              </Tag>
            </Typography.Text>
            <div className="muted" style={{ fontSize: 12 }}>
              <span>{entityTypeLabel(it.entity_type)}</span>
              <span> · </span>
              <span className="mono">{it.entity_id}</span>
              <span> · 操作人 {it.actor_display ?? `#${it.actor_id}`}</span>
              <span> · {it.created_at}</span>
              {it.trace_id && <span> · trace <span className="mono">{it.trace_id.slice(0, 8)}</span></span>}
            </div>
            {it.detail_json && Object.keys(it.detail_json).length > 0 && (
              <Collapse
                ghost
                size="small"
                style={{ marginTop: 4 }}
                items={[
                  {
                    key: "detail",
                    label: "查看详情",
                    children: (
                      <DetailSummary detail={it.detail_json} />
                    ),
                  },
                ]}
              />
            )}
          </div>
        ),
      }))}
    />
  );
}
