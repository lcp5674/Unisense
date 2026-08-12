import { useEffect, useState } from "react";
import { Empty, Spin, Tag, Timeline, Typography } from "antd";
import { listAudit } from "../../api";
import type { AuditEntry } from "../../types";

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
              <Tag color={ACTION_COLOR[it.action] ?? "default"}>{it.action}</Tag>
              {it.entity_type}
            </Typography.Text>
            <div className="muted" style={{ fontSize: 12 }}>
              <span className="mono">{it.entity_id}</span>
              <span> · 操作人 #{it.actor_id}</span>
              <span> · {it.created_at}</span>
              {it.trace_id && <span> · trace <span className="mono">{it.trace_id.slice(0, 8)}</span></span>}
            </div>
            {it.detail_json && Object.keys(it.detail_json).length > 0 && (
              <pre style={{ background: "var(--paper)", padding: 6, borderRadius: 4, fontSize: 12, margin: "4px 0 0", overflow: "auto" }}>
                {JSON.stringify(it.detail_json)}
              </pre>
            )}
          </div>
        ),
      }))}
    />
  );
}
