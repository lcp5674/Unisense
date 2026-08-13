import { useEffect, useState } from "react";
import { Card, Table, Tag, Button, Modal, Form, Input, Select, message, Space, Alert, Tooltip } from "antd";
import { PlusOutlined, ReloadOutlined, DeleteOutlined } from "@ant-design/icons";
import { listCatalogs, registerCatalog, bulkDeprecateCatalogs, UnisenseApiError } from "../api";
import type { DBCatalog } from "../types";
import { enumLabel, ENTITY_TYPE_LABEL } from "../utils/enums";

const SENSITIVITY_LABEL: Record<string, string> = {
  PUBLIC: "公开",
  INTERNAL: "内部",
  CONFIDENTIAL: "机密",
  PII: "PII",
  NEEDS_REVIEW: "待复核",
  UNKNOWN: "未知",
};
const SENSITIVITY_COLOR: Record<string, string> = {
  PUBLIC: "default",
  INTERNAL: "blue",
  CONFIDENTIAL: "orange",
  PII: "red",
  NEEDS_REVIEW: "gold",
  UNKNOWN: "default",
};

export function Catalogs() {
  const [items, setItems] = useState<DBCatalog[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [sourceId, setSourceId] = useState("");
  const [entityType, setEntityType] = useState("");
  const [sensitivity, setSensitivity] = useState("");
  const [keyword, setKeyword] = useState("");
  const [loading, setLoading] = useState(false);
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [form] = Form.useForm();

  async function load() {
    setLoading(true);
    try {
      const res = await listCatalogs({ source_id: sourceId || undefined, entity_type: entityType || undefined, sensitivity_level: sensitivity || undefined, keyword: keyword || undefined, page, page_size: 20 });
      setItems(res.items);
      setTotal(res.total);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, sourceId, entityType, sensitivity]);

  async function handleRegister(values: Record<string, unknown>) {
    try {
      const sid = String(values.source_id);
      await registerCatalog(sid, {
        source_id: sid,
        entity_name: String(values.entity_name),
        entity_type: String(values.entity_type ?? "TABLE"),
        schema_def: {},
        etl_sql: values.etl_sql ? String(values.etl_sql) : null,
      });
      message.success("目录实体已登记");
      setModalOpen(false);
      form.resetFields();
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "登记失败");
    }
  }

  async function handleBulkDeprecate() {
    if (selectedRowKeys.length === 0) return;
    const selected = items.filter((i) => selectedRowKeys.includes(`${i.source_id}-${i.entity_name}`));
    try {
      const res = await bulkDeprecateCatalogs(selected.map((i) => ({ source_id: i.source_id, entity_name: i.entity_name })));
      message.success(`批量废弃成功 ${res.succeeded.length} 项${res.failed.length ? `，失败 ${res.failed.length}` : ""}`);
      setSelectedRowKeys([]);
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "批量废弃失败");
    }
  }

  const columns = [
    { title: "数据源", dataIndex: "source_id", key: "source_id", render: (v: string) => <span className="mono">{v}</span> },
    { title: "实体", dataIndex: "entity_name", key: "entity_name", ellipsis: true, render: (v: string) => <span className="mono">{v}</span> },
    { title: "类型", dataIndex: "entity_type", key: "type", width: 90, render: (v: string) => <Tag>{enumLabel(ENTITY_TYPE_LABEL, v)}</Tag> },
    {
      title: "敏感度",
      dataIndex: "sensitivity_level",
      key: "sensitivity",
      width: 110,
      render: (v: string) => <Tag color={SENSITIVITY_COLOR[v]}>{SENSITIVITY_LABEL[v] ?? v}</Tag>,
    },
    { title: "责任人", dataIndex: "owner_id", key: "owner", width: 90, render: (v: number | null) => v ?? <Tag>无</Tag> },
    { title: "schema 缺失", dataIndex: "schema_incomplete", key: "incomplete", width: 120, render: (v: boolean) => (v ? <Tag color="error">是</Tag> : <Tag color="success">否</Tag>) },
    { title: "上游签名", dataIndex: "upstream_signature", key: "upstream", width: 130, render: (v: string) => (v ? (
      <Tooltip title={v}><span className="mono" style={{ fontSize: 11 }}>{v.slice(0, 12)}…</span></Tooltip>
    ) : <span className="muted">—</span>) },
  ];

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="page-kicker">Collection / Catalog</div>
          <h2>采集目录</h2>
          <p>采集器登记的元数据目录——含敏感度分级与 schema 完整性标记。</p>
        </div>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>登记实体</Button>
      </div>

      <Card
        extra={
          <Button icon={<ReloadOutlined />} onClick={load} loading={loading}>
            刷新
          </Button>
        }
      >
        <Space style={{ marginBottom: 12 }} wrap>
          <Input
            placeholder="Source ID"
            className="mono"
            style={{ width: 150 }}
            value={sourceId}
            onChange={(e) => setSourceId(e.target.value)}
            onPressEnter={() => { setPage(1); load(); }}
          />
          <Select
            allowClear
            placeholder="全部类型"
            style={{ width: 120 }}
            value={entityType || undefined}
            onChange={(v) => { setEntityType(v || ""); setPage(1); }}
            options={["TABLE", "VIEW", "FIELD"].map((v) => ({ value: v, label: ENTITY_TYPE_LABEL[v] ?? v }))}
          />
          <Select
            allowClear
            placeholder="全部敏感度"
            style={{ width: 140 }}
            value={sensitivity || undefined}
            onChange={(v) => { setSensitivity(v || ""); setPage(1); }}
            options={Object.entries(SENSITIVITY_LABEL).map(([v, l]) => ({ value: v, label: l }))}
          />
          <Input.Search
            placeholder="搜索实体"
            style={{ width: 200 }}
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            onSearch={() => { setPage(1); load(); }}
          />
          {selectedRowKeys.length > 0 && (
            <Button danger icon={<DeleteOutlined />} onClick={handleBulkDeprecate}>
              批量废弃（{selectedRowKeys.length}）
            </Button>
          )}
        </Space>

        <Table
          dataSource={items}
          columns={columns}
          rowKey={(r) => `${r.source_id}-${r.entity_name}`}
          loading={loading}
          rowSelection={{ selectedRowKeys, onChange: setSelectedRowKeys }}
          pagination={{ current: page, pageSize: 20, total, onChange: setPage, showTotal: (t) => `共 ${t} 条` }}
          locale={{ emptyText: "暂无目录实体" }}
        />
      </Card>

      <Modal title="登记目录实体" open={modalOpen} onCancel={() => setModalOpen(false)} onOk={() => form.submit()} okText="登记">
        <Form form={form} layout="vertical" onFinish={handleRegister} style={{ marginTop: 8 }}>
          <Form.Item name="source_id" label="Source ID" rules={[{ required: true }]}>
            <Input className="mono" />
          </Form.Item>
          <Form.Item name="entity_name" label="实体名" rules={[{ required: true }]}>
            <Input className="mono" placeholder="如 dwd_finance_order" />
          </Form.Item>
          <Form.Item name="entity_type" label="类型" initialValue="TABLE">
            <Select options={["TABLE", "VIEW", "FIELD"].map((v) => ({ value: v, label: ENTITY_TYPE_LABEL[v] ?? v }))} />
          </Form.Item>
          <Form.Item name="etl_sql" label="ETL SQL（可选）">
            <Input.TextArea rows={3} className="mono" />
          </Form.Item>
          <Alert type="info" showIcon message="schema_def 可在采集时自动填充；此处先登记元信息。" />
        </Form>
      </Modal>
    </div>
  );
}
