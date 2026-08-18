import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button, Card, Descriptions, Empty, Space, Spin, Tag, Tooltip, message } from "antd";
import {
  ArrowRightOutlined,
  ArrowUpOutlined,
  ArrowDownOutlined,
  SyncOutlined,
} from "@ant-design/icons";
import { usePermission } from "../../hooks/usePermission";
import {
  fetchRelatedMetrics,
  getMetric,
  getMetricHealth,
  syncMetricConsumers,
  UnisenseApiError,
} from "../../api";
import type { MetricHealth, MetricResponse, RecommendItem } from "../../types";
import { ResizableDrawer } from "../ResizableDrawer";
import { ManualEdgeModal } from "../lineage/ManualEdgeModal";
import { AGGREGATION_LABEL, DW_LAYER_LABEL, FRESHNESS_LABEL, GRANULARITY_LABEL, METRIC_TIER_LABEL } from "../../utils/enums";

const STATUS_COLOR: Record<string, string> = {
  DRAFT: "default",
  EXPERIMENTAL: "processing",
  REVIEW: "warning",
  PUBLISHED: "success",
  DEPRECATED: "error",
};

const STATUS_LABEL: Record<string, string> = {
  DRAFT: "草稿",
  EXPERIMENTAL: "实验",
  REVIEW: "审核",
  PUBLISHED: "已发布",
  DEPRECATED: "已废弃",
};

const HEALTH_LABEL: Record<string, string> = {
  EXCELLENT: "优秀",
  GOOD: "良好",
  WARNING: "警告",
  CRITICAL: "严重",
};
const HEALTH_COLOR: Record<string, string> = {
  EXCELLENT: "green",
  GOOD: "blue",
  WARNING: "orange",
  CRITICAL: "red",
};

/** 口径摘要：从 definition_json 提取表达式 / 定义 / 依赖 / 源表 / 来源字段 / ETL SQL */
function DefinitionsBlock({ def }: { def: Record<string, unknown> }) {
  const expression = typeof def.expression === "string" ? def.expression : undefined;
  const definition = typeof def.definition === "string" ? def.definition : undefined;
  const dependencies = Array.isArray(def.dependencies) ? def.dependencies.map((d) => String(d)) : [];
  const rawSource = def.source_fields ?? def.source_columns;
  const sourceFields = Array.isArray(rawSource) ? rawSource.map((s) => String(s)) : rawSource ? [String(rawSource)] : [];
  const sourceTables = Array.isArray(def.source_tables)
    ? def.source_tables.map((s) => String(s))
    : def.source_tables
      ? [String(def.source_tables)]
      : [];
  const rawEtl = def.etl_sql ?? def.sql;
  const etlSql = rawEtl == null ? "" : String(rawEtl);

  return (
    <div>
      {definition && (
        <p style={{ margin: "0 0 8px" }}>
          <span className="muted">指标定义：</span>
          {definition}
        </p>
      )}
      {expression && (
        <p style={{ margin: "0 0 8px" }}>
          <span className="muted">计算口径：</span>
          <code className="mono">{expression}</code>
        </p>
      )}
      {sourceTables.length > 0 && (
        <p style={{ margin: "0 0 8px" }}>
          <span className="muted">关联数据表：</span>
          {sourceTables.map((t) => (
            <Tag key={t} className="mono">{t}</Tag>
          ))}
        </p>
      )}
      {dependencies.length > 0 && (
        <p style={{ margin: "0 0 8px" }}>
          <span className="muted">依赖指标：</span>
          {dependencies.map((d) => (
            <Tag key={d}>{d}</Tag>
          ))}
        </p>
      )}
      {sourceFields.length > 0 && (
        <p style={{ margin: "0 0 8px" }}>
          <span className="muted">来源字段：</span>
          {sourceFields.map((s) => (
            <Tag key={s}>{s}</Tag>
          ))}
        </p>
      )}
      {etlSql && (
        <pre
          style={{
            background: "var(--paper)",
            padding: 8,
            borderRadius: 4,
            margin: "0 0 8px",
            fontSize: 12,
            overflow: "auto",
            maxHeight: 200,
          }}
        >
          {etlSql}
        </pre>
      )}
    </div>
  );
}

interface MetricDetailDrawerProps {
  open: boolean;
  /** 指标编码（metric 节点点击时传入，如 "sales_e2e_gmv_day"） */
  metricCode: string | null;
  onClose: () => void;
}

/**
 * 通用「指标详情」侧边栏（血缘图谱等图谱场景点击指标节点时展示）。
 * 在侧边栏内展示核心生产信息（状态/健康度/基础信息/口径明细/关联指标），
 * 提供「前往完整详情」按钮作为补充入口，默认不再跳转页面，避免打断图谱浏览。
 */
export function MetricDetailDrawer({ open, metricCode, onClose }: MetricDetailDrawerProps) {
  const navigate = useNavigate();
  const canLineageWrite = usePermission().can("lineage:write");
  const [metric, setMetric] = useState<MetricResponse | null>(null);
  const [health, setHealth] = useState<MetricHealth | null>(null);
  const [related, setRelated] = useState<RecommendItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 手动添加上下游 / 同步消费方（人工治理 + 消费方血缘）
  const [manualOpen, setManualOpen] = useState(false);
  const [manualDirection, setManualDirection] = useState<"upstream" | "downstream">("downstream");
  const [syncing, setSyncing] = useState(false);

  useEffect(() => {
    if (!open || !metricCode) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setMetric(null);
    setHealth(null);
    setRelated([]);
    (async () => {
      try {
        const [m, h, rel] = await Promise.all([
          getMetric(metricCode),
          getMetricHealth(metricCode).catch(() => null),
          fetchRelatedMetrics(metricCode).catch(() => [] as RecommendItem[]),
        ]);
        if (cancelled) return;
        setMetric(m);
        setHealth(h);
        setRelated(rel);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载指标详情失败");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open, metricCode]);

  const def = metric?.definition_json ?? {};

  return (
    <ResizableDrawer
      title={metric ? `指标详情：${metric.name}` : "指标详情"}
      open={open}
      onClose={onClose}
      storageKey="unisense.drawer.lineage-metric.width"
      defaultWidth={760}
      minWidth={560}
    >
      {loading ? (
        <div style={{ textAlign: "center", padding: "48px 0" }}>
          <Spin />
        </div>
      ) : error ? (
        <Empty description={error} />
      ) : metric ? (
        <div>
          <Space wrap style={{ marginBottom: 12 }}>
            <span className="mono" style={{ fontWeight: 600 }}>{metric.metric_code}</span>
            <Tag color={STATUS_COLOR[metric.status] ?? "default"}>{STATUS_LABEL[metric.status] ?? metric.status}</Tag>
            <Tag color={metric.metric_tier ? undefined : "default"}>{METRIC_TIER_LABEL[metric.metric_tier] ?? metric.metric_tier}</Tag>
            {metric.pii_flag && <Tag color="red">PII</Tag>}
            {metric.pending_version && <Tag color="purple">版本待确认</Tag>}
            {metric.pending_conflict && <Tag color="orange">待仲裁</Tag>}
          </Space>

          {health && (
            <Card size="small" title="健康度" style={{ marginBottom: 16 }}>
              <Space>
                <Tag color={HEALTH_COLOR[health.level] ?? "default"}>{HEALTH_LABEL[health.level] ?? health.level}</Tag>
                <span style={{ fontSize: 20, fontWeight: 600 }}>{health.score}</span>
                <span className="muted">分</span>
                <span className="muted" style={{ marginLeft: 8 }}>
                  计算于 {health.calculated_at}
                </span>
              </Space>
            </Card>
          )}

          <Descriptions column={2} bordered size="small" style={{ marginBottom: 16 }}>
            <Descriptions.Item label="指标名称">{metric.name}</Descriptions.Item>
            <Descriptions.Item label="所属域">{metric.domain}</Descriptions.Item>
            <Descriptions.Item label="类型">{metric.type}</Descriptions.Item>
            <Descriptions.Item label="聚合方式">{AGGREGATION_LABEL[metric.aggregation] ?? metric.aggregation}</Descriptions.Item>
            <Descriptions.Item label="粒度">{metric.granularity ? (GRANULARITY_LABEL[metric.granularity] ?? metric.granularity) : "—"}</Descriptions.Item>
            <Descriptions.Item label="单位">{metric.unit}</Descriptions.Item>
            <Descriptions.Item label="数仓层">{DW_LAYER_LABEL[metric.dw_layer] ?? metric.dw_layer}</Descriptions.Item>
            <Descriptions.Item label="新鲜度">{FRESHNESS_LABEL[metric.freshness] ?? metric.freshness}</Descriptions.Item>
            <Descriptions.Item label="时间语义">{metric.time_semantics}</Descriptions.Item>
            <Descriptions.Item label="版本">{metric.version}</Descriptions.Item>
            <Descriptions.Item label="可加性">{metric.additivity}</Descriptions.Item>
            <Descriptions.Item label="Owner ID">{metric.owner_id}</Descriptions.Item>
            <Descriptions.Item label="创建时间">{metric.created_at}</Descriptions.Item>
            <Descriptions.Item label="更新时间">{metric.updated_at}</Descriptions.Item>
          </Descriptions>

          <Card size="small" title="口径明细" style={{ marginBottom: 16 }}>
            <DefinitionsBlock def={def} />
          </Card>

          {related.length > 0 && (
            <Card size="small" title="关联指标" style={{ marginBottom: 16 }}>
              {related.map((r) => (
                <Tag
                  key={r.metric_id}
                  className="mono"
                  style={{ marginBottom: 4, cursor: "pointer" }}
                  onClick={() => navigate(`/detail/${encodeURIComponent(r.metric_id)}`)}
                >
                  {r.metric_id}
                </Tag>
              ))}
            </Card>
          )}

          <Button
            type="primary"
            icon={<ArrowRightOutlined />}
            onClick={() => navigate(`/detail/${encodeURIComponent(metric.metric_code)}`)}
            style={{ marginTop: 4 }}
          >
            前往完整详情
          </Button>
          <Space style={{ marginTop: 12, display: "flex", flexWrap: "wrap" }}>
            <Tooltip title={canLineageWrite ? undefined : "无 lineage:write 权限，血缘边登记不可用"}>
              <Button
                icon={<ArrowUpOutlined />}
                disabled={!canLineageWrite}
                onClick={() => {
                  setManualDirection("upstream");
                  setManualOpen(true);
                }}
              >
                添加上游
              </Button>
            </Tooltip>
            <Tooltip title={canLineageWrite ? undefined : "无 lineage:write 权限，血缘边登记不可用"}>
              <Button
                icon={<ArrowDownOutlined />}
                disabled={!canLineageWrite}
                onClick={() => {
                  setManualDirection("downstream");
                  setManualOpen(true);
                }}
              >
                添加下游
              </Button>
            </Tooltip>
            <Tooltip title={canLineageWrite ? undefined : "无 lineage:write 权限，同步消费方不可用"}>
              <Button
                icon={<SyncOutlined />}
                loading={syncing}
                disabled={!canLineageWrite}
                onClick={async () => {
                setSyncing(true);
                try {
                  const res = await syncMetricConsumers(metric.metric_code);
                  message.success(
                    res.registered_edges > 0
                      ? `已同步 ${res.registered_edges} 条消费方血缘边`
                      : "暂无可同步的消费方（接入方白名单未配置该指标）",
                  );
                } catch (err) {
                  message.error(
                    err instanceof UnisenseApiError
                      ? `${err.message}（${err.codeZh}）`
                      : "同步消费方失败",
                  );
                } finally {
                  setSyncing(false);
                }
              }}
            >
              同步消费方
            </Button>
            </Tooltip>
          </Space>
          <ManualEdgeModal
            open={manualOpen}
            onClose={() => setManualOpen(false)}
            baseNode={`metric:${metric.metric_code}`}
            baseLabel={metric.name}
            defaultDirection={manualDirection}
          />
        </div>
      ) : null}
    </ResizableDrawer>
  );
}
