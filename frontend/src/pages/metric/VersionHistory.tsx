import { useState } from "react";
import { Button, Descriptions, Input, Modal, Table, Tag, message } from "antd";
import {
  confirmMetricVersion,
  extendMetricVersion,
  rejectMetricVersion,
} from "../../api";
import type { MetricVersionResponse } from "../../types";

const VERSION_STATUS_META: Record<string, { color: string; label: string }> = {
  DRAFT: { color: "default", label: "草稿" },
  PENDING_CONFIRMATION: { color: "processing", label: "待确认" },
  PUBLISHED: { color: "success", label: "已发布" },
  CURRENT: { color: "green", label: "生效中" },
  ARCHIVED: { color: "default", label: "已归档" },
};

function VersionDefinition({ record }: { record: MetricVersionResponse }) {
  return (
    <div style={{ padding: "8px 0" }}>
      <Descriptions column={1} size="small" bordered>
        <Descriptions.Item label="版本">
          v{record.version} · <Tag color={VERSION_STATUS_META[record.status]?.color}>{VERSION_STATUS_META[record.status]?.label ?? record.status}</Tag>
        </Descriptions.Item>
        <Descriptions.Item label="变更类型">{record.change_type}</Descriptions.Item>
        <Descriptions.Item label="变更说明">{record.change_reason || <span className="muted">—</span>}</Descriptions.Item>
        <Descriptions.Item label="口径定义">
          <pre style={{ background: "var(--paper)", padding: 8, borderRadius: 4, margin: 0, fontSize: 12, overflow: "auto" }}>
            {JSON.stringify(record.definition_json, null, 2)}
          </pre>
        </Descriptions.Item>
        {record.diff_json && (
          <Descriptions.Item label="差异 (vs 上一版本)">
            <pre style={{ background: "var(--paper)", padding: 8, borderRadius: 4, margin: 0, fontSize: 12, overflow: "auto" }}>
              {JSON.stringify(record.diff_json, null, 2)}
            </pre>
          </Descriptions.Item>
        )}
      </Descriptions>
    </div>
  );
}

export function VersionHistory({
  metricCode,
  versions,
  onChanged,
}: {
  metricCode: string;
  versions: MetricVersionResponse[];
  onChanged: () => void;
}) {
  const [rejecting, setRejecting] = useState<MetricVersionResponse | null>(null);
  const [rejectReason, setRejectReason] = useState("");
  const [busy, setBusy] = useState(false);

  async function confirm(v: MetricVersionResponse) {
    setBusy(true);
    try {
      await confirmMetricVersion(metricCode, v.version);
      message.success(`v${v.version} 已确认`);
      onChanged();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "确认失败");
    } finally {
      setBusy(false);
    }
  }

  async function extend(v: MetricVersionResponse) {
    setBusy(true);
    try {
      await extendMetricVersion(metricCode, v.version);
      message.success(`v${v.version} 确认期已延期 7 天`);
      onChanged();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "延期失败");
    } finally {
      setBusy(false);
    }
  }

  async function doReject() {
    if (!rejecting) return;
    setBusy(true);
    try {
      await rejectMetricVersion(metricCode, rejecting.version, rejectReason);
      message.success(`v${rejecting.version} 已拒绝，版本取消`);
      setRejecting(null);
      setRejectReason("");
      onChanged();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "拒绝失败");
    } finally {
      setBusy(false);
    }
  }

  const columns = [
    { title: "版本", dataIndex: "version", key: "version", width: 80, render: (v: number) => `v${v}` },
    { title: "变更类型", dataIndex: "change_type", key: "type", width: 150 },
    { title: "说明", dataIndex: "change_reason", key: "reason" },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 120,
      render: (s: string) => <Tag color={VERSION_STATUS_META[s]?.color}>{VERSION_STATUS_META[s]?.label ?? s}</Tag>,
    },
    { title: "时间", dataIndex: "created_at", key: "created", width: 170 },
    {
      title: "操作",
      key: "action",
      width: 210,
      render: (_: unknown, v: MetricVersionResponse) =>
        v.status === "PENDING_CONFIRMATION" ? (
          <>
            <Button size="small" type="primary" loading={busy} onClick={() => confirm(v)}>确认</Button>
            <Button size="small" style={{ marginLeft: 8 }} loading={busy} onClick={() => { setRejecting(v); setRejectReason(""); }}>
              拒绝
            </Button>
            <Button size="small" style={{ marginLeft: 8 }} loading={busy} onClick={() => extend(v)}>延期</Button>
          </>
        ) : null,
    },
  ];

  return (
    <>
      <Table
        dataSource={versions}
        columns={columns}
        rowKey="id"
        size="small"
        pagination={false}
        expandable={{ expandedRowRender: (r) => <VersionDefinition record={r} /> }}
        locale={{ emptyText: "暂无版本" }}
      />
      <Modal
        title={`拒绝 v${rejecting?.version ?? ""}（取消该版本，回退旧版本）`}
        open={!!rejecting}
        onOk={doReject}
        confirmLoading={busy}
        onCancel={() => setRejecting(null)}
        okText="确认拒绝"
        okButtonProps={{ danger: true }}
      >
        <Input.TextArea
          placeholder="拒绝原因（必填，至少 4 字）"
          value={rejectReason}
          onChange={(e) => setRejectReason(e.target.value)}
          rows={3}
        />
      </Modal>
    </>
  );
}
