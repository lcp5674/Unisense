import { useEffect, useState } from "react";
import { Card, Table, Tag, Input, Select, Button, Space, message } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import { listAudit, UnisenseApiError } from "../api";
import type { AuditEntry } from "../types";

export function AuditLog() {
  const [items, setItems] = useState<AuditEntry[]>([]);
  const [page, setPage] = useState(1);
  const [entityType, setEntityType] = useState("");
  const [actorId, setActorId] = useState("");
  const [piiOnly, setPiiOnly] = useState<boolean | undefined>(undefined);
  const [loading, setLoading] = useState(false);

  async function load() {
    setLoading(true);
    try {
      const res = await listAudit({
        entity_type: entityType || undefined,
        actor_id: actorId ? Number(actorId) : undefined,
        pii_access: piiOnly,
        page,
        page_size: 20,
      });
      setItems(res.items);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message} (${err.code})` : "加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, entityType, piiOnly]);

  const columns = [
    { title: "ID", dataIndex: "id", key: "id", width: 70 },
    { title: "操作者", dataIndex: "actor_id", key: "actor", width: 140, render: (_: number, r: AuditEntry) => <span>{r.actor_display ?? `#${r.actor_id}`}</span> },
    { title: "动作", dataIndex: "action", key: "action", ellipsis: true, render: (v: string, r: AuditEntry) => (
      <span>
        <span>{r.action_desc ?? v}</span>
        <Tag style={{ marginLeft: 6 }}>{v}</Tag>
      </span>
    ) },
    { title: "实体类型", dataIndex: "entity_type", key: "entityType", width: 150, render: (v: string) => <Tag>{v}</Tag> },
    { title: "实体 ID", dataIndex: "entity_id", key: "entityId", ellipsis: true, render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{v}</span> },
    {
      title: "详情",
      dataIndex: "detail_json",
      key: "detail",
      ellipsis: true,
      render: (v: Record<string, unknown> | null) =>
        v && Object.keys(v).length > 0 ? (
          <span className="mono" style={{ fontSize: 12 }}>
            {Object.entries(v)
              .filter(([, val]) => val !== null && val !== undefined)
              .map(([k, val]) => `${k}:${typeof val === "object" ? JSON.stringify(val) : String(val)}`)
              .join(" · ")}
          </span>
        ) : (
          <span className="muted">—</span>
        ),
    },
    { title: "IP", dataIndex: "ip", key: "ip", width: 130, render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{v}</span> },
    {
      title: "PII",
      dataIndex: "pii_access",
      key: "pii",
      width: 80,
      render: (v: boolean) => (v ? <Tag color="red">PII</Tag> : <Tag>否</Tag>),
    },
    { title: "时间", dataIndex: "created_at", key: "created", width: 170 },
  ];

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="page-kicker">Governance / Audit Trail</div>
          <h2>审计日志</h2>
          <p>全量操作留痕——写操作、PII 访问与治理动作均可追溯。</p>
        </div>
      </div>

      <Card
        extra={
          <Button icon={<ReloadOutlined />} onClick={load} loading={loading}>
            刷新
          </Button>
        }
      >
        <Space style={{ marginBottom: 12 }} wrap>
          <Select
            allowClear
            placeholder="全部实体类型"
            style={{ width: 180 }}
            value={entityType || undefined}
            onChange={(v) => { setEntityType(v || ""); setPage(1); }}
            options={["metric_definition", "metric_template", "metric_version", "conflict", "lineage_edge", "grant", "term", "dimension", "quality_rule", "notification", "data_source", "db_catalog"].map((v) => ({ value: v, label: v }))}
          />
          <Input
            placeholder="操作者 ID"
            className="mono"
            style={{ width: 140 }}
            value={actorId}
            onChange={(e) => setActorId(e.target.value)}
            onPressEnter={() => { setPage(1); load(); }}
          />
          <Select
            allowClear
            placeholder="PII 访问"
            style={{ width: 140 }}
            value={piiOnly}
            onChange={(v) => { setPiiOnly(v); setPage(1); }}
            options={[{ value: true, label: "仅 PII 访问" }, { value: false, label: "非 PII" }]}
          />
        </Space>

        <Table
          dataSource={items}
          columns={columns}
          rowKey="id"
          loading={loading}
          pagination={{ current: page, pageSize: 20, onChange: setPage, showTotal: (t) => `共 ${t} 条（后端返回近似值）` }}
          locale={{ emptyText: "暂无审计记录" }}
          size="small"
        />
      </Card>
    </div>
  );
}
