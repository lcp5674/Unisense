import { useEffect, useState } from "react";
import { Card, Table, Tag, Input, Select, Button, Space, Tooltip, message } from "antd";
import { ReloadOutlined, SearchOutlined } from "@ant-design/icons";
import { listAudit, UnisenseApiError } from "../api";
import type { AuditEntry } from "../types";
import { AUDIT_FIELD_LABEL, auditValueText, entityTypeLabel, auditActionLabel } from "../utils/auditI18n";
import { formatCnTime } from "../utils/timeCn";

export function AuditLog() {
  const [items, setItems] = useState<AuditEntry[]>([]);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
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
        page_size: pageSize,
      });
      setItems(res.items);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, pageSize, entityType, piiOnly]);

  const columns = [
    { title: "编号", dataIndex: "id", key: "id", width: 70 },
    {
      title: "操作者",
      dataIndex: "actor_id",
      key: "actor",
      width: 150,
      render: (_: number, r: AuditEntry) => (
        <span className="mono" style={{ fontSize: 12 }}>{r.actor_display ?? `用户 #${r.actor_id}`}</span>
      ),
    },
    {
      title: "操作内容",
      dataIndex: "action",
      key: "action",
      render: (v: string, _r: AuditEntry) => (
        <Tooltip title={v}>
          <span>{auditActionLabel(v)}</span>
        </Tooltip>
      ),
    },
    {
      title: "操作对象",
      key: "entity",
      width: 200,
      render: (_: unknown, _r: AuditEntry) => (
        <span>
          <Tag>{entityTypeLabel(_r.entity_type)}</Tag>
          <span className="mono" style={{ fontSize: 12, marginLeft: 4 }}>
            #{String(_r.entity_id || "").replace(/^[^#]*#?/, "") || "—"}
          </span>
        </span>
      ),
    },
    {
      title: "操作详情",
      dataIndex: "detail_json",
      key: "detail",
      ellipsis: true,
      render: (v: Record<string, unknown> | null, _r: AuditEntry) =>
        v && Object.keys(v).length > 0 ? (
          <Tooltip
            title={
              <div>
                {Object.entries(v)
                  .filter(([, val]) => val !== null && val !== undefined)
                  .map(([k, val]) => (
                    <div key={k} style={{ marginBottom: 2 }}>
                      <span style={{ fontWeight: 500, marginRight: 6 }}>{AUDIT_FIELD_LABEL[k] ?? k}：</span>
                      <span>{auditValueText(val)}</span>
                    </div>
                  ))}
              </div>
            }
            overlayStyle={{ maxWidth: 400 }}
          >
            <span className="mono" style={{ fontSize: 12, color: "#888" }}>
              {Object.entries(v)
                .filter(([, val]) => val !== null && val !== undefined)
                .slice(0, 2)
                .map(([k, val]) => `${AUDIT_FIELD_LABEL[k] ?? k}:${auditValueText(val)}`)
                .join(" · ") + (Object.keys(v).length > 2 ? " …" : "")}
            </span>
          </Tooltip>
        ) : (
          <span className="muted">—</span>
        ),
    },
    {
      title: "来源地址",
      dataIndex: "ip",
      key: "ip",
      width: 130,
      render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{v || "—"}</span>,
    },
    {
      title: "敏感数据",
      dataIndex: "pii_access",
      key: "pii",
      width: 100,
      render: (v: boolean) => (v ? <Tag color="red">涉及敏感数据</Tag> : <Tag>非敏感</Tag>),
    },
    {
      title: "操作时间",
      dataIndex: "created_at",
      key: "created",
      width: 170,
      render: (v: string) => (
        <span className="mono" style={{ fontSize: 12 }}>{formatCnTime(v)}</span>
      ),
    },
  ];

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="page-kicker">合规审计 / 操作留痕</div>
          <h2>审计日志</h2>
          <p>平台所有操作记录——指标创建/发布/废弃、权限变更、PII 访问等均可追溯，满足 GB/T 35273 等保要求。</p>
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
            placeholder="全部操作对象类型"
            style={{ width: 200 }}
            value={entityType || undefined}
            onChange={(v) => { setEntityType(v || ""); setPage(1); }}
            options={["metric_definition", "metric_template", "metric_version", "conflict", "lineage_edge", "grant", "term", "dimension", "quality_rule", "notification", "data_source", "db_catalog"].map((v) => ({ value: v, label: entityTypeLabel(v) }))}
          />
          <Input
            placeholder="操作人"
            className="mono"
            style={{ width: 140 }}
            prefix={<SearchOutlined />}
            value={actorId}
            onChange={(e) => setActorId(e.target.value)}
            onPressEnter={() => { setPage(1); load(); }}
          />
          <Select
            allowClear
            placeholder="敏感数据筛选"
            style={{ width: 160 }}
            value={piiOnly}
            onChange={(v) => { setPiiOnly(v); setPage(1); }}
            options={[{ value: true, label: "仅涉及敏感数据" }, { value: false, label: "仅非敏感" }]}
          />
        </Space>

        <Table
          dataSource={items}
          columns={columns}
          rowKey="id"
          loading={loading}
          pagination={{ current: page, pageSize, showSizeChanger: true, pageSizeOptions: [10, 20, 50, 100], onChange: (p, ps) => { setPage(p); setPageSize(ps); }, showTotal: (t) => `共 ${t} 条（后端返回近似值）` }}
          locale={{ emptyText: "暂无审计记录" }}
          size="small"
        />
      </Card>
    </div>
  );
}
