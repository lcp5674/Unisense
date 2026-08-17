import { useEffect, useState } from "react";
import { Collapse, Empty, Spin, Tag, Timeline, Typography } from "antd";
import { listAudit } from "../../api";
import type { AuditEntry } from "../../types";
import {
  AUDIT_FIELD_LABEL,
  auditActionLabel,
  auditValueText,
  entityTypeLabel,
} from "../../utils/auditI18n";
import { formatCnTime, timeAgoCn } from "../../utils/timeCn";

// 动作 → 时间线节点/标签颜色（沿用指标审计配色，补齐采集/连接/批量/描述动作）
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
  PII_SECONDARY_VALIDATION: "magenta",
  PII_ANONYMIZED: "magenta",
  CLASSIFICATION_RESCAN: "magenta",
  CONFIRM_VERSION: "geekblue",
  REJECT_VERSION: "red",
  EXTEND_VERSION: "lime",
  // 数据源 / 采集目录三模块（TD §12.1）
  REGISTER: "green",
  REFRESH: "green",
  BULK_DEPRECATE: "red",
  COLLECT: "processing",
  COLLECT_NOW: "processing",
  COLLECT_ASYNC: "processing",
  COLLECT_SCHEDULE: "geekblue",
  TEST_CONNECTION: "cyan",
  CHECK_CONNECTION: "cyan",
  BATCH_PROBE: "cyan",
  BATCH_ENABLE: "orange",
  BATCH_DISABLE: "orange",
  BATCH_DELETE: "orange",
  BATCH_SCHEDULE: "orange",
  INFER_DESCRIPTION: "lime",
  INFER_DESCRIPTIONS_BATCH: "lime",
  UPDATE_DESCRIPTION: "lime",
  UPDATE_TABLE_DESCRIPTION: "lime",
  INFER_TABLE_DESCRIPTION: "lime",
};

// 动作 → 业务分类 Tag（替代原始英文 action，避免技术术语直面用户）
const ACTION_CATEGORY: Record<string, string> = {
  CREATE: "创建",
  UPDATE: "更新",
  DELETE: "删除",
  REGISTER: "登记",
  REFRESH: "刷新",
  PUBLISH: "发布",
  PROMOTE: "发布",
  ROLLBACK: "回滚",
  DEPRECATE: "废弃",
  BULK_DEPRECATE: "废弃",
  SUBMIT: "审核",
  APPROVE: "审核",
  REJECT: "审核",
  CONFIRM_VERSION: "审核",
  REJECT_VERSION: "审核",
  EXTEND_VERSION: "审核",
  PII_REVIEW: "合规",
  PII_SECONDARY_VALIDATION: "合规",
  PII_ANONYMIZED: "合规",
  CLASSIFICATION_RESCAN: "合规",
  COLLECT: "采集",
  COLLECT_NOW: "采集",
  COLLECT_ASYNC: "采集",
  COLLECT_SCHEDULE: "调度",
  TEST_CONNECTION: "连接",
  CHECK_CONNECTION: "连接",
  BATCH_PROBE: "连接",
  BATCH_ENABLE: "启停",
  BATCH_DISABLE: "启停",
  BATCH_DELETE: "批量删除",
  BATCH_SCHEDULE: "批量调度",
  INFER_DESCRIPTION: "描述",
  INFER_DESCRIPTIONS_BATCH: "描述",
  UPDATE_DESCRIPTION: "描述",
  UPDATE_TABLE_DESCRIPTION: "描述",
  INFER_TABLE_DESCRIPTION: "描述",
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

export interface AuditTimelineProps {
  /** 实体类型（如 metric_definition / data_source），与后端 AuditLog.entity_type 对齐 */
  entityType: string;
  /** 实体 ID（如指标编码 / 数据源 source_id） */
  entityId: string;
  /** 展示用实体中文名，缺省取 entityTypeLabel(entityType) */
  entityLabel?: string;
  /** 无记录时的空态文案 */
  emptyText?: string;
}

/** 通用操作审计时间线（指标/数据源等实体复用，TD §15.4）。 */
export function AuditTimeline({ entityType, entityId, entityLabel, emptyText }: AuditTimelineProps) {
  const [items, setItems] = useState<AuditEntry[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    listAudit({ entity_type: entityType, entity_id: entityId, page_size: 30 })
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
  }, [entityType, entityId]);

  if (loading) {
    return (
      <div style={{ textAlign: "center", padding: 24 }}>
        <Spin />
      </div>
    );
  }

  if (!items.length) {
    return <Empty description={emptyText ?? "暂无审计记录"} />;
  }

  return (
    <Timeline
      items={items.map((it) => ({
        key: it.id,
        color: ACTION_COLOR[it.action] ?? "gray",
        children: (
          <div>
            <Typography.Text strong>
              {/* 优先展示后端 enrich 的业务中文描述；缺省回退 auditI18n 中文动作 */}
              {it.action_desc ?? auditActionLabel(it.action)}
              <Tag color={ACTION_COLOR[it.action] ?? "default"} style={{ marginLeft: 8 }}>
                {ACTION_CATEGORY[it.action] ?? auditActionLabel(it.action)}
              </Tag>
            </Typography.Text>
            <div className="muted" style={{ fontSize: 12 }}>
              <span>{entityLabel ?? entityTypeLabel(it.entity_type)}</span>
              <span> · </span>
              <span className="mono">{it.entity_id}</span>
              <span> · 操作人 {it.actor_display ?? `#${it.actor_id}`}</span>
              <span> · {timeAgoCn(it.created_at)}（{formatCnTime(it.created_at)}）</span>
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
                    children: <DetailSummary detail={it.detail_json} />,
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
