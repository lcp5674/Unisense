import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Card, Empty, Select, Space, Table, Tag, Tooltip } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import { listDataSources, listDriftLogs } from "../api";
import type { DataSource } from "../types";
import type { DriftLogItem } from "../api";
import { formatCnTime } from "../utils/timeCn";

const CHANGE_TYPE_LABEL: Record<string, { label: string; color: string }> = {
  ADD_COLUMN: { label: "新增列", color: "green" },
  DROP_COLUMN: { label: "删除列", color: "red" },
  TYPE_CHANGE: { label: "类型变更", color: "orange" },
  SCHEMA_CHANGED: { label: "Schema 变更", color: "gold" },
  DROPPED: { label: "表已删除", color: "magenta" },
};

function changeTag(v: string) {
  const meta = CHANGE_TYPE_LABEL[v];
  return <Tag color={meta?.color ?? "default"}>{meta?.label ?? v}</Tag>;
}

export function CollectionHistory() {
  const [sources, setSources] = useState<DataSource[]>([]);
  const [sourceId, setSourceId] = useState<string>("");
  const [driftLogs, setDriftLogs] = useState<DriftLogItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [srcLoading, setSrcLoading] = useState(false);

  // 加载数据源列表
  const loadSources = useCallback(async () => {
    setSrcLoading(true);
    try {
      const res = await listDataSources({ page_size: 100 });
      setSources(res.items);
    } catch {
      /* 数据源加载失败不影响主体 */
    } finally {
      setSrcLoading(false);
    }
  }, []);

  // 加载漂移日志
  const loadDrift = useCallback(async () => {
    if (!sourceId) return;
    setLoading(true);
    try {
      const res = await listDriftLogs(sourceId, { page, page_size: 10 });
      setDriftLogs(res.items);
      setTotal(res.total);
    } catch (err) {
      setDriftLogs([]);
      setTotal(0);
    } finally {
      setLoading(false);
    }
  }, [sourceId, page]);

  useEffect(() => { loadSources(); }, [loadSources]);
  useEffect(() => { loadDrift(); }, [loadDrift]);

  const columns = [
    {
      title: "实体",
      dataIndex: "entity_name",
      key: "entity",
      ellipsis: true,
      render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{v}</span>,
    },
    {
      title: "变更类型",
      dataIndex: "change_type",
      key: "changeType",
      width: 110,
      render: (v: string) => changeTag(v),
    },
    {
      title: "变更前签名",
      dataIndex: "before_signature",
      key: "beforeSig",
      width: 130,
      render: (v: string | null) =>
        v ? (
          <Tooltip title={v}>
            <span className="mono" style={{ fontSize: 11, color: "#999" }}>{v.slice(0, 10)}…</span>
          </Tooltip>
        ) : (
          <span style={{ color: "#bbb" }}>—</span>
        ),
    },
    {
      title: "变更后签名",
      dataIndex: "after_signature",
      key: "afterSig",
      width: 130,
      render: (v: string) => (
        <Tooltip title={v}>
          <span className="mono" style={{ fontSize: 11, color: "#666" }}>{v.slice(0, 10)}…</span>
        </Tooltip>
      ),
    },
    {
      title: "检测时间",
      dataIndex: "detected_at",
      key: "detectedAt",
      width: 180,
      render: (v: string | null) =>
        v ? <span className="mono" style={{ fontSize: 12 }}>{formatCnTime(v)}</span> : <span style={{ color: "#bbb" }}>—</span>,
    },
  ];

  return (
    <Card
      title={
        <span>
          采集记录
          <span className="page-eyebrow">Collection History · Schema 变更追踪 · GB/T 36073 §6.4</span>
        </span>
      }
      extra={
        <Space>
          <Select
            allowClear
            showSearch
            placeholder="选择数据源查看采集记录"
            style={{ width: 220 }}
            value={sourceId || undefined}
            onChange={(v) => { setSourceId(v ?? ""); setPage(1); }}
            loading={srcLoading}
            filterOption={(input, opt) =>
              (opt?.label as string ?? "").toLowerCase().includes(input.toLowerCase())
            }
            options={sources.map((s) => ({ value: s.source_id, label: `${s.name} (${s.source_id})` }))}
          />
          <Button icon={<ReloadOutlined />} onClick={() => { setPage(1); loadDrift(); }} loading={loading}>
            刷新
          </Button>
        </Space>
      }
    >
      {!sourceId ? (
        <Empty description="请在上方选择数据源，查看其 Schema 变更记录（采集历史）" image={Empty.PRESENTED_IMAGE_SIMPLE} />
      ) : (
        <Table<DriftLogItem>
          rowKey={(r) => `${r.entity_name}-${r.change_type}-${r.detected_at}`}
          loading={loading}
          dataSource={driftLogs}
          columns={columns}
          size="middle"
          pagination={
            total > 10
              ? {
                  current: page,
                  pageSize: 10,
                  total,
                  onChange: (p) => setPage(p),
                  showSizeChanger: false,
                }
              : false
          }
          locale={{
            emptyText: (
              <Empty
                description={`${sourceId} 暂无 Schema 变更记录（首次采集或采集后无变更时为空）`}
                image={Empty.PRESENTED_IMAGE_SIMPLE}
              />
            ),
          }}
        />
      )}

      {/* 说明区 */}
      {sourceId && total === 0 && !loading && (
        <Alert
          type="info"
          showIcon
          style={{ marginTop: 8 }}
          message="无变更记录"
          description="该数据源自上次采集以来未发生 Schema 变更（新增列/删除列/类型变更）。若有变更，会在下一次采集后出现在此处。"
        />
      )}
    </Card>
  );
}
